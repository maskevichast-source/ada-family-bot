"""Устойчивое распознавание чеков, изображений и PDF через OpenAI Vision."""

import asyncio
import base64
import json
import subprocess
import tempfile
from pathlib import Path

from openai import AsyncOpenAI

from config import OPENAI_API_KEY
from services.categories import (
    EXPENSE_CATEGORIES, INCOME_CATEGORIES, TYPE_EXPENSE, TYPE_INCOME,
    format_category_list, SUBCATEGORIES_MAP, get_time_context_hint,
    is_ambiguous_item,
)
from services.banks import BANK_ALIASES_PROMPT

_SUBCATEGORIES_VISION_PROMPT = "\n".join(
    f"- {cat}: {', '.join(subs)}" for cat, subs in SUBCATEGORIES_MAP.items()
)

VISION_SYSTEM_PROMPT = f"""
Ты — Ада, помощница семейного финансового чата Влада и Дианы.
Проанализируй изображение чека, квитанции, банковского перевода, зарплатного листа или экрана приложения банка.

КОНТЕКСТ ВРЕМЕНИ:
Текущее время и день недели даны отдельным сообщением. Используй их для контекста.

Верни строгий JSON:
{{
  "reply": "Короткий живой комментарий на русском",
  "transactions": [
    {{
      "amount": 500,
      "currency": "KZT",
      "type": "{TYPE_EXPENSE}",
      "bank": "BCC",
      "source": "BCC Pay",
      "funds_type": "Собственные",
      "resource": "Карта",
      "category": "Еда и продукты",
      "subcategory": "Напитки и вода",
      "merchant": "Название магазина",
      "necessity": "Need",
      "user_comment": "Описание покупки",
      "ai_comment": "Короткий комментарий Ады",
      "confidence": 1.0,
      "alternatives": []
    }}
  ],
  "trip_hint": null,
  "subscription_hint": null,
  "debt_hint": null
}}

TRIP_HINT (билет на самолёт/поезд/автобус МЕЖДУ ГОРОДАМИ/странами, бронь отеля) —
заполняй "trip_hint" ТОЛЬКО для таких документов, НЕ для обычного разового проездного
билета по городу (автобус/метро в своём городе — это просто "Транспорт и авто", не поездка):
{{
  "destination": "город или страна назначения",
  "dates": "даты поездки как удалось понять, например '2026-10-10 - 2026-10-20' или одна дата вылета",
  "note": "что за документ (билет на самолёт, ж/д, бронь отеля)"
}}
Если сомневаешься — оставь "trip_hint": null, лучше пропустить, чем создать поездку из обычного проездного.

SUBSCRIPTION_HINT — заполняй ТОЛЬКО когда явно узнаваема повторяющаяся подписка
(Netflix, Spotify, YouTube Premium, мобильная связь/интернет, спортзал с ежемесячным
абонементом и т.п. — известный сервис с регулярным платежом), НЕ для разовой покупки:
{{
  "name": "название сервиса, например YouTube Premium",
  "day_of_month": 15
}}
День месяца бери из даты чека. Если не уверена, что это именно повторяющаяся подписка —
оставь "subscription_hint": null.

DEBT_HINT — заполняй ТОЛЬКО когда из подписи к фото или самого документа ЯВНО следует,
что это заём/долг, а не обычная покупка или обычный перевод человеку (за товар/подарок/
услугу). Явные признаки: "занял", "одолжил", "в долг", "взаймы", "вернул долг".
Если debt_hint заполнен — "transactions" ДОЛЖЕН остаться пустым списком []: это не
трата семьи, а перемещение денег, которое должно попасть только в учёт долгов, а не
задвоиться ещё и обычным расходом.
{{
  "direction": "lent" | "borrowed",
  "counterparty": "имя человека (из подписи или получателя перевода)",
  "amount": число
}}
Направление определяй по конструкции фразы, а не только по глаголу — "занял" в
разговорном языке используется в ОБЕ стороны:
- "занял У Саши 5000" / "взял у Саши в долг" → direction: "borrowed" (я должен Саше).
- "занял Саше 5000" / "занял Ануару, перевёл 1500" / "одолжил Саше 5000" / "дал Саше в долг"
  → direction: "lent" (Саша должен мне) — то же "занял", но без предлога "у" перед именем,
  имя в дательном падеже (кому — Саше, Ануару), а не в родительном (у кого — у Саши).
Если сомневаешься, что это именно долг, а не обычный перевод/покупка — оставь
"debt_hint": null и запиши обычной транзакцией, как раньше. Ложное срабатывание хуже
пропуска: лишний вопрос "это точно долг?" не страшен, а вот случайно посчитанный долгом
обычный платёж за услугу — плохо.

КАТЕГОРИИ РАСХОДОВ (используй ТОЛЬКО эти названия):
{format_category_list(EXPENSE_CATEGORIES)}

КАТЕГОРИИ ДОХОДОВ (для пополнений, переводов и зарплат):
{format_category_list(INCOME_CATEGORIES)}

СТРОГИЕ ПОДКАТЕГОРИИ РАСХОДОВ:
{_SUBCATEGORIES_VISION_PROMPT}

ПОГАШЕНИЯ КРЕДИТОВ И РАССРОЧЕК (важно, чтобы не было путаницы):
- Платёж БАНКУ по уже взятому кредиту, рассрочке или займу ("погашение кредита", "платёж по рассрочке",
  Kaspi Red, ежемесячный платёж и т.п.) ВСЕГДА: category "Финансовые расходы и переводы",
  subcategory "Кредиты и рассрочки" — независимо от того, на что этот кредит был взят (техника,
  ремонт, телефон). НЕ "Электроника и техника" и НЕ "Банковские комиссии".
- А вот ПОКУПКА через рассрочку или кредитную карту (товар куплен в рассрочку) остаётся в категории
  самого товара, а funds_type ставь "Рассрочка" или "Кредитные".

ПОДКАТЕГОРИИ:
- Для расходов "subcategory" ОБЯЗАН быть из списка выше. НЕ придумывай новые.
- Для доходов "subcategory" оставь пустой строкой "".

necessity:
- Need: еда домой, вода, транспорт на работу, лекарства, ЖКХ, товары для ремонта дома, корм питомцу
- Want: сигареты, энергетики, алкоголь, косметика, кафе, рестораны, доставка, развлечения
- "Алкоголь, табак и энергетики" → ВСЕГДА Want
- "Красота и уход" → Want
- "Развлечения и хобби" → Want

{BANK_ALIASES_PROMPT}

ТИП ОПЕРАЦИИ:
- Покупки и оплата счетов — РАСХОД (type = "{TYPE_EXPENSE}")
- Входящий перевод, зарплата, аванс, бонус, пополнение баланса или возврат — ДОХОД (type = "{TYPE_INCOME}")
- Для доходов category выбери из списка доходов (например "Зарплата", "Подарки и переводы (входящие)", "Кэшбэк и прочие поступления")

КОММЕНТАРИИ (поля "reply" и "ai_comment"):
- Пиши как живой, внимательный человек, а не как бот, обязанный похвалить
  любую покупку. Каждый раз формулируй по-своему — не повторяй один и тот
  же оборот ("отличный выбор", "для бодрого дня/утра", "прекрасно!") снова
  и снова, даже для похожих покупок. Если не знаешь, что сказать интересного
  — лучше короткая нейтральная констатация факта, чем шаблонный восторг.
- Если в подписи названо, ЧТО куплено ("мусорное ведро", "пачка сигарет"), комментарий
  обязан опираться на это и называть саму вещь. Название магазина на скриншоте (Ozon,
  Kaspi, Wildberries) НЕ заменяет подпись: подпись важнее. Запрещено писать "возможно",
  "что-то полезное", "какая-то покупка" и делать вид, что не знаешь, что куплено, когда
  подпись это прямо говорит. Если подписи нет — не выдумывай содержимое покупки, пиши
  только то, что видно (магазин, сумма, время).
- Заголовок "Записано по чеку" уже сообщает, что покупка записана, поэтому НЕ начинай
  комментарий со слов "Покупка ... записана/зафиксирована/приобретена" и не пересказывай
  сумму и магазин — это дубль. Комментарий должен добавлять что-то живое: реакцию на
  конкретную вещь, связь с недавним, к месту замечание — или быть совсем коротким.
  Канцелярит ("приобретены", "осуществлена покупка", "трата зафиксирована") запрещён:
  говори так, как говорят в семейном чате.
- Обычные бытовые траты (еда, транспорт, ЖКХ, всё для дома) не обязаны
  получать похвалу — иногда достаточно просто спокойно зафиксировать покупку.
- Сигареты, энергетики, алкоголь: не хвали и не одобряй ("для бодрого утра!",
  "отличный выбор" тут неуместны). НИКОГДА не цитируй название категории в
  кавычках ("из категории 'Алкоголь, табак и энергетики'") — так говорит робот,
  живой человек называет саму вещь (пачка, банка). Заезженные обороты вроде
  "Снова покупка из категории…", "Стоит подумать/обратить внимание на частоту"
  — запрещены совсем: это нотация, а не наблюдение.
  Если в "НЕДАВНИЙ КОНТЕКСТ СЕМЬИ" НЕТ строки "ФАКТ (посчитано кодом...)" — про
  частоту, привычку и категорию не говори ничего: либо короткая реакция на
  конкретную вещь, либо просто нейтральная фиксация покупки.
  Если такая строка ЕСТЬ — отметь это одной короткой фразой своими словами, с
  конкретным числом, как заметил бы близкий человек ("четвёртая пачка за
  неделю"), без советов, без "стоит", без вопросов и без смайлика-укора.
- Если видно, что это зарплата или важный/приятный перевод — порадуйся
  за семью, здесь тёплый и позитивный тон уместен.
- Если ниже дан блок "НЕДАВНИЙ КОНТЕКСТ СЕМЬИ" — учитывай его, если он ДЕЙСТВИТЕЛЬНО относится к
  этой покупке (например, вчера покупали коробки для переезда, а сегодня — такси и грузчики: это,
  скорее всего, один и тот же переезд, можно об этом упомянуть). Не притягивай контекст за уши,
  если он не подходит — тогда просто обычный короткий комментарий без ссылки на прошлое.

ВАЛЮТА:
- "currency" — трёхбуквенный код валюты (USD/RUB/EUR/...). Тенге — ВСЕГДА "KZT", независимо от
  того, каким символом она обозначена на чеке (₸, Т, тг, KZT, тенге — всё это одно и то же, тенге).
  Не путай кириллическую букву "Т" (казахстанский символ тенге) с валютой другой страны.
  "amount" — сумма в этой валюте как есть на чеке. Конвертацию в KZT сделает код по официальному
  курсу, сама не пересчитывай и не выдумывай курс.
"""


