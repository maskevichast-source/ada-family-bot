"""Нажатие кнопки «↩️ Отменить» — удаляет записанную трату из таблицы."""
import asyncio

from aiogram import types

from services import undo


async def _finish_message(callback: types.CallbackQuery, text: str) -> None:
    """Заменяет текст ответа на «Отменено…» и убирает кнопку. Сбой правки не критичен."""
    try:
        await callback.message.edit_text(text, parse_mode=None, reply_markup=None)
        return
    except Exception:
        pass
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


async def handle_undo_callback(callback: types.CallbackQuery):
    data = callback.data or ""
    if not data.startswith(undo.CALLBACK_PREFIX):
        return
    token = data[len(undo.CALLBACK_PREFIX):]

    try:
        result = await asyncio.to_thread(undo.perform_undo, token)
    except Exception as error:
        print(f"[Undo] Не удалось удалить: {error}")
        await callback.answer("Не получилось удалить — таблица не ответила. Нажми ещё раз.", show_alert=True)
        return

    status = result.get("status")
    summary = result.get("summary") or ""
    if status == "done":
        await _finish_message(callback, f"↩️ Отменено: {summary}".strip())
        await callback.answer("Удалила из таблицы")
    elif status == "already":
        await _finish_message(callback, f"↩️ Отменено: {summary}".strip())
        await callback.answer("Уже отменено")
    elif status == "not_found":
        await _finish_message(callback, f"↩️ Этой записи уже нет в таблице: {summary}".strip())
        await callback.answer("В таблице этой записи уже нет")
    else:
        await callback.answer("Отмена устарела. Удали через чат: «удали последнюю трату».", show_alert=True)
