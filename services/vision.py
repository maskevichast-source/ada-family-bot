"""Распознавание чеков, фото и PDF через OpenAI Vision с гарантированной конвертацией в JPEG."""

import asyncio
import base64
import json
import logging
import subprocess
import tempfile
import io
from pathlib import Path
from PIL import Image

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


def to_jpeg_bytes(raw_bytes: bytes) -> bytes:
    """Гарантированно преобразует любое изображение (PNG, WebP, HEIC) в чистый JPEG."""
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            img = img.convert("RGB")
            # Сжимаем до разумного размера, чтобы запрос не весил 15 МБ
            img.thumbnail((1800, 1800))
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=88)
            return out.getvalue()
    except Exception as e:
        logging.warning(f"[Pillow Convert Warning]: {e}")
        return raw_bytes


def _render_pdf(file_bytes: bytes) -> list[bytes]:
    """Надёжный рендеринг PDF в список JPEG-страниц."""
    images = []
    with tempfile.TemporaryDirectory(prefix="ada_pdf_") as tmp_dir:
        pdf_path = Path(tmp_dir) / "document.pdf"
        pdf_path.write_bytes(file_bytes)

        # 1. Пробуем pdftoppm с прямым выводом в JPEG
        out_prefix = Path(tmp_dir) / "page"
        try:
            res = subprocess.run(
                ["pdftoppm", "-jpeg", "-r", "150", str(pdf_path), str(out_prefix)],
                capture_output=True, timeout=30, check=False
            )
            if res.returncode == 0:
                for img_file in sorted(Path(tmp_dir).glob("page-*.jpg")):
                    images.append(img_file.read_bytes())
                if images:
                    return images
        except Exception:
            pass

        # 2. Резервный способ через pypdfium2 + Pillow
        try:
            import pypdfium2 as pdfium
            pdf = pdfium.PdfDocument(str(pdf_path))
            for i in range(min(len(pdf), 5)):
                page = pdf[i]
                bitmap = page.render(scale=2)
                pil_image = bitmap.to_pil()
                out = io.BytesIO()
                pil_image.convert("RGB").save(out, format="JPEG", quality=88)
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

    # Определяем, PDF это или изображение
    if ext == ".pdf" or file_bytes[:5] == b"%PDF-":
        images_bytes = await asyncio.to_thread(_render_pdf, file_bytes)
        if not images_bytes:
            return {"transactions": [], "reply": "Не удалось прочитать страницы PDF. Попробуй сделать скриншот чека."}
    else:
        # Любой скриншот (PNG/WebP/JPG) принудительно нормализуем в чистый JPEG
        jpeg_data = await asyncio.to_thread(to_jpeg_bytes, file_bytes)
        images_bytes = [jpeg_data]

    client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=45.0, max_retries=1)

    try:
        from services.timezone import now_astana
        now = now_astana()
        time_hint = get_time_context_hint(now.hour, now.weekday())

        content = [
            {
                "type": "text",
                "text": f"Чек от {user_name}. Подпись к чеку: «{caption or 'нет'}».\n"
                        f"Текущее время: {now.strftime('%Y-%m-%d %H:%M:%S (%A)')}.\n"
                        f"Контекст времени: {time_hint}.\n"
                        f"Распознай все покупки и суммы чека."
            }
        ]

        for img in images_bytes[:5]:
            b64 = base64.b64encode(img).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}
            })

        # Пробуем gpt-4o, если модель недоступна — пробуем gpt-4o-mini
        response = None
        for model_name in ["gpt-4o", "gpt-4o-mini"]:
            try:
                response = await client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": VISION_SYSTEM_PROMPT},
                        {"role": "user", "content": content}
                    ],
                    response_format={"type": "json_object"},
                    max_tokens=4096
                )
                if response:
                    break
            except Exception as model_err:
                logging.warning(f"[Model {model_name} failed]: {model_err}")
                if model_name == "gpt-4o-mini":
                    raise model_err

        result = _parse_json(response.choices[0].message.content)
        txs = result.get("transactions", [])

        # Проверка неоднозначности товаров
        for tx in txs:
            comm = str(tx.get("user_comment", "") or caption)
            is_ambig, alts = is_ambiguous_item(comm)
            if is_ambig and tx.get("confidence", 1.0) >= 0.9:
                tx["confidence"] = 0.6
                tx["alternatives"] = alts

        return result

    except Exception as e:
        err_msg = str(e)
        logging.exception(f"[Vision Fatal Error]: {err_msg}")
        
        # Показываем реальную причину ошибки, если проблема в аккаунте OpenAI
        if "quota" in err_msg.lower() or "billing" in err_msg.lower():
            reply_text = "⚠️ В OpenAI закончились средства на балансе (Quota Exceeded). Пополните баланс на platform.openai.com."
        elif "api_key" in err_msg.lower() or "auth" in err_msg.lower():
            reply_text = "⚠️ Ошибка авторизации OpenAI: неверный ключ OPENAI_API_KEY."
        else:
            reply_text = f"Не удалось разобрать чек ({type(e).__name__}: {err_msg[:90]})."

        return {"transactions": [], "reply": reply_text}

    finally:
        await client.close()
