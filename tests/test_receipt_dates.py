"""Чеки: время операции из самого чека (Диана присылает позже), сводка «что куплено»,
тип средств по карте, комиссия за перевод, защита от дублей, текст PDF."""
import asyncio
import datetime as dt
from unittest.mock import AsyncMock

import pytest

from services import banks, receipt_meta, sheets, state
from services.timezone import ASTANA_TZ, now_astana
from test_handlers import handler  # noqa: F401
from test_media_schedulers import media_message

NOW = dt.datetime(2026, 9, 30, 21, 0, tzinfo=ASTANA_TZ)


# ---------- разбор даты ----------

@pytest.mark.parametrize("raw,expected", [
    ("2026-09-25 07:04", dt.datetime(2026, 9, 25, 7, 4)),
    ("2026-09-25T07:04:19", dt.datetime(2026, 9, 25, 7, 4, 19)),
    ("25.09.2026 07:04:19", dt.datetime(2026, 9, 25, 7, 4, 19)),
    ("30.09.2026, 12:56", dt.datetime(2026, 9, 30, 12, 56)),
    ("2026-09-25", dt.datetime(2026, 9, 25, 12, 0)),                 # только дата — полдень
    ("2026-09-30", None),                                            # только дата «сегодня» — лучше время отправки
    ("2026-10-02 10:00", None),                                      # из будущего
    ("2024-01-01 10:00", None),                                      # старше года
    ("31.02.2026 10:00", None), ("вчера", None), ("", None), (None, None), ("null", None),
])
def test_parse_occurred_at(raw, expected):
    got = receipt_meta.parse_occurred_at(raw, NOW)
    assert (got.replace(tzinfo=None) if got else None) == expected


def test_items_summary_cleaning():
    assert receipt_meta.clean_items_summary("  кабель\nUgreen  USB-C. ") == "кабель Ugreen USB-C"
    assert receipt_meta.clean_items_summary("покупка") == "" and receipt_meta.clean_items_summary(None) == ""
    long = receipt_meta.clean_items_summary("овощи, " * 80)
    assert len(long) == receipt_meta.ITEMS_SUMMARY_MAX_CHARS and long.endswith("…")


