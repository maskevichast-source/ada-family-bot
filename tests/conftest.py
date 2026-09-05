"""Offline tests. Minimal third-party import shims only if packages are unavailable.
No Telegram/Google/LLM request is made. Not a substitute for live acceptance tests.
"""
import importlib.util
import sys
import types
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if importlib.util.find_spec("gspread") is None:
    g = types.ModuleType("gspread")
    class WorksheetNotFound(Exception): pass
    g.WorksheetNotFound = WorksheetNotFound
    u = types.ModuleType("gspread.utils")
    def rowcol_to_a1(row, col):
        result = ""
        while col:
            col, rem = divmod(col-1, 26)
            result = chr(65+rem) + result
        return f"{result}{row}"
    u.rowcol_to_a1 = rowcol_to_a1
    g.utils = u
    sys.modules.update({"gspread": g, "gspread.utils": u})
if importlib.util.find_spec("oauth2client") is None:
    o = types.ModuleType("oauth2client")
    sa = types.ModuleType("oauth2client.service_account")
    sa.ServiceAccountCredentials = type("Credentials", (), {})
    sys.modules.update({"oauth2client": o, "oauth2client.service_account": sa})
if importlib.util.find_spec("aiogram") is None:
    a = types.ModuleType("aiogram")
    exceptions = types.ModuleType("aiogram.exceptions")
    class TelegramBadRequest(Exception):
        def __init__(self, method=None, message=""):
            super().__init__(message)
    exceptions.TelegramBadRequest = TelegramBadRequest
    a.exceptions = exceptions
    sys.modules.update({"aiogram": a, "aiogram.exceptions": exceptions})

class Sheet:
    def __init__(self, title, rows=None, cols=16):
        self.title, self.data, self.col_count = title, [list(r) for r in (rows or [])], cols
        self.append_calls = 0
    def get_all_values(self):
        return [list(r) for r in self.data]
    def row_values(self, n):
        return list(self.data[n-1]) if n <= len(self.data) else []
    def append_row(self, row, **kwargs):
        self.data.append(list(row)); self.append_calls += 1
    def append_rows(self, rows, **kwargs):
        for row in rows: self.append_row(row, **kwargs)
    def batch_update(self, updates, **kwargs):
        for update in updates: self.update(range_name=update["range"], values=update["values"])
    def resize(self, cols=None, **kwargs):
        if cols: self.col_count = cols
    def update_cell(self, row, col, value):
        while len(self.data) < row: self.data.append([])
        while len(self.data[row-1]) < col: self.data[row-1].append("")
        self.data[row-1][col-1] = value
    def update(self, range_name=None, values=None, **kwargs):
        import re
        match = re.match(r"([A-Z]+)(\d+)", range_name)
        col = 0
        for c in match[1]: col = col*26 + ord(c)-64
        row = int(match[2])
        for dy, line in enumerate(values):
            for dx, value in enumerate(line):
                self.update_cell(row+dy, col+dx, value)
    def delete_rows(self, idx):
        del self.data[idx-1]
    def clear(self):
        self.data.clear()

class Database:
    def __init__(self): self.sheets = {}
    def worksheet(self, name):
        import gspread
        if name not in self.sheets: raise gspread.WorksheetNotFound(name)
        return self.sheets[name]
    def add_worksheet(self, title, rows=100, cols=16):
        if title in self.sheets: raise RuntimeError("duplicate sheet")
        sheet = Sheet(title, cols=cols)
        self.sheets[title] = sheet
        return sheet

@pytest.fixture
def db(monkeypatch):
    from services import sheets
    from services.preflight import TRANSACTION_HEADERS
    database = Database()
    database.sheets["Transactions"] = Sheet("Transactions", [TRANSACTION_HEADERS])
    monkeypatch.setattr(sheets, "get_db", lambda: database)
    return database

@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("ADA_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SHEETS_READ_CACHE_SECONDS", "0")
    import services.memory as memory
    monkeypatch.setattr(memory, "CHAT_HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr(memory, "_loaded", False)
    monkeypatch.setattr(memory, "_chat_history", __import__("collections").defaultdict(list))


# Optional import shims for handler orchestration tests; no behavioral API simulation.
if "aiogram" in sys.modules and not hasattr(sys.modules["aiogram"], "__path__"):
    a = sys.modules["aiogram"]
    class Value:
        def __init__(self, *args, **kwargs): self.__dict__.update(kwargs)
    at = types.ModuleType("aiogram.types")
    for name in ("Message", "InlineKeyboardMarkup", "InlineKeyboardButton", "BufferedInputFile", "CallbackQuery"):
        setattr(at, name, type(name, (Value,), {}))
    a.types = at
    class MagicFilter:
        def __getattr__(self,name): return self
        def startswith(self,*args): return self
    a.F = MagicFilter()
    a.BaseMiddleware = type("BaseMiddleware", (), {})
    class Observer:
        def __call__(self,*args,**kwargs):
            return lambda fn: fn
        def middleware(self,*args): pass
        def outer_middleware(self,*args): pass
    class Dispatcher:
        def __init__(self): self.message=Observer(); self.callback_query=Observer()
    a.Dispatcher=Dispatcher
    a.Bot=type("Bot", (Value,), {})
    filters=types.ModuleType("aiogram.filters")
    filters.Command=Value
    sys.modules.update({"aiogram.types":at, "aiogram.filters":filters})
if importlib.util.find_spec("openai") is None:
    o=types.ModuleType("openai")
    o.AsyncOpenAI=type("AsyncOpenAI", (), {"__init__":lambda self,*args,**kwargs:None})
    sys.modules["openai"]=o
if importlib.util.find_spec("curl_cffi") is None:
    c=types.ModuleType("curl_cffi")
    requests=types.ModuleType("curl_cffi.requests")
    requests.AsyncSession=type("AsyncSession", (), {})
    c.requests=requests
    sys.modules.update({"curl_cffi":c, "curl_cffi.requests":requests})
