#!/usr/bin/env python3
"""Текст для человека из проверенного файла отчёта: без id, ссылки — из журнала.

    python3 report_render.py --journal observations.jsonl --report reports/RUNID.md [--details-url URL]

Печатает сообщение — всё до строки «--- приложение ---»; `--details-url` дописывает
последней строкой «Подробно → URL» (документ на Диске). Ссылки на id
`[20260913T090644-e6q5]` убираются, `[ссылка <id>]` заменяется на страницу продавца из строки
журнала — link (форма покупки Wizz у wizzair_api, где url — эндпоинт) или url.
Текст документа целиком (сообщение и приложение) берёт drive_report.py через render(full=True).
Роль отправляет вывод как есть — после «ОШИБОК НЕТ» от verify_v3 по тому же файлу.
Код возврата: 0 — напечатано, 1 — ссылка не нашла строку с link/url (текст не печатается).
"""

import argparse
import re
import sys

from verify_v3 import LINK_RE, load_journal, seller_page, split_message

ID_TAG_RE = re.compile(r"[ \t]*\[\d{8}T\d{6}-[0-9a-z]{4}\]")


def render(text, by_id, full=False, details_url=None):
    """(текст, список id ссылок без url)."""
    message, appendix = split_message(text)
    parts = [message, appendix] if full else [message]
    out = "\n\n".join(p.strip("\n") for p in parts if p.strip())
    missing = []

    def link(m):
        page = seller_page(by_id.get(m.group(1)))
        if not page:
            missing.append(m.group(1))
            return m.group(0)
        return page

    out = ID_TAG_RE.sub("", LINK_RE.sub(link, out))
    if details_url:
        out += f"\nПодробно → {details_url}"
    return out, missing


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--details-url", help="ссылка на документ с подробностями, последней строкой")
    args = ap.parse_args()

    rows = load_journal(args.journal, [])
    by_id = {o.get("id"): o for _, o in rows if o.get("id")}
    with open(args.report, encoding="utf-8") as fh:
        text = fh.read()

    out, missing = render(text, by_id, details_url=args.details_url)
    if missing:
        print("ссылка без строки журнала с link/url: " + ", ".join(missing), file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
