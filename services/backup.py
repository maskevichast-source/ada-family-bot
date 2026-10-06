"""Ежедневная резервная копия всей таблицы Google Sheets.

Копия создаётся целым файлом в папке на Google Drive (BACKUP_FOLDER_ID): в неё нужно
один раз дать доступ «Редактор» сервисному аккаунту бота. Хранятся последние KEEP копий;
удаляются ТОЛЬКО файлы с префиксом бота в этой же папке. Без BACKUP_FOLDER_ID ничего
не делается (функция «спит»).
"""
import datetime
import os
from pathlib import Path

BACKUP_PREFIX = "Ada backup Family_Finance"
KEEP = 30


def backup_folder_id() -> str:
    return (os.getenv("BACKUP_FOLDER_ID") or "").strip()


def _friendly_reason(error: Exception) -> str:
    text = str(error)
    low = text.lower()
    if "notfound" in low or "not found" in low or "404" in low:
        return "папка не найдена или сервисному аккаунту не выдан доступ «Редактор»"
    if "quota" in low or "storage" in low:
        return "на Google Drive сервисного аккаунта закончилось место"
    if "403" in low or "permission" in low or "forbidden" in low:
        return "нет прав на копирование: проверь доступ сервисного аккаунта к таблице и папке"
    return f"{type(error).__name__}: {text[:120]}"


def make_drive_backup(now: datetime.datetime) -> dict:
    """Делает копию на Google Drive и чистит старые. Возвращает {"ok", "title", "url", "deleted"} или {"ok": False, "reason"}."""
    folder = backup_folder_id()
    if not folder:
        return {"ok": False, "reason": "not_configured"}
    from services import sheets

    try:
        db = sheets.get_db()
        client = sheets.get_client()
        title = f"{BACKUP_PREFIX} {now.strftime('%Y-%m-%d %H%M')}"
        copy = client.copy(db.id, title=title, copy_permissions=False, folder_id=folder)
        new_id = getattr(copy, "id", None)
    except Exception as e:  # noqa: BLE001 - причина показывается пользователю
        print(f"[Бэкап] Не удалось создать копию: {e}")
        return {"ok": False, "reason": _friendly_reason(e)}

    deleted = 0
    try:
        files = [f for f in client.list_spreadsheet_files(title=BACKUP_PREFIX, folder_id=folder)
                 if str(f.get("name", "")).startswith(BACKUP_PREFIX) and f.get("id") not in (new_id, db.id)]
        files.sort(key=lambda f: f["name"], reverse=True)           # имя содержит дату, новые первыми
        for old in files[KEEP - 1:]:                                # свежая копия уже создана — считаем её за одну
            client.del_spreadsheet(old["id"])
            deleted += 1
    except Exception as e:  # noqa: BLE001 - чистка не должна ломать успешную копию
        print(f"[Бэкап] Копия создана, но старые удалить не удалось: {e}")
    return {"ok": True, "title": title, "url": f"https://docs.google.com/spreadsheets/d/{new_id}" if new_id else "",
            "deleted": deleted}


LOCAL_PREFIX = "Ada_backup_"


def local_dir() -> Path:
    base = Path(os.getenv("ADA_STATE_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "./data")
    return base / "backups"


def make_local_backup(now: datetime.datetime) -> dict:
    """Копия всей таблицы в .xlsx на постоянный том бота (не на Google Drive: у сервисного аккаунта
    на личной почте нет своего места, Drive отвечает «закончилось место»). Хранятся последние KEEP файлов,
    самые старые удаляются. Только файлы с префиксом бота в этой папке."""
    try:
        from gspread.utils import ExportFormat
        from services import sheets
        data = sheets.get_client().export(sheets.get_db().id, ExportFormat.EXCEL)
        if not data or len(data) < 1000 or not bytes(data[:2]) == b"PK":
            return {"ok": False, "reason": "Google вернул пустой или повреждённый файл"}
        folder = local_dir()
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{LOCAL_PREFIX}{now.strftime('%Y-%m-%d_%H%M')}.xlsx"
        target = folder / name
        tmp = folder / (name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
    except Exception as e:  # noqa: BLE001 - причина показывается пользователю
        print(f"[Бэкап] Не удалось сохранить копию: {e}")
        return {"ok": False, "reason": _friendly_reason(e)}
    deleted = 0
    try:
        files = sorted(folder.glob(f"{LOCAL_PREFIX}*.xlsx"), key=lambda f: f.name, reverse=True)
        for old in files[KEEP:]:
            old.unlink()
            deleted += 1
    except Exception as e:  # noqa: BLE001 - чистка не должна ломать успешную копию
        print(f"[Бэкап] Копия сохранена, но старые удалить не удалось: {e}")
    return {"ok": True, "title": name, "url": "", "deleted": deleted, "path": str(target)}


def make_backup(now: datetime.datetime) -> dict:
    """Копия таблицы: если задан BACKUP_FOLDER_ID — на Google Drive, иначе файлом на томе бота."""
    if backup_folder_id():
        return make_drive_backup(now)
    return make_local_backup(now)
