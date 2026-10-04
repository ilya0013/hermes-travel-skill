#!/usr/bin/env python3
"""Ryanair fare-finder через ryanair-py: минимум по дню (тариф Basic, 1 взрослый), PLN.

    /opt/data/travel/lib/venv/bin/python ryanair_fares.py WMI BCN 2026-10-09 [2026-10-12] [--flex 3] [--pax 2]
    /opt/data/travel/lib/venv/bin/python ryanair_fares.py WMI BCN --month 2026-10 [--pax 2]
    /opt/data/travel/lib/venv/bin/python ryanair_fares.py WMI,WAW ANY --month 2026-11 --nights 2-3 [--weekend] [--pax 2]

`--pax N` — цена в строке на всех (на одного × N, pax=N): так `report.py --auto --pax N` берёт эти строки.
`ANY` — «куда дешевле»: все направления из каждого аэропорта вылета одним запросом (roundTripFares, ночей `--nights`,
`--weekend` — вылет чт/пт), плечи строками other_date; пары и сумму до двери собирает `report.py --auto`.

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
ANYWHERE_ENDPOINT = "https://services-api.ryanair.com/farfnd/v4/roundTripFares"
SELECT_PAGE = ("https://www.ryanair.com/pl/pl/trip/flights/select?adults={pax}&dateOut={day}"
               "&originIata={origin}&destinationIata={dest}&isReturn=false&discount=0")
# без searchMode=ALL fare-finder отдаёт один самый дешёвый день окна, а не каждый день:
# проверено 14.09.2026, WMI-BCN ±3 дня — 1 строка против 4 (заметка агента 13.09.2026)
SEARCH_ALL = {"searchMode": "ALL"}


def select_link(origin, dest, day, pax=1):
    """Страница выбора рейса Ryanair на плечо, день и число взрослых — та, что открывал агент браузером."""
    return SELECT_PAGE.format(origin=origin, dest=dest, day=day, pax=pax)


def for_pax(value, raw, pax):
    """Цена на всех: fare-finder отдаёт на одного взрослого, сайт на N = цена × N (эвал 02.10.2026, Рим вдвоём:
    4 рейса из 4, 235,48 = 117,74 × 2). Мест по этой цене может быть меньше N — сайт покажет дороже."""
    return (round(value * pax, 2), raw + (f" × {pax} пасс." if pax > 1 else ""))


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


def rows_for_month(run, fares, origin, dest, url, pax=1):
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
        value, raw = for_pax(float(price["value"]), (
            f"cheapestPerDay {f['day']} {f.get('departureDate', '')[11:16]}-{f.get('arrivalDate', '')[11:16]} "
            f"{origin}-{dest} {price['value']:.2f} {price['currencyCode']} (Basic)"), pax)
        rows.append(journal.observation(
            run, "other_date", SOURCE_ID, url, f"{origin}-{dest}", f["day"],
            value, price["currencyCode"], "QUOTED", raw, pax=pax,
            link=select_link(origin, dest, f["day"], pax),
        ))
    return rows


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": journal.BROWSER_UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_month(url):
    return fetch_json(url)["outbound"]["fares"]


def fetch_anywhere(url):
    """Все страницы roundTripFares: `pageNumber` до `nextPage: null` (живьём 04.10.2026: WMI, ноябрь — 2 страницы)."""
    fares, page, seen = [], 0, set()
    while page is not None and page not in seen:          # `page=` API не слышит и отдаёт nextPage 1 вечно
        seen.add(page)
        data = fetch_json(url + (f"&pageNumber={page}" if page else ""))
        fares += data.get("fares") or []
        page = data.get("nextPage")
    return fares


def anywhere_url(origin, year_month, nights, weekend=False, currency="PLN"):
    """roundTripFares без пункта назначения: все направления из `origin` за месяц, `nights` ночей там; `weekend` —
    вылет чт или пт (ночи пт и сб там, как `journal.weekend`). Живьём 04.10.2026: WMI, ноябрь — 141 поездка за 4 с."""
    first = date.fromisoformat(year_month + "-01")
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    params = {
        "departureAirportIataCode": origin,
        "outboundDepartureDateFrom": first.isoformat(), "outboundDepartureDateTo": last.isoformat(),
        "inboundDepartureDateFrom": first.isoformat(),
        "inboundDepartureDateTo": (last + timedelta(days=nights[1])).isoformat(),
        "durationFrom": nights[0], "durationTo": nights[1],
        **({"outboundDepartureDaysOfWeek": "THURSDAY,FRIDAY"} if weekend else {}),
        "currency": currency, "market": "pl-pl", **SEARCH_ALL,
    }
    return f"{ANYWHERE_ENDPOINT}?{urlencode(params)}"


def rows_for_anywhere(run, fares, url, pax=1):
    """Плечи поездок roundTripFares строками other_date — по одной на маршрут и день, самая дешёвая: пары плеч и ночи
    собирает `report.py --auto`. У Ryanair туда-обратно — два билета, цена плеча та же, что one-way."""
    best = {}
    for f in fares:
        for leg in (f["outbound"], f["inbound"]):
            price = leg["price"]
            if price.get("currencyCode") != "PLN":
                continue
            o, d, day = leg["departureAirport"]["iataCode"], leg["arrivalAirport"]["iataCode"], leg["departureDate"][:10]
            if (o, d, day) not in best or price["value"] < best[(o, d, day)]["price"]["value"]:
                best[(o, d, day)] = leg
    rows = []
    for (o, d, day), leg in sorted(best.items()):
        value, raw = for_pax(float(leg["price"]["value"]), (
            f"{leg.get('flightNumber', '')} {leg['departureDate'][:16].replace('T', ' ')} {o}-{d} "
            f"{leg['price']['value']:.2f} PLN (roundTripFares, Basic)"), pax)
        rows.append(journal.observation(run, "other_date", SOURCE_ID, url, f"{o}-{d}", day, value, "PLN", "QUOTED",
                                        raw, pax=pax, link=select_link(o, d, day, pax)))
    return rows


WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def anywhere_summary(origin, fares, pax=1, weekend=False):
    """Города по цене: самая дешёвая поездка в каждый — агенту выбрать 2–3 города, а не читать сотни строк (ревью
    04.10.2026: `report.py --top 8` — 7 раз Венеция на разные даты). `weekend` — только ночи пт и сб там."""
    best = {}
    for f in fares:
        o, b = f["outbound"], f["inbound"]
        if o["price"].get("currencyCode") != "PLN" or b["price"].get("currencyCode") != "PLN":
            continue
        if weekend and not journal.weekend(*(date.fromisoformat(x["departureDate"][:10]) for x in (o, b))):
            continue                                       # API: вылет чт на 2 ночи — чт→сб, ночь субботы дома
        dest, total = o["arrivalAirport"]["iataCode"], (o["price"]["value"] + b["price"]["value"]) * pax
        if dest not in best or total < best[dest][0]:
            best[dest] = (total, o, b)
    day = lambda leg: (lambda d: f"{WEEKDAYS[d.weekday()]} {d:%d.%m}")(date.fromisoformat(leg["departureDate"][:10]))  # noqa: E731
    lines = [f"{origin}: направлений {len(best)} — самая дешёвая поездка в каждое (перелёт, без дороги; пары и ★ — "
             "report.py --auto):"]
    for dest, (total, o, b) in sorted(best.items(), key=lambda kv: kv[1][0]):
        place = ", ".join(x for x in (o["arrivalAirport"].get("name"), o["arrivalAirport"].get("countryName")) if x)
        lines.append(f"  {dest} {total:.0f} PLN  {day(o)} → {day(b)}" + (f"  {place}" if place else ""))
    return lines


def rows_for_leg(run, flights, origin, dest, wanted, url, pax=1):
    """Строки журнала из объектов Flight ryanair-py (по одному на день окна)."""
    rows = []
    for f in sorted(flights, key=lambda x: x.departureTime):
        day = f.departureTime.date().isoformat()
        value, raw = for_pax(float(f.price), (f"{f.flightNumber} {f.departureTime.strftime('%Y-%m-%d %H:%M')} "
                                              f"{f.origin}-{f.destination} {f.price:.2f} {f.currency} (fare-finder, Basic)"), pax)
        rows.append(journal.observation(
            run, "fare" if day == wanted else "other_date", SOURCE_ID, url,
            f"{f.origin}-{f.destination}", day, value, f.currency, "QUOTED", raw, pax=pax,
            link=select_link(f.origin, f.destination, day, pax),
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
    ap.add_argument("--pax", type=int, default=1, help="взрослых: цена в строке — на всех (на одного × N)")
    ap.add_argument("--nights", help="для ANY: ночей «2-3» или «3»")
    ap.add_argument("--weekend", action="store_true", help="для ANY: «на выходные» — вылет в чт или пт")
    journal.add_common_args(ap)
    args = ap.parse_args()
    if args.dest.upper() == "ANY" and not (args.month and args.nights and re.fullmatch(r"\d+(-\d+)?", args.nights)):
        ap.error("ANY: нужны --month YYYY-MM и --nights N-M")
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
    if args.dest.upper() == "ANY":
        lo, _, hi = args.nights.partition("-")
        for origin in (o.strip().upper() for o in args.origin.split(",")):   # API регистрозависим: wmi → 0
            url = anywhere_url(origin, args.month, (int(lo), int(hi or lo)), args.weekend)
            try:
                fares = fetch_anywhere(url)
            except (OSError, ValueError) as exc:          # один аэропорт упал — строки другого остаются
                print(f"{origin}: roundTripFares не ответил ({exc})", file=sys.stderr)
                continue
            print("\n".join(anywhere_summary(origin, fares, args.pax, args.weekend)))
            rows.extend(rows_for_anywhere(run, fares, url, args.pax))
        journal.finish(args, run, rows, show=False)     # сотни плеч — в журнал; глазами — сводка и report.py --list
        return 0 if rows else 1
    if args.month:
        for origin, dest in ((args.origin, args.dest), (args.dest, args.origin)):
            url = month_url(origin, dest, args.month)
            leg_rows = rows_for_month(run, fetch_month(url), origin, dest, url, args.pax)
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
        leg_rows = rows_for_leg(run, flights, origin, dest, wanted, url, args.pax)
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
            pax=args.pax,
        ))

    journal.finish(args, run, rows)
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
