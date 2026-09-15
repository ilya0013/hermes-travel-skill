#!/usr/bin/env python3
"""Google Flights через fast-flights, без браузера: блок «лучшие» (~6 вариантов), все перевозчики.

    /opt/data/travel/lib/venv/bin/python google_flights.py WAW BCN 2026-10-09 [2026-10-12] \
        [--adults 1] [--max-stops 1] [--top 6] [--airline Wizz]

Одна строка QUOTED на вариант; цена — за всех пассажиров в обе стороны, как показывает Google.
`--airline` оставляет варианты одного перевозчика и без `--max-stops` ищет прямые: в блоке
«лучшие» без этого прямой Wizz WAW→BCN 26.10 (209 zł) прятался за стыковками Lufthansa от 751
(проверено 14.09.2026). Нет такого перевозчика — строк нет, код 1.
Валюта в ответе не приходит, берётся из параметра запроса → currency_observed: false (W202).
Блок «Statystyki cen» (эталон) этот путь не отдаёт — за эталоном роль идёт браузером.
"""

import argparse
import sys

import journal

SOURCE_ID = "google_flights"


def leg_text(fl):
    d, t = fl.departure.date, fl.departure.time
    a, at = fl.arrival.date, fl.arrival.time
    return (f"{fl.from_airport.code} {d[0]:04d}-{d[1]:02d}-{d[2]:02d} {t[0]:02d}:{t[1]:02d}"
            f"→{fl.to_airport.code} {a[0]:04d}-{a[1]:02d}-{a[2]:02d} {at[0]:02d}:{at[1]:02d}")


def rows_for_results(run, results, origin, dest, dates, pax, currency, url, top, airline=None):
    rows = []
    items = list(results)
    if airline:
        items = [it for it in items if any(airline.lower() in a.lower() for a in it.airlines)]
    for item in items[:top]:
        raw = (f"{'+'.join(item.airlines)}; {', '.join(leg_text(f) for f in item.flights)}; "
               f"{item.price} {currency}, {'w obie strony' if '/' in dates else 'w jedna strone'}, "
               f"blok najlepsze, typ {item.type}")
        rows.append(journal.observation(
            run, "fare", SOURCE_ID, url, f"{origin}-{dest}", dates, float(item.price), currency,
            "QUOTED", raw, pax=pax, currency_observed=False,
        ))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("origin")
    ap.add_argument("dest")
    ap.add_argument("date_out")
    ap.add_argument("date_back", nargs="?")
    ap.add_argument("--adults", type=int, default=1)
    ap.add_argument("--max-stops", type=int)
    ap.add_argument("--top", type=int, default=6)
    ap.add_argument("--airline", help="только этот перевозчик (подстрока имени, без регистра); прямые рейсы")
    journal.add_common_args(ap)
    args = ap.parse_args()
    run = journal.run_id(args)

    import fast_flights as ff
    flights = [ff.FlightQuery(date=args.date_out, from_airport=args.origin, to_airport=args.dest)]
    if args.date_back:
        flights.append(ff.FlightQuery(date=args.date_back, from_airport=args.dest, to_airport=args.origin))
    currency = "PLN"
    query = ff.create_query(
        flights=flights, passengers=ff.Passengers(adults=args.adults),
        trip="round-trip" if args.date_back else "one-way", currency=currency, language="pl",
        max_stops=0 if args.airline and args.max_stops is None else args.max_stops,
    )
    try:
        results = ff.get_flights(query)
    except IndexError as exc:
        # 15.09.2026: WAW→SIN 5–16.11 трижды «рейсов нет», а Kiwi давал Qatar — падает парсер fast-flights
        # (IndexError на варианте без цены: PR #114 fast-flights слит 11.09.2026, в PyPI 3.1.0 его нет),
        # а не маршрут пуст; до этого ошибка выдавалась за пустую выдачу
        print(f"Google Flights: парсер fast-flights упал ({exc}) — выдача не прочитана. Это сбой источника, "
              f"не «рейсов нет»: в --failed google_flights, цену маршрута бери у Kiwi или со страницы "
              f"браузером: {query.url()}", file=sys.stderr)
        journal.finish(args, run, [])
        return 1
    dates = f"{args.date_out}/{args.date_back}" if args.date_back else args.date_out
    rows = rows_for_results(run, results, args.origin, args.dest, dates, args.adults, currency,
                            query.url(), args.top, args.airline)
    if not rows:
        what = f"нет вариантов {args.airline}" if args.airline else "блок «лучшие» пуст"
        print(f"Google Flights: {what}", file=sys.stderr)
    journal.finish(args, run, rows)
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
