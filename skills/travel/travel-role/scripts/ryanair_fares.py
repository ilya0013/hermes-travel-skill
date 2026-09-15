#!/usr/bin/env python3
"""Ryanair fare-finder через ryanair-py: минимум по дню (тариф Basic, 1 взрослый), PLN.

    /opt/data/travel/lib/venv/bin/python ryanair_fares.py WMI BCN 2026-10-09 [2026-10-12] [--flex 3]
    /opt/data/travel/lib/venv/bin/python ryanair_fares.py WMI BCN --month 2026-10

На запрошенную дату — строка kind=fare, на соседние дни окна --flex — other_date.
С --month — минимум по каждому дню месяца в обе стороны (эндпоинт cheapestPerDay, один
запрос на плечо; проверен 13.09.2026), все other_date, без суммы: пары дат складывает
роль (journal_add.py --operator sum).
С обратной датой добавляется сумма двух one-way (CALC): у Ryanair round-trip — это два
билета, а не одна цена продавца. Из Шопена (WAW) Ryanair в BCN не летает — брать WMI.
Статус QUOTED: цена перевозчика на конкретный день, но не страница тарифа.
У каждой строки link — страница выбора рейса Ryanair на это плечо и день (форма из живых
строк ryanair_site 14.09.2026): ссылка на покупку без браузера, как link у карты Wizz.
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import date, timedelta
from urllib.parse import urlencode

import journal

SOURCE_ID = "ryanair_api"
ENDPOINT = "https://services-api.ryanair.com/farfnd/v4/oneWayFares"
SELECT_PAGE = ("https://www.ryanair.com/pl/pl/trip/flights/select?adults=1&dateOut={day}"
               "&originIata={origin}&destinationIata={dest}&isReturn=false&discount=0")
# без searchMode=ALL fare-finder отдаёт один самый дешёвый день окна, а не каждый день:
# проверено 14.09.2026, WMI-BCN ±3 дня — 1 строка против 4 (заметка агента 13.09.2026)
SEARCH_ALL = {"searchMode": "ALL"}


def select_link(origin, dest, day):
    """Страница выбора рейса Ryanair на плечо и день, 1 взрослый — та, что открывал агент браузером."""
    return SELECT_PAGE.format(origin=origin, dest=dest, day=day)


def query_url(origin, dest, date_from, date_to, currency="PLN"):
    """Тот же запрос, что делает ryanair-py, в виде адреса с параметрами."""
    params = {
        "departureAirportIataCode": origin,
        "outboundDepartureDateFrom": date_from,
        "outboundDepartureDateTo": date_to,
        "outboundDepartureTimeFrom": "00:00",
        "outboundDepartureTimeTo": "23:59",
        "currency": currency,
        "arrivalAirportIataCode": dest,
        **SEARCH_ALL,
    }
    return f"{ENDPOINT}?{urlencode(params)}"


def fetch_window(api, origin, dest, date_from, date_to):
    """Все дни окна, по одному рейсу-минимуму на день."""
    return api.get_cheapest_flights(origin, date_from, date_to, destination_airport=dest,
                                    custom_params=dict(SEARCH_ALL))


def window(iso, flex):
    d = date.fromisoformat(iso)
    return (d - timedelta(days=flex)).isoformat(), (d + timedelta(days=flex)).isoformat()


def month_url(origin, dest, year_month, currency="PLN"):
    return (f"{ENDPOINT}/{origin}/{dest}/cheapestPerDay?"
            f"{urlencode({'outboundMonthOfDate': year_month + '-01', 'currency': currency})}")


def rows_for_month(run, fares, origin, dest, url):
    """Строки из cheapestPerDay: по одной на день с ценой; дни без рейса и не в PLN пропускаются."""
    rows = []
    for f in fares:
        price = f.get("price")
        if not price or f.get("unavailable") or f.get("soldOut"):
            continue
        if price.get("currencyCode") != "PLN":
            print(f"{origin}-{dest} {f.get('day')}: {price.get('value')} {price.get('currencyCode')} — "
                  "валюта не PLN, в журнал не идёт", file=sys.stderr)
            continue
        raw = (f"cheapestPerDay {f['day']} {f.get('departureDate', '')[11:16]}-{f.get('arrivalDate', '')[11:16]} "
               f"{origin}-{dest} {price['value']:.2f} {price['currencyCode']} (Basic)")
        rows.append(journal.observation(
            run, "other_date", SOURCE_ID, url, f"{origin}-{dest}", f["day"],
            float(price["value"]), price["currencyCode"], "QUOTED", raw,
            link=select_link(origin, dest, f["day"]),
        ))
    return rows


def fetch_month(url):
    req = urllib.request.Request(url, headers={"User-Agent": journal.BROWSER_UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["outbound"]["fares"]


def rows_for_leg(run, flights, origin, dest, wanted, url):
    """Строки журнала из объектов Flight ryanair-py (по одному на день окна)."""
    rows = []
    for f in sorted(flights, key=lambda x: x.departureTime):
        day = f.departureTime.date().isoformat()
        raw = (f"{f.flightNumber} {f.departureTime.strftime('%Y-%m-%d %H:%M')} "
               f"{f.origin}-{f.destination} {f.price:.2f} {f.currency} (fare-finder, Basic)")
        rows.append(journal.observation(
            run, "fare" if day == wanted else "other_date", SOURCE_ID, url,
            f"{f.origin}-{f.destination}", day, float(f.price), f.currency, "QUOTED", raw,
            link=select_link(f.origin, f.destination, day),
        ))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("origin")
    ap.add_argument("dest")
    ap.add_argument("date_out", nargs="?")
    ap.add_argument("date_back", nargs="?")
    ap.add_argument("--flex", type=int, default=0, help="±дней вокруг каждой даты")
    ap.add_argument("--month", help="YYYY-MM: весь месяц в обе стороны вместо дат")
    journal.add_common_args(ap)
    args = ap.parse_args()
    if bool(args.month) == bool(args.date_out):
        ap.error("нужна либо дата вылета, либо --month")
    if args.month and (not re.fullmatch(r"\d{4}-\d{2}", args.month) or args.flex):
        ap.error("--month в виде YYYY-MM и без --flex: месяц берётся целиком")
    run = journal.run_id(args)
    # эвал деградации: источник «упал» — до любой ветки, и --month тоже. Файл-флаг, потому что
    # окружение в terminal агента не передаётся (проверено 15.09.2026: -e TRAVEL_FAULT не дошёл)
    fault = os.path.join(os.path.dirname(os.path.abspath(args.journal)), ".fault")
    if os.environ.get("TRAVEL_FAULT") == "ryanair_api" or \
            (os.path.exists(fault) and "ryanair_api" in open(fault, encoding="utf-8").read()):
        sys.exit("ryanair_api: HTTP 403 (инъекция отказа для эвала: файл .fault рядом с журналом)")

    rows = []
    if args.month:
        for origin, dest in ((args.origin, args.dest), (args.dest, args.origin)):
            url = month_url(origin, dest, args.month)
            leg_rows = rows_for_month(run, fetch_month(url), origin, dest, url)
            if not leg_rows:
                print(f"{origin}-{dest} {args.month}: cheapestPerDay пуст (нет рейсов)", file=sys.stderr)
            rows.extend(leg_rows)
        journal.finish(args, run, rows)
        return 0 if rows else 1

    from ryanair import Ryanair
    api = Ryanair(currency="PLN")

    legs = [(args.origin, args.dest, args.date_out)]
    if args.date_back:
        legs.append((args.dest, args.origin, args.date_back))
    exact = {}
    for origin, dest, wanted in legs:
        d_from, d_to = window(wanted, args.flex)
        url = query_url(origin, dest, d_from, d_to)
        flights = fetch_window(api, origin, dest, d_from, d_to)
        leg_rows = rows_for_leg(run, flights, origin, dest, wanted, url)
        if not leg_rows:
            print(f"{origin}-{dest} {d_from}..{d_to}: fare-finder пуст (нет рейсов или дат)", file=sys.stderr)
        rows.extend(leg_rows)
        exact[(origin, dest)] = next((r for r in leg_rows if r["kind"] == "fare"), None)

    out_row = exact.get((args.origin, args.dest))
    back_row = exact.get((args.dest, args.origin))
    if out_row and back_row:
        rows.append(journal.calc(
            run, "fare", f"{args.origin}-{args.dest}", f"{args.date_out}/{args.date_back}",
            "sum", [out_row, back_row], f"{out_row['value']:.2f}+{back_row['value']:.2f} (два one-way Basic)",
        ))

    journal.finish(args, run, rows)
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
