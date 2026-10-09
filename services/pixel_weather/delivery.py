"""Отправка набора в Telegram: GIF «На улице», альбом из 6 страниц и карточка Dispatch с кнопкой «Перемешать»."""
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto


def mix_keyboard(kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎲 Перемешать", callback_data=f"wxmix:{kind}")]])


async def deliver(pixel, kind: str, anim, group, photo) -> bool:
    """anim(file, caption), group(media_list), photo(file, keyboard). True — главное (GIF) ушло; картинка — дополнение.
    Одна страница уходит обычным фото, несколько — альбомом. Карточка Dispatch отправляется, только если она есть."""
    try:
        await anim(BufferedInputFile(pixel.gif, filename="ada_weather.gif"), pixel.caption)
    except Exception as error:
        print(f"[Погода] Не удалось отправить анимацию: {error}")
        return False
    try:
        if len(pixel.pages) == 1:
            await photo(BufferedInputFile(pixel.pages[0], filename=f"{pixel.names[0]}.png"), None)
        else:
            await group([InputMediaPhoto(media=BufferedInputFile(png, filename=f"{name}.png"))
                         for png, name in zip(pixel.pages, pixel.names)])
    except Exception as error:
        print(f"[Погода] Страницы не отправились: {error}")
    if pixel.dispatch:
        try:
            await photo(BufferedInputFile(pixel.dispatch, filename="dispatch.png"), mix_keyboard(kind))
        except Exception as error:
            print(f"[Погода] Dispatch не отправился: {error}")
    return True
