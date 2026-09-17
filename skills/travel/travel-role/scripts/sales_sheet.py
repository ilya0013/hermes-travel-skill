#!/usr/bin/env python3
"""Наводки лент и рассылок → Google Sheet «Распродажи» в папке «Поездки» на Диске владельца.

    echo '[{"block": "заметное", "dates": "2026-09-16", "source_id": "f4f_qatar_feed", "matched": ["qatar"],
           "value": 2633, "currency": "PLN", "raw": "…", "url": "https://…"}]' \\
      | HERMES_HOME=/opt/data /opt/hermes/.venv/bin/python3 sales_sheet.py

Строки — JSON-список строк журнала (`kind=lead`) с полем `block` («интерес <имя>», «заметное», «флэш»)
на stdin; зовёт `deal_feeds.py` в режиме интереса. Таблица ищется по имени в папке «Поездки» (папка — как
у `drive_report.py`), нет — создаётся с шапкой; строки дописываются в конец листа «Наводки». Витрина, не
первичная запись: журнал `observations.jsonl` и копилка `pending.md` живут без неё. Ходит токеном агента
через `build_service` скилла google-workspace — потому питон Hermes, не venv источников. Печатает JSON
`{"url": …, "appended": N}`; любая ошибка — код 1 и текст в stderr (deal_feeds её только докладывает).
Колонки «продажа до» и «полёт когда» — под письма перевозчиков (следующий кусок), у лент пусты.
"""

import datetime
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHEET_NAME = "Распродажи"
TAB = "Наводки"
GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"
HEADER = ["найдено", "опубликовано", "блок", "источник", "слово", "цена", "валюта", "заголовок", "ссылка",
          "продажа до", "полёт когда"]


def to_values(rows, today):
    """Строки таблицы из строк журнала: порядок — HEADER."""
    out = []
    for r in rows:
        words = r.get("matched") or []
        out.append([today, r.get("dates") or "", r.get("block") or "", str(r.get("source_id") or "").removesuffix("_feed"),
                    ", ".join(str(w) for w in words), r.get("value"), r.get("currency") or "", r.get("raw") or "",
                    r.get("url") or "", "", ""])
    return out


def find_sheet(drive, folder_id):
    q = (f"name = '{SHEET_NAME}' and mimeType = '{GOOGLE_SHEET}' and '{folder_id}' in parents and trashed = false")
    found = drive.files().list(q=q, fields="files(id, webViewLink)", pageSize=1).execute().get("files")
    return found[0] if found else None


def create_sheet(drive, sheets, folder_id):
    """Таблица с листом «Наводки» и шапкой, перенесена из корня Диска в папку."""
    created = sheets.spreadsheets().create(
        body={"properties": {"title": SHEET_NAME}, "sheets": [{"properties": {"title": TAB}}]},
        fields="spreadsheetId,spreadsheetUrl").execute()
    sheet_id = created["spreadsheetId"]
    drive.files().update(fileId=sheet_id, addParents=folder_id, removeParents="root", fields="id").execute()
    sheets.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"{TAB}!A1", valueInputOption="RAW", body={"values": [HEADER]}).execute()
    return {"id": sheet_id, "webViewLink": created.get("spreadsheetUrl")
            or f"https://docs.google.com/spreadsheets/d/{sheet_id}"}


def append_rows(sheets, sheet_id, values):
    if not values:
        return 0
    sheets.spreadsheets().values().append(
        spreadsheetId=sheet_id, range=f"{TAB}!A:K", valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS", body={"values": values}).execute()
    return len(values)


def main():
    sys.path.insert(0, str(HERE))
    sys.path.append(str(Path(os.environ.get("HERMES_HOME", "/opt/data")) / "skills" / "productivity"
                        / "google-workspace" / "scripts"))   # в конец: скилл агента не должен затенять наши модули
    from drive_report import find_or_create_folder
    from google_api import build_service

    rows = json.load(sys.stdin)
    if not isinstance(rows, list):
        raise SystemExit("на stdin нужен JSON-список строк")
    drive, sheets = build_service("drive", "v3"), build_service("sheets", "v4")
    folder = find_or_create_folder(drive)
    sheet = find_sheet(drive, folder["id"]) or create_sheet(drive, sheets, folder["id"])
    n = append_rows(sheets, sheet["id"], to_values(rows, datetime.date.today().isoformat()))
    print(json.dumps({"url": sheet.get("webViewLink") or f"https://docs.google.com/spreadsheets/d/{sheet['id']}",
                      "appended": n}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
