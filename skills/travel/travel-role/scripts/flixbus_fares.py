#!/usr/bin/env python3
"""Flixbus: рейсы на дату с ценой и сбором платформы, без ключа, PLN.

    /opt/data/travel/lib/venv/bin/python flixbus_fares.py Warszawa Modlin 2026-10-26 --route WAW-WMI \\
        [--pax 1] [--after 10:00] [--before 14:00] [--top 3]

Города — словами, как в автокомплите Flixbus (id берётся из него каждый раз: чужой id
даёт HTTP 400); аэропорты тоже: «Modlin», «Port lotniczy Warszawa», «Barcelona Airport».
На каждый рейс из окна времени — самые дешёвые `--top` — две строки: kind=ground (дорога до
аэропорта, цена на всех пассажиров, как отдаёт Flixbus) и kind=fee (сбор платформы, `of` → рейс).
Статус QUOTED. `--route` — как в отчёте: коды IATA или город, `WAW-WMI`.
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request

import journal

SOURCE_ID = "flixbus_api"
AUTOCOMPLETE = "https://global.api.flixbus.com/search/autocomplete/cities"
SEARCH = "https://global.api.flixbus.com/search/service/v4/search"


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": journal.BROWSER_UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.load(resp)


def city_candidates(query):
    """Первые хиты автокомплита: (id, имя). Берётся первый; станционный id даёт 0 рейсов —
    тогда печатаются остальные, чтобы позвать скрипт с точным именем."""
    data = get_json(AUTOCOMPLETE + "?" + urllib.parse.urlencode({"q": query, "lang": "pl"}))
    items = data if isinstance(data, list) else data.get("cities", [])
    if not items:
        raise SystemExit(f"Flixbus: автокомплит не знает «{query}»")
    return [(c["id"], c.get("name", query)) for c in items[:6]]


def search_url(from_id, to_id, iso_date, pax):
    day = journal.iso_to_ddmmyyyy(iso_date).replace("/", ".")
    return SEARCH + "?" + urllib.parse.urlencode({
        "from_city_id": from_id, "to_city_id": to_id, "departure_date": day,
        "products": json.dumps({"adult": pax}), "currency": "PLN", "locale": "pl",
        "search_by": "cities", "include_after_midnight_rides": 1,
    })


def rides(data):
    """Рейсы из ответа: results{} бывает и контейнером с items, и самим рейсом."""
    stations = data.get("stations") or {}
    out = []
    for trip in data.get("trips", []):
        for value in (trip.get("results") or {}).values():
            for x in (value.get("items", [value]) if isinstance(value, dict) else []):
                if x.get("status") != "available":
                    continue
                price = x.get("price") or {}
                out.append({
                    "dep": x["departure"]["date"][:16], "arr": x["arrival"]["date"][:16],
                    "total": price.get("total"), "with_fee": price.get("total_with_platform_fee"),
                    "seats": (x.get("available") or {}).get("seats"),
                    "from_st": (stations.get(x["departure"].get("station_id")) or {}).get("name"),
                    "to_st": (stations.get(x["arrival"].get("station_id")) or {}).get("name"),
                })
    return out


def rows_for_rides(run, chosen, route, iso_date, url, pax):
    rows = []
    for r in chosen:
        raw = (f"Flixbus {r['from_st']} {r['dep'][11:]} -> {r['to_st']} {r['arr'][11:]}; "
               f"{r['total']:.2f} zl ({pax} adult), miejsc {r['seats']}")
        fare = journal.observation(run, "ground", SOURCE_ID, url, route, iso_date,      # дорога, не перелёт
                                   float(r["total"]), "PLN", "QUOTED", raw, pax=pax)
        rows.append(fare)
        if r["with_fee"] is not None and r["with_fee"] > r["total"]:
            fee = round(r["with_fee"] - r["total"], 2)
            rows.append(journal.observation(
                run, "fee", SOURCE_ID, url, route, iso_date, fee, "PLN", "QUOTED",
                f"oplata platformowa {fee:.2f} zl (total_with_platform_fee {r['with_fee']:.2f})",
                pax=pax, of=fare["id"],
            ))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("origin", help="город или станция словами, как в автокомплите")
    ap.add_argument("dest")
    ap.add_argument("date", help="ISO, 2026-10-26")
    ap.add_argument("--route", required=True, help="как в отчёте: WAW-WMI")
    ap.add_argument("--pax", type=int, default=1)
    ap.add_argument("--after", default="00:00", help="окно: не раньше HH:MM")
    ap.add_argument("--before", default="23:59", help="окно: не позже HH:MM")
    ap.add_argument("--top", type=int, default=3, help="сколько самых дешёвых рейсов из окна писать")
    journal.add_common_args(ap)
    args = ap.parse_args()
    run = journal.run_id(args)

    origins, dests = city_candidates(args.origin), city_candidates(args.dest)
    (from_id, from_name), (to_id, to_name) = origins[0], dests[0]
    url = search_url(from_id, to_id, args.date, args.pax)
    found = rides(get_json(url))
    if not found:
        print("рейсов нет; автокомплит предлагал: " + " | ".join(n for _, n in origins) + "  ->  "
              + " | ".join(n for _, n in dests) + " — попробовать другое имя", file=sys.stderr)
    window = [r for r in found if args.after <= r["dep"][11:] <= args.before and r["total"] is not None]
    chosen = sorted(window, key=lambda r: (r["total"], r["dep"]))[: args.top]
    print(f"{from_name} -> {to_name} {args.date}: рейсов {len(found)}, в окне {args.after}-{args.before}: "
          f"{len(window)}, пишу {len(chosen)}", file=sys.stderr)

    rows = rows_for_rides(run, chosen, args.route, args.date, url, args.pax)
    journal.finish(args, run, rows)
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
