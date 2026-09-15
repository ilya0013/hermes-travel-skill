#!/usr/bin/env python3
"""Строки прогона одним экраном: id, kind, источник, маршрут, даты, цена, статус, raw.
Замена heredoc-питону, которым Лиза читала журнал перед отчётом (14.09.2026) — на него
Hermes останавливается за разрешением, и прогон висит."""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from journal import JOURNAL  # noqa: E402
from verify_v3 import is_api_url, seller_page  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--journal", default=JOURNAL, help=f"файл журнала (по умолчанию {JOURNAL})")
    ap.add_argument("--run", help="id прогона; без него — последний в файле")
    ap.add_argument("--source", help="только этот source_id")
    args = ap.parse_args()

    rows, broken = [], 0
    with open(args.journal, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                broken += 1              # битую строку называет verify_v3 (E001); здесь — не падать
                continue
            if isinstance(row, dict) and row.get("id") and row.get("run"):
                rows.append(row)
    if not rows:
        sys.exit("журнал пуст")
    run = args.run or rows[-1]["run"]
    rows = [r for r in rows if r.get("run") == run
            and (not args.source or r.get("source_id") == args.source)]
    rows.sort(key=lambda r: (r.get("source_id") or "", r.get("kind") or "", r.get("dates") or "", r["id"]))
    for r in rows:
        value = r.get("value")
        shown = f"{value:.2f}" if isinstance(value, (int, float)) else "—"
        page = seller_page(r)                      # то же условие, что у E406
        tail = " ссылка" if page and not is_api_url(page) else ""
        print(f"{r['id']} {r.get('kind') or '':10} {r.get('source_id') or '':16} {r.get('route') or '':8} "
              f"{r.get('dates') or '':21} {shown:>8} {r.get('currency') or '':3} {r.get('status') or '':9} "
              f"{str(r.get('raw') or '')[:70]}{tail}")
    print(f"прогон {run}: {len(rows)} строк" + (f"; битых строк в файле: {broken}" if broken else ""))


if __name__ == "__main__":
    main()
