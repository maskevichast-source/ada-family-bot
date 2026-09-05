"""Additive offline migration: does not change any existing populated cell.
Usage: python scripts/prepare_workbook.py SOURCE.xlsx DESTINATION.xlsx
Do not replace a newer live Google Sheet with this snapshot.
"""
import argparse
import datetime
from pathlib import Path
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

REMINDER_HEADERS = ["reminder_id","created_at","target_user","remind_at","text","status","recurrence",
                    "created_by","anchor_day","deliveries"]
DEBT_HEADERS = ["event_id","debt_id","date","owner","counterparty","direction",
                "event_type","amount","currency","due_date","note"]

def prepare(source, destination):
    if Path(source).resolve() == Path(destination).resolve():
        raise ValueError("Нужен другой выходной файл — оригинал не перезаписываем")
    book = openpyxl.load_workbook(source)
    ws = book["Reminders"]
    if [ws.cell(1,col).value for col in range(1,8)] != REMINDER_HEADERS[:7]:
        raise ValueError("Неожиданная схема Reminders")
    for col, header in enumerate(REMINDER_HEADERS[7:],8):
        if ws.cell(1,col).value not in (None,header):
            raise ValueError("Заняты расширяемые колонки")
        ws.cell(1,col,header)
    for row in range(2,ws.max_row+1):
        if not ws.cell(row,1).value:
            continue
        when=ws.cell(row,4).value
        if not isinstance(when,datetime.datetime):
            try: when=datetime.datetime.fromisoformat(str(when))
            except ValueError: when=None
        if when and ws.cell(row,9).value is None:
            ws.cell(row,9,when.day)
        if ws.cell(row,10).value is None:
            ws.cell(row,10,"{}")
    trips = book["Trips"]
    if trips.cell(1, 5).value not in (None, "notes"):
        raise ValueError("Колонка E Trips уже занята")
    trips.cell(1, 5, "notes")
    subscriptions = book["Subscriptions"]
    if subscriptions.cell(1, 8).value not in (None, "last_warning"):
        raise ValueError("Колонка H Subscriptions занята")
    subscriptions.cell(1, 8, "last_warning")
    if "Debts" not in book.sheetnames:
        book.create_sheet("Debts").append(DEBT_HEADERS)
    elif [c.value for c in book["Debts"][1]] != DEBT_HEADERS:
        raise ValueError("Неожиданная схема Debts")
    for sheet in (ws,book["Debts"]):
        sheet.freeze_panes="A2"
        sheet.auto_filter.ref=sheet.dimensions
        for cell in sheet[1]:
            cell.font=Font(name="Calibri",bold=True,color="FFFFFF")
            cell.fill=PatternFill("solid",fgColor="18344A")
            cell.alignment=Alignment(vertical="center",wrap_text=True)
        sheet.row_dimensions[1].height=30
    for col in ("A","B","C","D","E","F","G","H","I","J","K"):
        book["Debts"].column_dimensions[col].width=22
    book["Debts"].column_dimensions["K"].width=48
    ws.column_dimensions["H"].width=18
    ws.column_dimensions["I"].width=14
    ws.column_dimensions["J"].width=35
    book.save(destination)
    # Verify all old nonempty cell values, including 106 historical transactions.
    original=openpyxl.load_workbook(source)
    migrated=openpyxl.load_workbook(destination)
    for sheet in original:
        for row in sheet:
            for cell in row:
                if cell.value not in (None, ""):
                    assert migrated[sheet.title][cell.coordinate].value == cell.value, (sheet.title,cell.coordinate)
    return destination

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("destination")
    args=parser.parse_args()
    print(prepare(args.source,args.destination))
