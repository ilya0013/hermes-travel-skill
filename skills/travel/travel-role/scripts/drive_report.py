#!/usr/bin/env python3
"""Полный отчёт → Google Doc в папке «Поездки» на Диске владельца; печатает ссылку.

    HERMES_HOME=/opt/data /opt/hermes/.venv/bin/python3 drive_report.py \\
        --journal /opt/data/travel/observations.jsonl \\
        --report /opt/data/travel/reports/RUNID.md \\
        --title "2026-09-13 WAW→BCN октябрь 3–4 ночи"

Текст документа — сообщение и приложение из файла отчёта без id, ссылки из журнала
(render(full=True) из report_render). Папка ищется по имени в корне Диска и создаётся,
если её нет; имя документа — `--title`, по нему папка и читается как указатель: дата,
маршрут, окно. Преобразование при загрузке — Drive API, guide «Upload file data»
(developers.google.com, прочитан 13.09.2026): «specify the Google Workspace mimeType when
creating the file», в таблице конвертаций Markdown → Docs. Первый живой документ 13.09 грузился
как text/plain — в нём остались сырые `##` и `**`; с text/markdown заголовки и жирный
становятся форматированием.
На Диск ходит токеном агента через build_service скилла google-workspace — потому
запускается питоном Hermes (`/opt/hermes/.venv/bin/python3`), а не venv источников.
Печатает JSON: {"doc_url": ..., "folder_url": ...}. Код возврата 1 — ссылка в отчёте
без строки журнала (документ не создаётся).
"""

import argparse
import io
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FOLDER_NAME = "Поездки"
GOOGLE_DOC = "application/vnd.google-apps.document"
GOOGLE_FOLDER = "application/vnd.google-apps.folder"


def workspace_scripts():
    home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    return home / "skills" / "productivity" / "google-workspace" / "scripts"


def find_or_create_folder(drive, name=FOLDER_NAME):
    q = (f"name = '{name}' and mimeType = '{GOOGLE_FOLDER}' "
         "and 'root' in parents and trashed = false")
    found = drive.files().list(q=q, fields="files(id, webViewLink)", pageSize=1).execute()
    if found.get("files"):
        return found["files"][0]
    return drive.files().create(
        body={"name": name, "mimeType": GOOGLE_FOLDER, "parents": ["root"]},
        fields="id, webViewLink",
    ).execute()


def create_doc(drive, folder_id, title, text):
    """Текст загружается как text/markdown с преобразованием в Google Doc — хватает права drive."""
    from googleapiclient.http import MediaIoBaseUpload

    media = MediaIoBaseUpload(io.BytesIO(text.encode("utf-8")), mimetype="text/markdown; charset=utf-8",
                              resumable=False)
    return drive.files().create(
        body={"name": title, "mimeType": GOOGLE_DOC, "parents": [folder_id]},
        media_body=media,
        fields="id, webViewLink",
    ).execute()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--title", required=True, help="имя документа: дата, маршрут, окно дат, ночи")
    args = ap.parse_args()

    sys.path.insert(0, str(HERE))
    sys.path.append(str(workspace_scripts()))   # в конец: скилл агента не должен затенять наши модули
    from report_render import render
    from verify_v3 import load_journal
    from google_api import build_service

    rows = load_journal(args.journal, [])
    by_id = {o.get("id"): o for _, o in rows if o.get("id")}
    with open(args.report, encoding="utf-8") as fh:
        text, missing = render(fh.read(), by_id, full=True)
    if missing:
        print("ссылка без строки журнала с url: " + ", ".join(missing), file=sys.stderr)
        return 1

    drive = build_service("drive", "v3")
    folder = find_or_create_folder(drive)
    doc = create_doc(drive, folder["id"], args.title, text)
    print(json.dumps({"doc_url": doc.get("webViewLink", ""), "folder_url": folder.get("webViewLink", "")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
