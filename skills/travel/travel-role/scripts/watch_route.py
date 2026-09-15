#!/usr/bin/env python3
"""Дозор по заданию владельца: один тик cron Hermes в режиме --monitor-script.

    /opt/data/travel/lib/venv/bin/python watch_route.py /opt/data/travel/watch/<имя>.json

Файл задания:
    {"name": "pqc_feb2027", "top": 3, "threshold": 4000,
     "args": {"flyFrom": "WAW", "flyTo": "PQC", "departureDate": "01/02/2027",
              "departureDateTo": "28/02/2027", "nights_in_dst_from": 10, "nights_in_dst_to": 16,
              "adults": 1, "max_sector_stopovers": 2, "select_airlines": "AY,KL,LO"}}

stdout — стабильный: без времени, id и ссылок (Hermes сравнивает байты; изменился —
зовёт модель с диффом, не изменился — модель не вызывается). Рядом с заданием пишутся
<имя>.jsonl — журнал дозора (строки с id, run = тик) и <имя>.latest.md — тот же список
с id и bookingUrl: его содержимое роль и отправляет владельцу, когда цена того стоит.
Ошибка источника — код возврата 1, вывод не меняется, модель не вызывается.
"""

import asyncio
import json
import os
import sys

import journal
import kiwi_search


def render_stable(cfg, args, rows):
    """Строки stdout: только то, что не меняется от тика к тику при той же выдаче."""
    lines = [f"дозор {cfg['name']}: {json.dumps(args, ensure_ascii=False, sort_keys=True)}"]
    threshold = cfg.get("threshold")
    best = rows[0]["value"] if rows else None
    if threshold is not None:
        state = "выдачи нет" if best is None else ("достигнут" if best <= threshold else "не достигнут")
        lines.append(f"порог {threshold:g} {rows[0]['currency'] if rows else ''}: {state}".rstrip())
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['value']:.0f} {r['currency']}  {r['dates']}  {r['raw']}")
    if not rows:
        lines.append("Kiwi: выдача пуста")
    return "\n".join(lines) + "\n"


def render_latest(cfg, rows, tick):
    lines = [f"# дозор {cfg['name']} — снято {tick}", ""]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['value']:.0f} {r['currency']} [{r['id']}]  {r['dates']}  {r['raw']}  {r['url']}")
    if not rows:
        lines.append("Kiwi: выдача пуста")
    return "\n".join(lines) + "\n"


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    cfg_path = sys.argv[1]
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    args = dict(cfg["args"])
    args.setdefault("currency", "PLN")
    args.setdefault("allow_self_transfer", False)
    args.setdefault("sort", "price")
    base = os.path.join(os.path.dirname(os.path.abspath(cfg_path)), cfg["name"])

    try:
        data = asyncio.run(kiwi_search.search(args))
    except Exception as exc:  # noqa: BLE001 — любой сбой источника: не «изменение», а ошибка тика
        print(f"дозор {cfg['name']}: источник недоступен: {exc}", file=sys.stderr)
        return 1

    tick = journal.now()
    run = journal.compact(tick)
    rows = kiwi_search.rows_for_itineraries(run, data, args, cfg.get("top", 3))
    rows.sort(key=lambda r: r["value"])
    journal.append(rows, base + ".jsonl")
    with open(base + ".latest.md", "w", encoding="utf-8") as fh:
        fh.write(render_latest(cfg, rows, tick.isoformat()))
    sys.stdout.write(render_stable(cfg, args, rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