def _parse_json(content: object) -> dict:
    if not isinstance(content, str):
        return {}
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").strip()
        cleaned = cleaned.removesuffix("```").strip()
    parsed = json.loads(cleaned)
    return parsed if isinstance(parsed, dict) else {}


def _render_pdf(pdf_path: Path, output_path: Path) -> Path:
    rendered_prefix = output_path.with_suffix("")
    try:
        result = subprocess.run(
            ["pdftoppm", "-f", "1", "-singlefile", "-png", "-r", "150",
             str(pdf_path), str(rendered_prefix)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode == 0:
            rendered_path = rendered_prefix.with_suffix(".png")
            if rendered_path.exists():
                return rendered_path
    except Exception:
        pass
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(pdf_path))
        page = pdf[0]
        image = page.render(scale=2).to_pil()
        rendered_path = rendered_prefix.with_suffix(".png")
        image.save(str(rendered_path), "PNG")
        if rendered_path.exists():
            return rendered_path
    except Exception as error:
        raise RuntimeError(f"Не удалось преобразовать PDF: {error}")
    raise RuntimeError("PDF не содержит доступной страницы")


MAX_PDF_PAGES = 12


def _render_pdf_pages(pdf_path: Path, output_dir: Path, max_pages: int = MAX_PDF_PAGES) -> list[Path]:
    """Рендерит ВСЕ страницы PDF в PNG (а не только первую, как _render_pdf) —
    для длинных чеков/выписок из нескольких страниц. Отказывается работать
    с чем-то похожим на целую книгу — большой PDF съест уйму токенов на
    Vision-запрос и с большой вероятностью означает, что это не чек."""
    import fitz  # PyMuPDF
    doc = fitz.open(str(pdf_path))
    try:
        page_count = doc.page_count
        if page_count > max_pages:
            raise ValueError(
                f"В PDF {page_count} страниц — это больше {max_pages}, похоже, это не чек. "
                f"Пришли, пожалуйста, отдельные страницы или сфотографируй чек."
            )
        images = []
        for i in range(page_count):
            page = doc.load_page(i)
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            out_path = Path(output_dir) / f"{pdf_path.stem}_p{i + 1}.png"
            pix.save(str(out_path))
            images.append(out_path)
        return images
    finally:
        doc.close()


async def parse_receipt(file_bytes: bytes, filename: str, caption: str = "",
                        user_name: str = "Пользователь", recent_context: str = "") -> dict:
    if not file_bytes:
        print("[Распознавание] Получен пустой файл")
        return {}

    try:
        with tempfile.TemporaryDirectory(prefix="receipt_") as temporary_dir:
            source_path = Path(temporary_dir) / (Path(filename or "receipt").name or "receipt")
            source_path.write_bytes(file_bytes)
            suffix = source_path.suffix.lower()
            image_path = source_path
            mime_type = {".png": "image/png", ".jpg": "image/jpeg",
                         ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(suffix)
            if suffix == ".pdf":
                image_path = _render_pdf(source_path, Path(temporary_dir) / "receipt.png")
                mime_type = "image/png"
            if mime_type is None:
                print(f"[Распознавание] Неподдерживаемый формат: {suffix or 'без расширения'}")
                return {}

            encoded_image = base64.b64encode(image_path.read_bytes()).decode("ascii")
            client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=45.0, max_retries=2)
            try:
                from services.timezone import now_astana
                now = now_astana()
                time_hint = get_time_context_hint(now.hour, now.weekday())
                weekday_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"][now.weekday()]

                prompt = (
                    f"Файл от {user_name}. "
                    + (f"Подпись пользователя (это и есть ЧТО КУПЛЕНО, опирайся на неё в комментарии): {caption}.\n"
                       if caption else "Подписи нет.\n")
                    +
                    f"Текущее время: {now.strftime('%Y-%m-%d %H:%M:%S')}, {weekday_ru} "
                    f"(ориентируйся только на это название дня, не угадывай).\n"
                    f"Контекст: {time_hint}.\n"
                    + (f"НЕДАВНИЙ КОНТЕКСТ СЕМЬИ (последние сообщения/траты, самое новое внизу):\n{recent_context}\n" if recent_context else "")
                    + "Распознай чек/скриншот перевода и верни JSON."
                )
                response = await client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {"role": "system", "content": VISION_SYSTEM_PROMPT},
                        {"role": "user", "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {
                                "url": f"data:{mime_type};base64,{encoded_image}"}},
                        ]},
                    ],
                    response_format={"type": "json_object"},
                    max_tokens=900,
                )

                choice = response.choices[0]
                if getattr(choice, "finish_reason", None) == "length":
                    # Ответ обрезан лимитом токенов — это значит, что модель
                    # НЕ договорила JSON. Молча принять его — значит рискнуть
                    # либо потерять часть покупок из длинного чека, либо
                    # словить невалидный JSON. Явно просим прислать по частям.
                    print("[Распознавание] Ответ Vision обрезан по finish_reason=length")
                    return {
                        "transactions": [],
                        "reply": "Чек слишком длинный, ответ модели обрезался. Пришли, пожалуйста, частями (например, по фото на каждые несколько позиций).",
                    }

                result = _parse_json(choice.message.content)

                transactions = result.get("transactions", [])
                for tx in transactions:
                    user_comment = str(tx.get("user_comment", "") or caption)
                    is_ambig, alternatives = is_ambiguous_item(user_comment)
                    if is_ambig and tx.get("confidence", 1.0) >= 0.9:
                        tx["confidence"] = 0.6
                        tx["alternatives"] = alternatives

                return result
            finally:
                # Раньше клиент никогда явно не закрывался — при большом
                # количестве чеков в день это медленно копит открытые
                # HTTP-сессии/соединения.
                await client.close()
    except asyncio.TimeoutError:
        print("[Распознавание] OpenAI не ответил вовремя")
    except Exception as error:
        print(f"[Распознавание] Сбой API или формата файла: {error}")
    return {}
