"""Распознавание чеков, фото и PDF через OpenAI Vision."""

import asyncio
import base64
import json
import logging
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
Проанализируй изображение чека, банковского перевода или оплаты.

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

Категории:
{format_category_list(EXPENSE_CATEGORIES)}

Подкатегории:
{_SUBCATEGORIES_VISION_PROMPT}

{BANK_ALIASES_PROMPT}

ВАЖНО:
- Если на чеке несколько товаров — верни их отдельными элементами в transactions.
- Если это возврат — type: "{TYPE_INCOME}".
- Оплата услуг (OpenAI, интернет и т.д.) — это РАСХОД.
- Если валюта USD/EUR — укажи реальную валюту в currency.
"""


def _parse_json(content: str) -> dict:
    if not isinstance(content, str):
        return {}
    raw = content.strip()
    if raw.startswith("```"):
        raw = raw.removeprefix("```json").removeprefix("```").strip()
        raw = raw.removesuffix("```").strip()
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _render_pdf(file_bytes: bytes) -> list[bytes]:
    """Надёжный рендеринг PDF: сначала pdftoppm, затем pypdfium2."""
    images = []
    with tempfile.TemporaryDirectory(prefix="ada_pdf_") as tmp_dir:
        pdf_path = Path(tmp_dir) / "document.pdf"
        pdf_path.write_bytes(file_bytes)

        # 1. Пробуем через pdftoppm (быстро и стабильно на Linux)
        out_prefix = Path(tmp_dir) / "page"
        try:
            res = subprocess.run(
                ["pdftoppm", "-png", "-r", "150", str(pdf_path), str(out_prefix)],
                capture_output=True, timeout=30, check=False
            )
            if res.returncode == 0:
                for img_file in sorted(Path(tmp_dir).glob("page-*.png")):
                    images.append(img_file.read_bytes())
                if images:
                    return images
        except Exception:
            pass

        # 2. Резервный способ через pypdfium2
        try:
            import pypdfium2 as pdfium
            pdf = pdfium.PdfDocument(str(pdf_path))
            for i in range(min(len(pdf), 10)):
                page = pdf[i]
                bitmap = page.render(scale=2)
                pil_image = bitmap.to_pil()
                import io
                out = io.BytesIO()
                pil_image.save(out, format="JPEG", quality=90)
                images.append(out.getvalue())
            return images
        except Exception as e:
            logging.error(f"[PDF Render Error]: {e}")

    return images


async def parse_receipt(file_bytes: bytes, filename: str = "", caption: str = "", user_name: str = "Пользователь"):
    if not file_bytes:
        return {"transactions": [], "reply": "Файл пустой."}

    ext = Path(filename or "file.jpg").suffix.lower()
    images_bytes = []

    if ext == ".pdf" or file_bytes[:5] == b"%PDF-":
        images_bytes = await asyncio.to_thread(_render_pdf, file_bytes)
        if not images_bytes:
            return {"transactions": [], "reply": "Не удалось прочитать PDF. Попробуй сделать скриншот чека."}
    else:
        images_bytes = [file_bytes]

    client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=45.0, max_retries=1)

    try:
        from services.timezone import now_astana
        now = now_astana()
        time_hint = get_time_context_hint(now.hour, now.weekday())

        content = [
            {
                "type": "text",
                "text": f"Чек от {user_name}. Подпись: {caption or 'нет'}.\n"
                        f"Время: {now.strftime('%Y-%m-%d %H:%M:%S (%A)')}.\n"
                        f"Контекст: {time_hint}.\nРаспознай все операции."
            }
        ]

        for img in images_bytes[:5]:  # до 5 страниц
            b64 = base64.b64encode(img).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}
            })

        # max_tokens=4096 (НЕ 12000, иначе OpenAI выдает 400 ошибку!)
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": VISION_SYSTEM_PROMPT},
                {"role": "user", "content": content}
            ],
            response_format={"type": "json_object"},
            max_tokens=4096
        )

        result = _parse_json(response.choices[0].message.content)
        txs = result.get("transactions", [])

        # Проверка неоднозначности
        for tx in txs:
            comm = str(tx.get("user_comment", "") or caption)
            is_ambig, alts = is_ambiguous_item(comm)
            if is_ambig and tx.get("confidence", 1.0) >= 0.9:
                tx["confidence"] = 0.6
                tx["alternatives"] = alts

        return result

    except Exception as e:
        logging.exception(f"[Vision Error]: {e}")
        return {"transactions": [], "reply": "Не удалось разобрать чек. Попробуй сделать фото чётче или введи текстом."}
    finally:
        await client.close()
