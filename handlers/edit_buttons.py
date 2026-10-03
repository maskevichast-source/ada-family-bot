"""«✏️ Изменить» под ответом о записи: правка записи кнопками, без переписки.

Поток: [Изменить] → (если в чеке несколько позиций — выбор позиции) → что менять
(банк / категория / сумма / магазин / комментарий / дата / кто / нужность) →
для банка, категории, «кто», нужности — выбор кнопкой; для суммы, магазина, комментария и даты —
бот просит написать значение следующим сообщением (есть кнопка «Отмена»).

Все данные берутся из таблицы по transaction_id, поэтому кнопки не зависят от номеров строк.
Сообщение «Изменила» уходит только после реальной записи в таблицу.
Токен общий с кнопкой «Отменить» (services/undo.py): отменённую запись править нельзя.
"""
import asyncio
import time

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from services import sheets, state, tx_edit, undo
from services.state import dialogue_key
from services.telegram_safe import safe_answer

PREFIX = undo.EDIT_PREFIX            # "edt:"
INPUT_NS = "edit_input"
INPUT_TTL_SECONDS = 300


def _cb(action: str, token: str, *rest) -> str:
    return PREFIX + ":".join([action, token, *[str(r) for r in rest]])


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _rows(buttons: list, per_row: int) -> list[list]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


# ── клавиатуры ────────────────────────────────────────────────────────────

