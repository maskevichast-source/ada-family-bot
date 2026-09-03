import datetime

ASTANA_TZ = datetime.timezone(datetime.timedelta(hours=5), name="Asia/Astana")

def now_astana():
    return datetime.datetime.now(ASTANA_TZ)


def parse_flexible_datetime(value):
    """Устойчиво разобрать время напоминания.

    Бот сам всегда пишет время строкой "YYYY-MM-DD HH:MM:SS", но если
    ячейку когда-либо отредактировали вручную через интерфейс Google
    Таблиц, Sheets мог отформатировать её как настоящую дату — тогда
    gspread возвращает не строку, а объект datetime (или строку в
    другом формате, например "29.08.2026 21:00:00"). Раньше жёсткий
    strptime() на такое падал, и напоминание тихо никогда не срабатывало.

    Возвращает datetime с таймзоной Астаны, либо None, если разобрать
    не удалось (вызывающий код должен явно это залогировать, а не
    проглотить молча).
    """
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=ASTANA_TZ)

    text = str(value).strip()
    if not text:
        return None

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.datetime.strptime(text, fmt).replace(tzinfo=ASTANA_TZ)
        except ValueError:
            continue
    return None


