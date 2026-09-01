"""Устойчивое распознавание чеков, изображений и PDF через OpenAI Vision."""

import asyncio
import base64
import json
import subprocess
import tempfile
from pathlib import Path

from openai import AsyncOpenAI

from config import OPENAI_API_KEY
from services.categories import EXPENSE_CATEGORIES, TYPE_EXPENSE, TYPE_INCOME, format_category_list
from services.banks import BANK_ALIASES_PROMPT


VISION_SYSTEM_PROMPT = f"""
Ты — Ада, помощница семейного финансового чата Влада и Дианы.
Проанализируй изображение чека, банковского перевода или оплаты.
Верни только строгий JSON такого вида:
{{
  "reply": "Короткий комментарий на русском",
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
      "subcategory": "Продукты",
      "merchant": "Название магазина",
      "necessity": "Need",
      "user_comment": "Описание покупки",
      "ai_comment": "Короткий комментарий Ады"
    }}
  ]
}}
Если на изображении несколько покупок, верни несколько элементов. Если данных нет,
верни transactions как пустой массив. Суммы указывай числами, валюту по умолчанию KZT.

  ПОДКАТЕГОРИИ (ОБЯЗАТЕЛЬНО ВЫБЕРИ ТОЛЬКО ИЗ ЭТОГО СПИСКА):
{format_subcategory_dict(STRICT_SUBCATEGORIES)}

ЖЕСТКИЕ ПРАВИЛА ДЛЯ ОСТАЛЬНЫХ ПОЛЕЙ (ПУСТЫХ БЫТЬ НЕ ДОЛЖНО):
- "necessity": ТОЛЬКО "Need" (базовые потребности) или "Want" (хотелки, развлечения). НИКАКИХ "обязательное" или русских слов.
- "bank": ТОЛЬКО из списка: BCC, Kaspi, Halyk, Forte, Freedom, Jusan, Евразийский, Наличные. Если способ оплаты "Наличные", то bank должен быть "Наличные". Если банк неизвестен, но это безнал — ставь "Kaspi".
- "subcategory": выбери подкатегорию, строго соответствующую выбранной категории. Если ничего не подходит — "Прочее".

Категории (используй ТОЛЬКО эти названия, точь-в-точь):
{format_category_list(EXPENSE_CATEGORIES)}

{BANK_ALIASES_PROMPT}

ТИП ОПЕРАЦИИ:
- Почти все чеки и оплаты — это РАСХОД (type = "{TYPE_EXPENSE}").
- Если на изображении явно виден чек ВОЗВРАТА/РЕФАНДА или зачисление денег на счёт
  от кого-то другого (не пополнение своего же счёта) — это ДОХОД
  (type = "{TYPE_INCOME}").
- ПОПОЛНЕНИЕ СВОЕГО ЖЕ СЧЁТА/КОШЕЛЬКА (например, пополнение Kaspi Gold с карты BCC
  того же человека) — это НЕ доход и НЕ расход, деньги просто перекладываются между
  своими счетами. В этом случае верни "transactions" ПУСТЫМ массивом, а в "reply"
  вежливо объясни, что это похоже на перевод между своими счетами и в бюджет не
  записывается.
- ПЕРЕВОД другому человеку или от другого человека — определяй по направлению:
  деньги УХОДЯТ от пользователя кому-то → РАСХОД, category "Финансовые расходы и
  переводы". Деньги ПРИХОДЯТ пользователю от кого-то → ДОХОД, category "Подарки и
  переводы (входящие)" (или "Возврат долга", если из подписи/суммы ясно, что это
  именно возврат долга).

Для Kaspi Red используй bank "Kaspi", source "Kaspi Red" и funds_type "Рассрочка".
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
    result = subprocess.run(
        [
            "pdftoppm",
            "-f",
            "1",
            "-singlefile",
            "-png",
            "-r",
            "150",
            str(pdf_path),
            str(rendered_prefix),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Не удалось преобразовать PDF")
    rendered_path = rendered_prefix.with_suffix(".png")
    if not rendered_path.exists():
        raise RuntimeError("PDF не содержит доступной страницы")
    return rendered_path


async def parse_receipt(
    file_bytes: bytes,
    filename: str,
    caption: str = "",
    user_name: str = "Пользователь",
) -> dict:
    """Распознать файл и вернуть JSON; при любом сбое вернуть пустой словарь."""
    if not file_bytes:
        print("[Распознавание] Получен пустой файл")
        return {}

    try:
        with tempfile.TemporaryDirectory(prefix="receipt_") as temporary_dir:
            source_path = Path(temporary_dir) / (Path(filename or "receipt").name or "receipt")
            source_path.write_bytes(file_bytes)
            suffix = source_path.suffix.lower()
            image_path = source_path
            mime_type = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
            }.get(suffix)
            if suffix == ".pdf":
                image_path = _render_pdf(source_path, Path(temporary_dir) / "receipt.png")
                mime_type = "image/png"
            if mime_type is None:
                print(f"[Распознавание] Неподдерживаемый формат: {suffix or 'без расширения'}")
                return {}

            encoded_image = base64.b64encode(image_path.read_bytes()).decode("ascii")
            client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=45.0, max_retries=2)
            prompt = (
                f"Файл от пользователя {user_name}. Подпись к файлу: {caption or 'нет'}.\n"
                "Распознай чек и верни JSON по заданной схеме."
            )
            response = await client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": VISION_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{encoded_image}"
                                },
                            },
                        ],
                    },
                ],
                response_format={"type": "json_object"},
                max_tokens=900,
            )
            return _parse_json(response.choices[0].message.content)
    except asyncio.TimeoutError:
        print("[Распознавание] OpenAI не ответил вовремя")
    except Exception as error:
        print(f"[Распознавание] Сбой API или формата файла: {error}")
    return {}
