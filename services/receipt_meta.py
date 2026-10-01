"""Вспомогательные функции для чеков: дата и время ОПЕРАЦИИ, сводка покупок, текст PDF.

Диана иногда присылает чеки через день, два или неделю. Время операции берётся из самого
чека (если оно там есть и правдоподобно), иначе — время отправки боту, как раньше.
"""
import datetime
import re
from pathlib import Path

from services.timezone import ASTANA_TZ

MAX_AGE_DAYS = 370           # чек старше года — скорее всего ошибка распознавания года
FUTURE_TOLERANCE_MIN = 15    # дата из будущего (с запасом на разницу часов) не принимается
ITEMS_SUMMARY_MAX_CHARS = 160

_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?")
_DMY = re.compile(r"^(\d{1,2})[./](\d{1,2})[./](\d{4})(?:[ ,T]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?")


def parse_occurred_at(raw, now: datetime.datetime) -> datetime.datetime | None:
    """Дата/время операции из чека -> datetime по Астане или None (тогда берём «сейчас»).

    Принимает «2026-09-25 07:04», «2026-09-25T07:04:19», «25.09.2026 07:04», а также одну дату.
    Только дата: берём полдень (чтобы не уехать на соседний день), а если это сегодня — None
    (время отправки точнее полудня). Из будущего и старше MAX_AGE_DAYS — None.
    """
    text = str(raw or "").strip()
    if not text or text.lower() in ("null", "none", "-"):
        return None
    m = _ISO.match(text)
    if m:
        y, mo, d, hh, mm, ss = m.groups()
    else:
        m = _DMY.match(text)
        if not m:
            return None
        d, mo, y, hh, mm, ss = m.groups()
    try:
        date_only = hh is None
        value = datetime.datetime(int(y), int(mo), int(d), int(hh or 12), int(mm or 0), int(ss or 0), tzinfo=ASTANA_TZ)
    except ValueError:
        return None
    now = now.astimezone(ASTANA_TZ) if now.tzinfo else now.replace(tzinfo=ASTANA_TZ)
    if date_only and value.date() == now.date():
        return None
    if value > now + datetime.timedelta(minutes=FUTURE_TOLERANCE_MIN):
        return None
    if now - value > datetime.timedelta(days=MAX_AGE_DAYS):
        return None
    return value


def format_sheet_datetime(value: datetime.datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


_GENERIC_SUMMARIES = {"покупка", "товары", "товар", "продукты", "чек", "оплата", "не указано", "нет данных", "null", "none", "-"}


def clean_items_summary(raw) -> str:
    """Короткая сводка «что куплено» из чека: без переносов, не длиннее лимита, без пустых слов."""
    text = " ".join(str(raw or "").split()).strip(" .;,")
    if text.lower() in _GENERIC_SUMMARIES:
        return ""
    if len(text) > ITEMS_SUMMARY_MAX_CHARS:
        text = text[: ITEMS_SUMMARY_MAX_CHARS - 1].rstrip(" ,;") + "…"
    return text


MAX_PDF_TEXT_CHARS = 14000
MIN_PDF_TEXT_CHARS = 40


def extract_pdf_text(path: Path) -> str:
    """Выделяемый текст PDF (у чеков Kaspi и банков он есть) — точнее, чем чтение картинки.
    Пустая строка, если PDF — скан без текстового слоя или не открылся."""
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(str(path))
        try:
            parts = [doc.load_page(i).get_text("text") for i in range(doc.page_count)]
        finally:
            doc.close()
    except Exception as e:  # noqa: BLE001
        print(f"[Чек] Не удалось извлечь текст PDF: {e}")
        return ""
    text = "\n".join(p.strip() for p in parts if p.strip())
    if len(text) < MIN_PDF_TEXT_CHARS:
        return ""
    return text[:MAX_PDF_TEXT_CHARS]
