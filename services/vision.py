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
    EXPENSE_CATEGORIES, TYPE_EXPENSE, TYPE_INCOME,
    format_category_list, SUBCATEGORIES_MAP, get_time_context_hint,
    is_ambiguous_item,
)
from services.banks import BANK_ALIASES_PROMPT

_SUBCATEGORIES_VISION_PROMPT = "\n".join(
    f"- {cat}: {', '.join(subs)}" for cat, subs in SUBCATEGORIES_MAP.items()
)

VISION_SYSTEM_PROMPT = f"""
Ты — Ада, помощница семейного финансового чата Влада и Дианы.
Проанализируй изображение чека, банковского перевода или оплаты.

КОНТЕКСТ ВРЕМЕНИ:
Текущее время и день недели даны отдельным сообщением. Используй их:
- Утро (6-10): дорога на работу → транспорт, энергетики
- Обед (12-15): еда вне дома → кафе, доставка
- Вечер (18-21): дорога домой, ужин → транспорт, продукты домой
- Ночь (21+): доставка, вредные привычки
- Будни: работа, транспорт
- Выходные: кафе, развлечения, продукты домой

Верни строгий JSON:
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
      "subcategory": "Напитки и вода",
      "merchant": "Название магазина",
      "necessity": "Need",
      "user_comment": "Описание покупки",
      "ai_comment": "Короткий комментарий Ады",
      "confidence": 1.0,
      "alternatives": []
    }}
  ]
}}

Если несколько покупок — верни несколько элементов. Если нет данных — transactions пустой.

Категории (ТОЛЬКО эти названия):
{format_category_list(EXPENSE_CATEGORIES)}

СТРОГИЕ ПОДКАТЕГОРИИ:
{_SUBCATEGORIES_VISION_PROMPT}

ПОДКАТЕГОРИИ:
- "subcategory" ОБЯЗАН быть из списка выше. НЕ придумывай.
- Если не уверен — бери ПЕРВУЮ подкатегорию категории.

necessity:
- Need: еда домой, вода, транспорт на работу, лекарства, ЖКХ, корм питомцу
- Want: сигареты, энергетики, алкоголь, косметика, кафе, рестораны, доставка, развлечения
- "Алкоголь, табак и энергетики" → ВСЕГДА Want
- "Красота и уход" → Want
- "Развлечения и хобби" → Want
- Кафе, рестораны, доставка → Want

{BANK_ALIASES_PROMPT}

ТИП ОПЕРАЦИИ:
- Почти все чеки — РАСХОД (type = "{TYPE_EXPENSE}")
- Возврат/рефанд или зачисление от другого → ДОХОД (type = "{TYPE_INCOME}")
- Пополнение СВОЕГО счёта → НЕ доход и НЕ расход, верни transactions пустым
- Перевод физлицу: если указан товар ("беляш") → классифицируй по товару
- Энергетики → ВСЕГДА "Алкоголь, табак и энергетики"

НЕОДНОЗНАЧНЫЕ ТОВАРЫ:
- Самса, пицца, суши, роллы, бургеры, шаурма → могут быть "Еда и продукты" (домой) ИЛИ "Кафе" (в заведении)
- Если на чеке нет явного указания "на вынос"/"в зале" И время 12:00-15:00 → скорее "Кафе"
- Если время 18:00+ → скорее "Еда и продукты" (домой)
- Если НЕ уверен — confidence < 1.0 и alternatives

ТВОЙ ТОН:
- Умеренный, живой, с лёгкой иронией
- НЕ грубый, НЕ "ахуевший"
- Подкалывай мягко, семья доверяет тебе деньги
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


async def parse_receipt(file_bytes: bytes, filename: str, caption: str = "",
                        user_name: str = "Пользователь") -> dict:
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

            # Добавляем контекст времени
            from services.timezone import now_astana
            now = now_astana()
            time_hint = get_time_context_hint(now.hour, now.weekday())

            prompt = (
                f"Файл от {user_name}. Подпись: {caption or 'нет'}.\n"
                f"Текущее время: {now.strftime('%Y-%m-%d %H:%M:%S (%A)')}.\n"
                f"Контекст: {time_hint}.\n"
                "Распознай чек и верни JSON."
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
            result = _parse_json(response.choices[0].message.content)

            # Пост-обработка: проверяем неоднозначность
            transactions = result.get("transactions", [])
            for tx in transactions:
                user_comment = str(tx.get("user_comment", "") or caption)
                is_ambig, alternatives = is_ambiguous_item(user_comment)
                if is_ambig and tx.get("confidence", 1.0) >= 0.9:
                    tx["confidence"] = 0.6
                    tx["alternatives"] = alternatives

            return result
    except asyncio.TimeoutError:
        print("[Распознавание] OpenAI не ответил вовремя")
    except Exception as error:
        print(f"[Распознавание] Сбой API или формата файла: {error}")
    return {}
