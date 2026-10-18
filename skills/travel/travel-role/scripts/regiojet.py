#!/usr/bin/env python3
"""RegioJet — поезда и автобусы из Польши в Чехию, Австрию, Словакию: цена на день из публичного API сайта, без
ключа, строками kind=ground в журнал.

    /opt/data/travel/lib/venv/bin/python regiojet.py Warszawa Praga 2026-11-14 [2026-11-16] [--pax 2] [--top 3]

Города — любым именем, которое знает RegioJet: Praga, Praha, Prague, Прага (список — `consts/locations`, 170 городов).
API `brn-ybus-pubapi.sa.cz/restapi` — тот, которым ходит сам regiojet.pl; ключа нет (проверено 18.10.2026 с VPS).
Поиск отдаёт рейсы трёх дней с даты — берутся только рейсы запрошенного дня. Цена — нижний класс рейса (`priceFrom`)
на всех пассажиров (`tariffs` × N: на двоих 138 = 2 × 69), PLN; в `raw` — вилка классов, время в пути, пересадки.
Живьём 18.10.2026 из Варшавы: Прага 9 ч 31 мин без пересадок 69–199 zł, Острава 6 ч 59–159, Брно 9 ч с пересадкой,
Вена 11,5 ч и Братислава 13 ч с двумя; Берлин (26 ч) и Будапешт (21 ч) — поезд с ночным автобусом, не вариант;
Катовице — прямо в аэропорт Пыжовице, 2 ч 44 мин, 85–145.

kind=ground — наземная дорога, не перелёт: `report.py --auto` её в ★ не берёт, сумма поездом — `--variant`.
route — города, как их пишет RegioJet (`Warszawa-Praga`): у вокзалов кодов IATA нет. url — запрос к API, link —
страница выбора рейса regiojet.pl на этот день и число пассажиров (форма проверена браузером 18.10.2026: 14.11, «od 69 zł»).
"""

import argparse
import json
import sys
import unicodedata
import urllib.request
from datetime import date
from urllib.parse import urlencode

import journal

SOURCE_ID = "regiojet_api"
API = "https://brn-ybus-pubapi.sa.cz/restapi"
PAGE = "https://regiojet.pl/?"
HEADERS = {"X-Currency": "PLN", "X-Lang": "pl", "Accept": "application/json", "User-Agent": journal.BROWSER_UA}


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _norm(s):
    s = unicodedata.normalize("NFKD", s.strip().casefold())
    return "".join(c for c in s if not unicodedata.combining(c)).replace("ł", "l")


def find_city(locations, name):
    """Город по имени или любому из его aliases (там бывают списки через запятую); None — RegioJet туда не возит."""
    want = _norm(name)
    for country in locations:
        for city in country.get("cities") or []:
            names = [city.get("name") or ""] + [a for al in city.get("aliases") or [] for a in al.split(",")]
            if want in {_norm(n) for n in names if n}:
                return city
    return None


def stations_of(locations):
    return {s["id"]: s.get("name") for c in locations for city in c.get("cities") or [] for s in city.get("stations") or []}


def query(frm, to, day, pax):
    """Параметры поиска: `tariffs` повторяется на каждого пассажира — цена ответа на всех."""
    return urlencode([("tariffs", "REGULAR")] * pax + [
        ("fromLocationType", "CITY"), ("fromLocationId", frm["id"]),
        ("toLocationType", "CITY"), ("toLocationId", to["id"]), ("departureDate", day)])


def rows_for_routes(run, data, frm, to, day, pax, stations, top, url, link):
    """Строки kind=ground рейсов запрошенного дня, самые дешёвые `top`; не продаётся или без цены — мимо."""
    routes = [r for r in data.get("routes") or [] if str(r.get("departureTime") or "")[:10] == day
              and r.get("bookable") and isinstance(r.get("priceFrom"), (int, float))]
    rows = []
    for r in sorted(routes, key=lambda r: r["priceFrom"])[:top]:
        t0, t1 = str(r["departureTime"]), str(r.get("arrivalTime") or "")
        arrive = t1[11:16] + (f" ({t1[8:10]}.{t1[5:7]})" if t1[:10] != t0[:10] else "")
        raw = (f"{'+'.join(r.get('vehicleTypes') or [])} {frm['name']} {stations.get(r.get('departureStationId'), '')} "
               f"{t0[11:16]} → {to['name']} {stations.get(r.get('arrivalStationId'), '')} {arrive}, "
               f"{r.get('travelTime')}, пересадок {r.get('transfersCount')}, "
               f"{r['priceFrom']:g}–{r.get('priceTo') or r['priceFrom']:g} PLN, мест {r.get('freeSeatsCount')}")
        rows.append(journal.observation(run, "ground", SOURCE_ID, url, f"{frm['name']}-{to['name']}", day,
                                        float(r["priceFrom"]), "PLN", "QUOTED", raw, pax=pax, link=link))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("origin", help="город отправления: Warszawa")
    ap.add_argument("dest", help="город назначения: Praga")
    ap.add_argument("date_out")
    ap.add_argument("date_back", nargs="?")
    ap.add_argument("--pax", type=int, default=1)
    ap.add_argument("--top", type=int, default=3, help="рейсов на день, самые дешёвые")
    journal.add_common_args(ap)
    ns = ap.parse_args()
    run = journal.run_id(ns)
    try:
        locations = get(f"{API}/consts/locations")
        frm, to = find_city(locations, ns.origin), find_city(locations, ns.dest)
        missing = [n for n, c in ((ns.origin, frm), (ns.dest, to)) if c is None]
        if missing:
            print(f"RegioJet: города {', '.join(missing)} в его сети нет — поездом RegioJet туда не доехать", file=sys.stderr)
            return 1
        stations = stations_of(locations)
        rows = []
        legs = [(frm, to, date.fromisoformat(ns.date_out).isoformat())]
        if ns.date_back:
            legs.append((to, frm, date.fromisoformat(ns.date_back).isoformat()))
        for a, b, day in legs:
            q = query(a, b, day, ns.pax)
            url = f"{API}/routes/search/simple?{q}"
            leg = rows_for_routes(run, get(url), a, b, day, ns.pax, stations, ns.top, url, PAGE + q)
            print(f"RegioJet {a['name']}→{b['name']} {day}: рейсов дня {len(leg)}")
            rows += leg
    except Exception as exc:  # noqa: BLE001 — сбой источника одной строкой: роль ставит его в --failed
        print(f"RegioJet: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
        return 1
    if not rows:
        print("RegioJet: на эти дни рейсов нет", file=sys.stderr)
    journal.finish(ns, run, rows)
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