def test_pdf_text_layer_is_extracted_and_scans_are_not(tmp_path):
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    pdfmetrics.registerFont(TTFont("DV", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
    with_text = tmp_path / "r.pdf"
    c = canvas.Canvas(str(with_text))
    c.setFont("DV", 11)
    for i, line in enumerate(["Фискальный чек", "Оплата совершена 5 175 ₸", "Оплачено с Kaspi Red", "30.09.2026 20:15"]):
        c.drawString(50, 780 - i * 20, line)
    c.save()
    text = receipt_meta.extract_pdf_text(with_text)
    assert "Оплачено с Kaspi Red" in text and "30.09.2026 20:15" in text
    blank = tmp_path / "scan.pdf"
    c = canvas.Canvas(str(blank)); c.showPage(); c.save()
    assert receipt_meta.extract_pdf_text(blank) == ""                        # нет текстового слоя -> пойдёт как картинка
    assert receipt_meta.extract_pdf_text(tmp_path / "нет-файла.pdf") == ""


# ---------- карта -> «рассрочка или свои» ----------

def test_installment_cards():
    for src in ("Ozen", "Kaspi Red", "ForteBlack", "Картакарта", "SmartCard", "Halyk Рассрочка"):
        assert banks.funds_type_for_source(src) == "Рассрочка"
    for src in ("Kaspi Gold", "BCC Pay", "Forte Card", "", None):
        assert banks.funds_type_for_source(src) is None


# ---------- запись в таблицу ----------

def _last(db):
    return db.worksheet("Transactions").get_all_values()[-1]


def test_receipt_time_is_used_and_bad_values_fall_back_to_now(db):
    sheets.append_transaction({"amount": 1000, "category": "Еда и продукты", "occurred_at": "2026-09-25 07:04:19"})
    assert _last(db)[1] == "2026-09-25 07:04:19"
    now_year = now_astana().strftime("%Y")
    for bad in ("2099-01-01 10:00", "мусор", "", None):
        sheets.append_transaction({"amount": 1001, "category": "Еда и продукты", "occurred_at": bad})
        assert _last(db)[1].startswith(now_year) and _last(db)[1][:10] == now_astana().strftime("%Y-%m-%d")


def test_funds_type_is_forced_from_the_card(db):
    sheets.append_transaction({"amount": 500, "bank": "Home Credit", "source": "Ozen", "funds_type": "Собственные",
                               "category": "Еда и продукты"})
    assert _last(db)[8] == "Рассрочка"                                       # раньше модель записывала «Собственные»
    sheets.append_transaction({"amount": 500, "bank": "Kaspi", "source": "Kaspi Gold", "funds_type": "Рассрочка",
                               "category": "Еда и продукты"})
    assert _last(db)[8] == "Рассрочка"                                       # явное значение модели не затираем
    sheets.append_transaction({"amount": 34450, "bank": "Kaspi", "source": "Kaspi Red",
                               "category": "Финансовые расходы и переводы", "subcategory": "Кредиты и рассрочки"})
    assert _last(db)[8] == "Собственные"                                     # платёж банку по кредиту — свои деньги


# ---------- дубли ----------

def test_duplicate_finder(db):
    sheets.append_transaction({"amount": 5175, "merchant": 'ТОО "ART-IT"', "category": "Электроника и техника",
                               "occurred_at": "2026-09-30 20:15"})
    same = {"amount": 5175, "merchant": "ART-IT", "type": "РАСХОД", "occurred_at": "2026-09-30 20:16"}
    assert sheets.find_duplicate_receipt(same) is not None                   # другое написание, та же минута
    assert sheets.find_duplicate_receipt(dict(same, merchant="Magnum")) is None            # другой магазин
    assert sheets.find_duplicate_receipt(dict(same, amount=5176)) is None
    assert sheets.find_duplicate_receipt(dict(same, occurred_at="2026-09-30 20:25")) is None   # другое время
    assert sheets.find_duplicate_receipt(dict(same, occurred_at="")) is None                  # без времени чека не сравниваем
    assert sheets.find_duplicate_receipt(dict(same, type="ДОХОД")) is None


# ---------- чек целиком: handle_media ----------

_MESSAGE_IDS = iter(range(1000, 2000))


def _run(handler, monkeypatch, result, caption="", doc=True):
    from handlers import media_handler as mh
    monkeypatch.setattr(mh, "parse_receipt", AsyncMock(return_value=result))
    m = media_message(caption, doc=doc)
    m.message_id = next(_MESSAGE_IDS)          # у каждого настоящего сообщения свой ID
    asyncio.run(mh.handle_media(m))
    return " ".join(str(m.answer.call_args.args[0]).split())


def _kaspi(occurred, **kw):
    tx = {"amount": 5175, "currency": "KZT", "type": "РАСХОД", "category": "Электроника и техника",
          "subcategory": "Гаджеты и техника", "bank": "Kaspi", "source": "Kaspi Red", "funds_type": "Рассрочка",
          "resource": "Карта", "merchant": 'ТОО "ART-IT"', "occurred_at": occurred,
          "items_summary": "кабель Ugreen USB-C — HDMI 1,5 м", "user_comment": "кабель"}
    tx.update(kw)
    return {"transactions": [tx], "reply": "Ок."}


def test_late_receipt_with_items_is_saved_instantly_with_real_date(handler, db, monkeypatch):
    when = (now_astana() - dt.timedelta(days=5)).replace(hour=20, minute=15, second=0, microsecond=0)
    said = _run(handler, monkeypatch, _kaspi(when.strftime("%Y-%m-%d %H:%M")))
    row = _last(db)
    assert row[1] == when.strftime("%Y-%m-%d %H:%M:00")                     # дата чека, а не отправки
    assert row[14] == "кабель Ugreen USB-C — HDMI 1,5 м"                    # что куплено — из чека, без вопроса
    assert row[7] == "Kaspi Red" and row[8] == "Рассрочка"
    assert "чек от " + when.strftime("%d.%m.%Y 20:15") in said and "рассрочка" in said


def test_receipt_without_items_still_asks_for_comment_and_keeps_time(handler, db, monkeypatch):
    when = (now_astana() - dt.timedelta(days=2)).replace(hour=16, minute=43, second=0, microsecond=0)
    before = len(db.worksheet("Transactions").get_all_values())
    said = _run(handler, monkeypatch, {"transactions": [{
        "amount": 500, "currency": "KZT", "type": "РАСХОД", "category": "Еда и продукты", "subcategory": "Супермаркет и рынок",
        "bank": "Home Credit", "source": "Ozen", "merchant": "Mangilik El Shop", "occurred_at": when.strftime("%Y-%m-%d %H:%M"),
        "items_summary": ""}], "reply": "Скриншот Wallet."}, doc=False)
    assert len(db.worksheet("Transactions").get_all_values()) == before        # ждёт комментария
    assert "комментарий" in said.lower() or "Скриншот" in said


def test_caption_beats_receipt_summary_but_date_still_from_receipt(handler, db, monkeypatch):
    when = (now_astana() - dt.timedelta(days=3)).replace(hour=7, minute=4, second=0, microsecond=0)
    _run(handler, monkeypatch, _kaspi(when.strftime("%Y-%m-%d %H:%M")), caption="кабель для приставки")
    row = _last(db)
    assert row[14] == "кабель для приставки" and row[1] == when.strftime("%Y-%m-%d %H:%M:00")


def test_same_receipt_twice_is_not_duplicated_unless_told(handler, db, monkeypatch):
    when = (now_astana() - dt.timedelta(days=1)).replace(hour=12, minute=48, second=0, microsecond=0)
    result = _kaspi(when.strftime("%Y-%m-%d %H:%M"), amount=1800, merchant="Минимаркет Мәңгілік ел")
    _run(handler, monkeypatch, result)
    n = len(db.worksheet("Transactions").get_all_values())
    said = _run(handler, monkeypatch, _kaspi(when.strftime("%Y-%m-%d %H:%M"), amount=1800, merchant="Минимаркет Мәңгілік ел"))
    assert "уже записан" in said and "не дубль" in said
    assert len(db.worksheet("Transactions").get_all_values()) == n
    _run(handler, monkeypatch, _kaspi(when.strftime("%Y-%m-%d %H:%M"), amount=1800, merchant="Минимаркет Мәңгілік ел"),
         caption="ещё одна, не дубль")
    assert len(db.worksheet("Transactions").get_all_values()) == n + 1


def test_transfer_commission_becomes_its_own_expense(handler, db, monkeypatch):
    when = (now_astana() - dt.timedelta(days=1)).replace(hour=15, minute=38, second=0, microsecond=0)
    stamp = when.strftime("%Y-%m-%d %H:%M")
    base = {"currency": "KZT", "type": "РАСХОД", "bank": "Forte", "source": "Forte Card", "occurred_at": stamp, "merchant": "Forte"}
    _run(handler, monkeypatch, {"transactions": [
        dict(base, amount=1700, category="Подарки, праздники и благотворительность", subcategory="Подарки"),
        dict(base, amount=119, category="Финансовые расходы и переводы", subcategory="Банковские комиссии",
             user_comment="Комиссия за перевод")], "reply": ""}, caption="скинулись на букет")
    rows = db.worksheet("Transactions").get_all_values()[-2:]
    assert [r[4] for r in rows] == [1700, 119]
    assert rows[1][10] == "Финансовые расходы и переводы" and rows[1][11] == "Банковские комиссии"
    assert all(r[1] == when.strftime("%Y-%m-%d %H:%M:00") for r in rows)
