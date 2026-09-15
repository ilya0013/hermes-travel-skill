#!/usr/bin/env python3
"""Одна строка журнала из аргументов — для наблюдений, снятых браузером, и для сумм.

Наблюдение:
    journal_add.py --run R --kind fare --source-id ryanair_site --url URL \
        --route WMI-BCN --dates 2026-11-06/2026-11-09 --value 524.21 --status CONFIRMED \
        --raw "Kontynuuj dla zl 524,21 - w obie strony, taryfa Basic" [--pax 1] [--of ID]

Сумма или разность (value считается из строк-операндов, руками не передаётся):
    journal_add.py --run R --kind fare --route WMI-BCN --dates 2026-11-06/2026-11-09 \
        --operator sum --inputs ID1,ID2,ID3 --raw "524,21+29,99+3,99"

id и ts ставит скрипт; price_prefix ставится сам, если в --raw есть маркер «от».
Валюта всегда PLN: цена в другой валюте в журнал не пишется (контракт).
"""

import argparse
import os
import sys

import journal


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    journal.add_common_args(ap)
    ap.add_argument("--kind", required=True,
                    choices=["fare", "fee", "forecast", "benchmark", "other_date", "diff", "lead", "ground"],
                    help="ground: дорога аэропорт↔город одним билетом; report.py считает её по порогу профиля")
    ap.add_argument("--route", required=True)
    ap.add_argument("--dates", required=True)
    ap.add_argument("--raw", required=True)
    ap.add_argument("--pax", type=int, default=1)
    ap.add_argument("--source-id")
    ap.add_argument("--url")
    ap.add_argument("--value", type=float)
    ap.add_argument("--currency", default="PLN", help="валюта источника кодом ISO; чужую report.py переводит по НБП")
    ap.add_argument("--status", choices=["ORIENTIR", "QUOTED", "CONFIRMED"])
    ap.add_argument("--of", help="fee: id строки, на которой доплата наблюдалась")
    ap.add_argument("--operator", choices=["sum", "diff"], help="строка CALC")
    ap.add_argument("--inputs", help="CALC: id операндов через запятую, из этого же прогона")
    ap.add_argument("--comparable-false", action="store_true", help="diff по разным route/dates")
    args = ap.parse_args()

    if not args.run:
        ap.error("--run обязателен: строка ложится в уже начатый прогон")
    run = args.run

    if args.operator:
        if not args.inputs:
            ap.error("--operator требует --inputs")
        if args.value is not None or args.source_id or args.url or args.status:
            ap.error("у CALC value/source-id/url/status не задаются: value считается из inputs")
        ids = [i.strip() for i in args.inputs.split(",") if i.strip()]
        if not os.path.exists(args.journal):
            print(f"журнала нет: {args.journal}", file=sys.stderr)
            return 1
        run_rows = journal.load_rows(args.journal, run)
        if not run_rows:
            print(f"в журнале {args.journal} нет строк прогона {run} — проверь --run", file=sys.stderr)
            return 1
        by_id = {r["id"]: r for r in run_rows}
        missing = [i for i in ids if i not in by_id]
        if missing:
            print(f"в прогоне {run} нет строк: {', '.join(missing)}", file=sys.stderr)
            return 1
        extra = {"comparable": False} if args.comparable_false else {}
        row = journal.calc(run, args.kind, args.route, args.dates, args.operator,
                           [by_id[i] for i in ids], args.raw, pax=args.pax, **extra)
    else:
        for name in ("source_id", "url", "value", "status"):
            if getattr(args, name) is None:
                ap.error(f"наблюдению нужен --{name.replace('_', '-')}")
        if args.kind == "fee" and not args.of:
            ap.error("fee требует --of")
        extra = {"of": args.of} if args.of else {}
        row = journal.observation(run, args.kind, args.source_id, args.url, args.route, args.dates,
                                  args.value, args.currency.upper(), args.status, args.raw, pax=args.pax,
                                  price_prefix=journal.price_prefix_of(args.raw), **extra)

    journal.finish(args, run, [row])
    return 0


if __name__ == "__main__":
    sys.exit(main())
