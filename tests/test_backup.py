import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services import backup, sheets, state
from services.timezone import ASTANA_TZ
from test_media_schedulers import StopTick, app, frozen, tick  # noqa: F401  (фикстура app)
from test_handlers import handler  # noqa: F401

NOW = dt.datetime(2026, 10, 4, 3, 35, tzinfo=ASTANA_TZ)      # воскресенье


class FakeClient:
    def __init__(self, existing=(), fail_copy=None, fail_delete=False):
        self.files = [dict(f) for f in existing]
        self.copies, self.deleted = [], []
        self.fail_copy, self.fail_delete = fail_copy, fail_delete

    def copy(self, file_id, title=None, copy_permissions=False, folder_id=None, copy_comments=True):
        if self.fail_copy:
            raise self.fail_copy
        self.copies.append({"file_id": file_id, "title": title, "folder_id": folder_id, "perm": copy_permissions})
        self.files.append({"id": "NEW", "name": title})
        return SimpleNamespace(id="NEW")

    def list_spreadsheet_files(self, title=None, folder_id=None):
        return [f for f in self.files if title in f["name"]]

    def del_spreadsheet(self, file_id):
        if self.fail_delete:
            raise RuntimeError("нет прав")
        self.deleted.append(file_id)


def wire(monkeypatch, client, folder="FOLDER"):
    monkeypatch.setattr(sheets, "get_db", lambda: SimpleNamespace(id="SRC"))
    monkeypatch.setattr(sheets, "get_client", lambda: client)
    if folder:
        monkeypatch.setenv("BACKUP_FOLDER_ID", folder)
    else:
        monkeypatch.delenv("BACKUP_FOLDER_ID", raising=False)


def old_files(n):
    return [{"id": f"OLD{i}", "name": f"{backup.BACKUP_PREFIX} 2026-09-{i:02d} 0330"} for i in range(1, n + 1)]


def test_not_configured_does_nothing(monkeypatch):
    client = FakeClient()
    wire(monkeypatch, client, folder=None)
    assert backup.make_backup(NOW) == {"ok": False, "reason": "not_configured"}
    assert client.copies == []


def test_copy_goes_to_folder_without_permissions_and_names_by_date(monkeypatch):
    client = FakeClient()
    wire(monkeypatch, client)
    out = backup.make_backup(NOW)
    assert out["ok"] and out["title"] == f"{backup.BACKUP_PREFIX} 2026-10-04 0335" and out["deleted"] == 0
    assert client.copies == [{"file_id": "SRC", "title": out["title"], "folder_id": "FOLDER", "perm": False}]
    assert out["url"].endswith("/NEW")


def test_retention_keeps_latest_and_never_touches_foreign_files(monkeypatch):
    foreign = [{"id": "PHOTO", "name": "Фото отпуска"}, {"id": "SRC", "name": backup.BACKUP_PREFIX + " source"}]
    client = FakeClient(existing=old_files(10) + foreign)
    wire(monkeypatch, client)
    out = backup.make_backup(NOW)
    # после копии их 11; остаются 8 свежих (новая + 7 самых новых старых), удаляются 3 самых старых
    assert out["deleted"] == 3
    assert sorted(client.deleted) == ["OLD1", "OLD2", "OLD3"]
    assert "PHOTO" not in client.deleted and "SRC" not in client.deleted and "NEW" not in client.deleted


def test_delete_failure_does_not_spoil_successful_backup(monkeypatch):
    client = FakeClient(existing=old_files(12), fail_delete=True)
    wire(monkeypatch, client)
    out = backup.make_backup(NOW)
    assert out["ok"] and out["deleted"] == 0


@pytest.mark.parametrize("error,fragment", [
    (RuntimeError("404 File not found: FOLDER"), "папка не найдена"),
    (RuntimeError("storageQuotaExceeded"), "закончилось место"),
    (RuntimeError("403 The caller does not have permission"), "нет прав"),
    (ValueError("что-то странное"), "ValueError"),
])
def test_copy_errors_become_readable_reasons(monkeypatch, error, fragment):
    wire(monkeypatch, FakeClient(fail_copy=error))
    out = backup.make_backup(NOW)
    assert out["ok"] is False and fragment in out["reason"]


# ---------- команда и планировщик ----------

def test_backup_command_texts(app, monkeypatch):
    def run(result):
        monkeypatch.setattr(app.backup_module, "make_backup", lambda now: result)
        msg = SimpleNamespace(chat=SimpleNamespace(id=1, type="private"), answer=AsyncMock(), message_id=1)
        asyncio.run(app.cmd_backup(msg))
        return msg.answer.call_args.args[0]
    assert "Резервные копии пока не настроены" in run({"ok": False, "reason": "not_configured"}) 
    assert "BACKUP_FOLDER_ID" in run({"ok": False, "reason": "not_configured"})
    ok = run({"ok": True, "title": "T", "url": "https://x/y", "deleted": 2})
    assert "T" in ok and "https://x/y" in ok and "удалено: 2" in ok
    assert "папка не найдена" in run({"ok": False, "reason": "папка не найдена"})


def test_scheduler_runs_only_sunday_night_once_per_week(app, monkeypatch):
    calls = []
    monkeypatch.setattr(app.backup_module, "make_backup", lambda now: calls.append(now) or {"ok": True, "title": "T"})
    monkeypatch.setenv("BACKUP_FOLDER_ID", "F")
    frozen(monkeypatch, app, NOW)
    tick(app.backup_scheduler)
    assert len(calls) == 1 and state.get("scheduler", "backup:2026-W40")
    tick(app.backup_scheduler)                                            # маркер недели уже стоит
    assert len(calls) == 1
    for when in (NOW.replace(day=5), NOW.replace(hour=4), NOW.replace(minute=10)):   # пн / не тот час / до 03:30
        frozen(monkeypatch, app, when)
        tick(app.backup_scheduler)
    assert len(calls) == 1


def test_scheduler_disabled_without_folder(app, monkeypatch):
    calls = []
    monkeypatch.setattr(app.backup_module, "make_backup", lambda now: calls.append(now))
    monkeypatch.delenv("BACKUP_FOLDER_ID", raising=False)
    frozen(monkeypatch, app, NOW)
    tick(app.backup_scheduler)
    assert calls == []


def test_scheduler_failure_retries_then_reports_once(app, monkeypatch):
    calls = []
    monkeypatch.setattr(app.backup_module, "make_backup", lambda now: calls.append(now) or {"ok": False, "reason": "нет прав"})
    monkeypatch.setenv("BACKUP_FOLDER_ID", "F")
    frozen(monkeypatch, app, NOW)
    app.bot.send_message.reset_mock()
    tick(app.backup_scheduler)
    assert len(calls) == 1 and not state.get("scheduler", "backup:2026-W40")    # маркера нет: повторим позже
    assert app.bot.send_message.call_count == 0                                  # первую неудачу в чат не несём