def field_menu_kb(token: str, i: int, many: bool) -> InlineKeyboardMarkup:
    rows = _rows([_button(label, _cb("f", token, i, code)) for code, label in tx_edit.FIELDS], 2)
    last = [_button("✅ Готово", _cb("d", token, i))]
    if many:
        last.insert(0, _button("⬅️ К позициям", _cb("o", token)))
    rows.append(last)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def positions_kb(token: str, ids: list[str], records: dict) -> InlineKeyboardMarkup:
    rows = [[_button(tx_edit.position_label(records[tid]), _cb("p", token, i))]
            for i, tid in enumerate(ids) if tid in records]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def bank_kb(token: str, i: int) -> InlineKeyboardMarkup:
    buttons = [_button(label, _cb("v", token, i, "b", k)) for k, (label, *_rest) in enumerate(tx_edit.BANK_CHOICES)]
    rows = _rows(buttons, 2) + [[_button("⬅️ Назад", _cb("b", token, i))]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def category_kb(token: str, i: int, record: dict) -> InlineKeyboardMarkup:
    rows = [[_button(name, _cb("v", token, i, "c", k))] for k, name in enumerate(tx_edit.category_choices(record))]
    rows.append([_button("⬅️ Назад", _cb("b", token, i))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subcategory_kb(token: str, i: int, category: str) -> InlineKeyboardMarkup:
    rows = [[_button(name, _cb("s", token, i, k))] for k, name in enumerate(tx_edit.subcategory_choices(category))]
    rows.append([_button("✅ Оставить так", _cb("b", token, i))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def who_kb(token: str, i: int) -> InlineKeyboardMarkup:
    rows = [[_button(name, _cb("v", token, i, "u", k)) for k, name in enumerate(tx_edit.FAMILY_NAMES)],
            [_button("⬅️ Назад", _cb("b", token, i))]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def necessity_kb(token: str, i: int) -> InlineKeyboardMarkup:
    rows = [[_button(label, _cb("v", token, i, "w", k)) for k, (label, _code) in enumerate(tx_edit.NECESSITY_CHOICES)],
            [_button("⬅️ Назад", _cb("b", token, i))]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_input_kb(token: str, i: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_button("Отмена", _cb("x", token, i))]])


# ── вспомогательное ───────────────────────────────────────────────────────

async def _answer(callback, text: str | None = None, alert: bool = False) -> None:
    try:
        if text is None:
            await callback.answer()
        else:
            await callback.answer(text, show_alert=alert)
    except Exception:
        pass


async def _render(callback, text: str, kb) -> None:
    """Меняет текст и кнопки сообщения. «Не изменилось» и прочие сбои правки не критичны."""
    try:
        await callback.message.edit_text(text, parse_mode=None, reply_markup=kb)
    except Exception as error:
        if "not modified" not in str(error).lower():
            print(f"[Правка] Не удалось обновить сообщение: {error}")


def _prompt_text(field: str, record: dict) -> str:
    if field == "a":
        return f"Сумма сейчас: {tx_edit._money(record.get('amount'))} ₸.\nНапиши новую сумму, например 1750."
    if field == "d":
        return (f"Дата сейчас: {tx_edit._date_text(record.get('date'))}.\n"
                "Напиши новую: 06.09.2026 15:41, 06.09.2026 (время останется) или «вчера».")
    if field == "m":
        current = str(record.get("merchant") or "").strip() or "не указан"
        return f"Магазин сейчас: {current}.\nНапиши новое название или «-», чтобы очистить."
    current = str(record.get("user_comment") or "").strip() or "нет"
    return f"Комментарий сейчас: {current}.\nНапиши новый или «-», чтобы очистить."


async def _apply(token: str, ids: list[str], tid: str, changes: dict):
    """Пишет изменения в таблицу. Возвращает новую запись или None, если записи уже нет.
    Ошибки таблицы пробрасываются."""
    result = await asyncio.to_thread(sheets.update_transaction_fields, tid, changes)
    if result is None:
        return None
    _old, new = result
    if len(ids) == 1:
        undo.refresh_summary(token, undo.summarize([new]))
    return new


def _menu_text(record: dict, header: str) -> str:
    return f"{header}\n\n{tx_edit.describe(record)}"


# ── нажатия кнопок ────────────────────────────────────────────────────────

async def handle_edit_callback(callback):
    parts = (callback.data or "").split(":")
    if len(parts) < 3 or parts[0] + ":" != PREFIX:
        return
    action, token, args = parts[1], parts[2], parts[3:]

    entry = undo.get_entry(token)
    if not entry:
        await _answer(callback, "Кнопка устарела. Исправь запись через чат.", True)
        return
    if entry.get("status") != "active":
        await _answer(callback, "Эта запись уже отменена, менять нечего.", True)
        return
    ids = [str(x) for x in (entry.get("ids") or [])]
    many = len(ids) > 1

    try:
        records = await asyncio.to_thread(sheets.get_transactions_by_ids, ids)
    except Exception as error:
        print(f"[Правка] Таблица не ответила: {error}")
        await _answer(callback, "Таблица не ответила. Нажми ещё раз.", True)
        return
    if not records:
        await _answer(callback, "Этой записи уже нет в таблице.", True)
        return

    try:
        i = int(args[0]) if args else 0
    except ValueError:
        i = 0
    if not 0 <= i < len(ids) or ids[i] not in records:
        if action == "o" and many:
            i = 0
        else:
            await _answer(callback, "Не нашла эту позицию в таблице.", True)
            return
    tid = ids[i]
    record = records.get(tid, {})
    chat_user_key = dialogue_key(callback.message.chat.id, callback.from_user.id)

    if action == "o":                                   # открыть
        state.delete(INPUT_NS, chat_user_key)
        if many:
            await _render(callback, "Какую позицию изменить?", positions_kb(token, ids, records))
        else:
            await _render(callback, _menu_text(record, "✏️ Что изменить?"), field_menu_kb(token, 0, False))
    elif action in ("p", "b", "x"):                     # позиция выбрана / назад / отмена ввода
        state.delete(INPUT_NS, chat_user_key)
        await _render(callback, _menu_text(record, "✏️ Что изменить?"), field_menu_kb(token, i, many))
    elif action == "d":                                 # готово
        state.delete(INPUT_NS, chat_user_key)
        await _render(callback, f"✅ Записано:\n{tx_edit.describe(record)}", undo.keyboard_for_token(token))
    elif action == "f":                                 # выбрано поле
        field = args[1] if len(args) > 1 else ""
        if field == "b":
            await _render(callback, _menu_text(record, "🏦 Выбери банк или карту:"), bank_kb(token, i))
        elif field == "c":
            await _render(callback, _menu_text(record, "🗂 Выбери категорию:"), category_kb(token, i, record))
        elif field == "u":
            await _render(callback, _menu_text(record, "👤 Кто потратил?"), who_kb(token, i))
        elif field == "w":
            await _render(callback, _menu_text(record, "⚖️ Это было нужное или хотелка?"), necessity_kb(token, i))
        elif field in tx_edit.TEXT_FIELDS:
            state.put(INPUT_NS, chat_user_key, {
                "token": token, "i": i, "field": field, "ts": time.time(),
                "chat_id": callback.message.chat.id, "message_id": callback.message.message_id,
            })
            await _render(callback, _prompt_text(field, record), cancel_input_kb(token, i))
        else:
            await _answer(callback, "Неизвестное поле.", True)
            return
    elif action in ("v", "s"):                          # выбрано значение / подкатегория
        try:
            if action == "s":
                changes = tx_edit.subcategory_changes(record, int(args[1]))
                field = "s"
            else:
                field, index = args[1], int(args[2])
                if field == "b":
                    changes = tx_edit.bank_changes(record, index)
                elif field == "c":
                    changes = tx_edit.category_changes(record, index)
                elif field == "u":
                    changes = tx_edit.who_changes(index)
                elif field == "w":
                    changes = tx_edit.necessity_changes(index)
                else:
                    raise ValueError(field)
        except (ValueError, IndexError):
            await _answer(callback, "Не поняла выбор. Нажми «Изменить» заново.", True)
            return
        try:
            new = await _apply(token, ids, tid, changes)
        except Exception as error:
            print(f"[Правка] Не удалось записать: {error}")
            await _answer(callback, "Не получилось записать в таблицу. Нажми ещё раз.", True)
            return
        if new is None:
            await _answer(callback, "Этой записи уже нет в таблице.", True)
            return
        if field == "c" and tx_edit.subcategory_choices(str(new.get("category") or "")) and \
                not tx_edit.is_income_type(new.get("type")):
            await _render(callback, _menu_text(new, "✅ Категорию поменяла. Уточнить подкатегорию?"),
                          subcategory_kb(token, i, str(new.get("category"))))
        else:
            await _render(callback, _menu_text(new, "✅ Изменила. Что-нибудь ещё?"), field_menu_kb(token, i, many))
        await _answer(callback, "Готово")
        return
    await _answer(callback)


# ── ввод значения текстом ─────────────────────────────────────────────────

async def handle_edit_input(message, text: str) -> bool:
    """Если человек сейчас вводит значение для правки — забирает сообщение и возвращает True."""
    key = dialogue_key(message.chat.id, message.from_user.id)
    pending = state.get(INPUT_NS, key)
    if not pending:
        return False
    if time.time() - float(pending.get("ts") or 0) > INPUT_TTL_SECONDS:
        state.delete(INPUT_NS, key)
        return False

    token = str(pending.get("token"))
    entry = undo.get_entry(token)
    if not entry or entry.get("status") != "active":
        state.delete(INPUT_NS, key)
        return False
    ids = [str(x) for x in (entry.get("ids") or [])]
    i = int(pending.get("i") or 0)
    if not 0 <= i < len(ids):
        state.delete(INPUT_NS, key)
        return False
    tid = ids[i]

    try:
        records = await asyncio.to_thread(sheets.get_transactions_by_ids, [tid])
    except Exception as error:
        print(f"[Правка] Таблица не ответила: {error}")
        await safe_answer(message, "Таблица не ответила. Напиши значение ещё раз.")
        return True
    record = records.get(tid)
    if not record:
        state.delete(INPUT_NS, key)
        await safe_answer(message, "Этой записи уже нет в таблице.")
        return True

    changes, error_text = tx_edit.changes_for_text_field(str(pending.get("field")), text, record)
    if error_text:
        await safe_answer(message, f"{error_text} Или нажми «Отмена» под моим вопросом.")
        return True
    try:
        new = await _apply(token, ids, tid, changes)
    except Exception as error:
        print(f"[Правка] Не удалось записать: {error}")
        await safe_answer(message, "Не получилось записать в таблицу. Напиши значение ещё раз.")
        return True
    state.delete(INPUT_NS, key)
    if new is None:
        await safe_answer(message, "Этой записи уже нет в таблице.")
        return True

    result_text = _menu_text(new, "✅ Изменила. Что-нибудь ещё?")
    kb = field_menu_kb(token, i, len(ids) > 1)
    try:
        await message.bot.edit_message_text(
            chat_id=pending.get("chat_id"), message_id=pending.get("message_id"),
            text=result_text, parse_mode=None, reply_markup=kb)
    except Exception as error:
        print(f"[Правка] Не удалось обновить исходное сообщение, шлю новое: {error}")
        await safe_answer(message, result_text, reply_markup=kb)
    return True
