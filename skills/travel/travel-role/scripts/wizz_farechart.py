#!/usr/bin/env python3
"""Wizz Air: карта минимумов по дням (asset/farechart через Flywizz), тариф Basic, 1 взрослый.

    /opt/data/travel/lib/venv/bin/python wizz_farechart.py WAW BCN 2026-10-09 [2026-10-12] [--flex 3..10]

Одно плечо — один запрос: на запрошенный день kind=fare, на соседние (окно ±flex, 3–10: пределы
farechart) — other_date. Валюта — аэропорта вылета, переключить её у Wizz нельзя (Flywizz: «no server-side
currency override», 13.09.2026): обратное плечо из-за границы приходит в EUR и пишется как есть,
а рядом — строка курса НБП (kind=rate, одна на валюту) и CALC convert в злотых; в отчёт идёт
convert. Полная выдача Wizz (search/search) закрыта бот-гейтом Kasada — тарифы Regular/Plus
этим путём не берутся; карта минимумов — не живой тариф (14.09.2026: карта 31,99 EUR, сайт и
Google Flights 39,99), живая цена плеча на дату — google_flights.py.
Запрос POST: url — эндпоинт, тело запроса — в поле request; link — форма покупки Wizz с
подставленными маршрутом, датой и одним взрослым (открывается у человека, проверено 14.09.2026).
"""

import argparse
import sys
from datetime import datetime

import journal

SOURCE_ID = "wizzair_api"
BOOKING = "https://www.wizzair.com/en-gb/booking/select-flight/{origin}/{dest}/{day}/null/1/0/0/null"


def booking_link(origin, dest, day):
    """Форма покупки Wizz на плечо и дату, 1 взрослый: шаблон метапоисковиков, сайт для скрипта закрыт."""
    return BOOKING.format(origin=origin, dest=dest, day=day)


def rows_for_chart(run, entries, wanted, url, request, rate_for):
    """rate_for(code) — строка курса для чужой валюты (None — курса нет, пересчёт пропускается);
    вызывающий кладёт её в журнал один раз."""
    rows, skipped = [], []
    for e in sorted(entries, key=lambda x: x.day):
        if e.price is None or e.price.amount is None:
            continue
        day = e.day.date().isoformat()
        route = f"{e.departure_station}-{e.arrival_station}"
        raw = (f"farechart {route} {day} {e.price.amount:.2f} {e.price.currency} "
               f"class {e.class_of_service} ({e.price_type})")
        if e.price_type != "price" or e.price.amount <= 0:
            # checkPrice с amount=0: в карте цены нет, только «проверить» — это не наблюдение
            skipped.append(raw + " — цены в карте нет")
            continue
        row = journal.observation(
            run, "fare" if day == wanted else "other_date", SOURCE_ID, url, route, day,
            float(e.price.amount), e.price.currency, "QUOTED", raw, request=request,
            link=booking_link(e.departure_station, e.arrival_station, day),
        )
        rows.append(row)
        if e.price.currency != "PLN":
            rate = rate_for(e.price.currency)
            if rate is not None:
                rows.append(journal.convert(run, row, rate))
    return rows, skipped


def rate_source(run, rows):
    """rate_for для rows_for_chart: курс берётся один раз на валюту и ложится в rows перед строками,
    которые на него ссылаются. НБП недоступен или валюты нет в таблице A — плечо остаётся в своей
    валюте без convert, остальные строки не теряются."""
    rates = {}

    def rate_for(code):
        if code not in rates:
            try:
                rates[code] = journal.rate_row(run, code)
                rows.append(rates[code])
            except (OSError, ValueError, KeyError, IndexError) as exc:
                print(f"курс {code}-PLN не получен ({exc}): строки в {code} без пересчёта", file=sys.stderr)
                rates[code] = None
        return rates[code]

    return rate_for


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("origin")
    ap.add_argument("dest")
    ap.add_argument("date_out")
    ap.add_argument("date_back", nargs="?")
    ap.add_argument("--flex", type=int, default=3, help="±дней вокруг даты, от 3 до 10")
    journal.add_common_args(ap)
    args = ap.parse_args()
    if args.flex < 3:
        ap.error("--flex не меньше 3: farechart Wizz отвергает окно уже (DayIntervalMustBeGreaterOrEqualTo3)")
    if args.flex > 10:
        ap.error("--flex не больше 10: farechart Wizz отвергает окно шире (DayIntervalMustBeLessOrEqualTo10), "
                 "длинное окно — двумя вызовами")
    run = journal.run_id(args)

    from flywizz import FareChartSearch, WizzAir
    from flywizz.wire import serialize_fare_chart

    legs = [(args.origin, args.dest, args.date_out)]
    if args.date_back:
        legs.append((args.dest, args.origin, args.date_back))

    rows = []
    rate_for = rate_source(run, rows)
    client = WizzAir()
    try:
        for origin, dest, wanted in legs:
            search = FareChartSearch(origin=origin, destination=dest,
                                     date=datetime.fromisoformat(wanted), day_interval=args.flex)
            entries = client.get_fare_chart(search)
            url = f"{client.transport.base}/asset/farechart"
            leg_rows, skipped = rows_for_chart(run, entries, wanted, url, serialize_fare_chart(search),
                                               rate_for)
            for line in skipped:
                print(f"не в журнал: {line}", file=sys.stderr)
            if not leg_rows and not skipped:
                print(f"{origin}-{dest} около {wanted}: farechart пуст", file=sys.stderr)
            rows.extend(leg_rows)
    finally:
        client.close()

    journal.finish(args, run, rows)
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
