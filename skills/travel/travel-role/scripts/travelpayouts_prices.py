#!/usr/bin/env python3
"""Travelpayouts / Aviasales Data API: кэш цен из чужих поисков за 48 ч — карта дешевизны, не выдача.

    /opt/data/travel/lib/venv/bin/python travelpayouts_prices.py dates WAW BCN 2026-10-09 [2026-10-12] [--direct] [--limit 10]
    /opt/data/travel/lib/venv/bin/python travelpayouts_prices.py calendar WAW BCN 2026-10 [--nights 3-4] [--direct]
    /opt/data/travel/lib/venv/bin/python travelpayouts_prices.py offers WAW [BCN] [--airline AY]
    (фильтр по авиакомпании применяется в скрипте: сервер параметр airline не учитывает)

dates    — prices_for_dates: самые дешёвые билеты на даты (день или месяц YYYY-MM).
calendar — grouped_prices: минимум на каждый день месяца вылета (--nights — длина поездки).
offers   — get_special_offers: аномально низкие цены по направлению/авиакомпании.

Все строки — ORIENTIR: это кэш поисков других людей, а не живая выдача; дальше цена
проверяется источником (Kiwi и др.). Валюта запрашивается PLN; ответ не в PLN — не в журнал.
Токен — TRAVELPAYOUTS_TOKEN из окружения или из файла .env в HERMES_HOME; в url строки его нет
(заголовок X-Access-Token), на экран он не выводится. Поле link — ссылка на выдачу Aviasales.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import journal

SOURCE_ID = "travelpayouts_api"
BASE = "https://api.travelpayouts.com/aviasales/v3/"
AVIASALES = "https://www.aviasales.com"
TOKEN_VAR = "TRAVELPAYOUTS_TOKEN"


def parse_env_line(line):
    """KEY=VALUE из .env (кавычки и `export ` снимаются); не пара — None."""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    if line.startswith("export "):
        line = line[len("export "):]
    key, _, value = line.partition("=")
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return key.strip(), value


def load_token(env=None, hermes_home=None):
    env = os.environ if env is None else env
    token = env.get(TOKEN_VAR)
    if token:
        return token
    path = os.path.join(hermes_home or env.get("HERMES_HOME", "/opt/data"), ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                pair = parse_env_line(line)
                if pair and pair[0] == TOKEN_VAR and pair[1]:
                    return pair[1]
    raise SystemExit(f"{TOKEN_VAR} не найден ни в окружении, ни в {path}")


def request(endpoint, params, token):
    url = BASE + endpoint + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, headers={"X-Access-Token": token, "Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{endpoint}: HTTP {exc.code} — {'токен не принят' if exc.code in (401, 403) else exc.reason}")
    if not data.get("success", True):
        raise SystemExit(f"{endpoint}: success=false, error={data.get('error')!r}")
    return url, data


def _dates(item):
    dep = (item.get("departure_at") or "")[:10]
    ret = (item.get("return_at") or "")[:10]
    return f"{dep}/{ret}" if ret else dep


def _raw(kind_label, item):
    parts = [f"{kind_label} {item.get('origin_airport') or item.get('origin')}→{item.get('destination_airport') or item.get('destination')}",
             f"{item.get('departure_at', '')}", f"back {item.get('return_at') or '-'}",
             f"price={item.get('price')}", f"airline={item.get('airline')} {item.get('flight_number', '')}".strip()]
    if item.get("transfers") is not None:
        parts.append(f"transfers={item['transfers']}/{item.get('return_transfers', '-')}")
    if item.get("duration") is not None:
        parts.append(f"duration={item['duration']}min")
    if item.get("airline_title"):
        parts.append(item["airline_title"])
    return "; ".join(parts)


def rows_for_items(run, items, kind_label, url, currency, currency_observed, wanted_dates=None):
    """Строки ORIENTIR из списка элементов v3 (prices_for_dates, grouped_prices, special offers)."""
    rows, skipped = [], []
    for item in items:
        price = item.get("price")
        if not isinstance(price, (int, float)):
            continue
        dates = _dates(item)
        if not dates:
            skipped.append(_raw(kind_label, item) + " — нет даты вылета")
            continue
        if currency != "PLN":
            skipped.append(_raw(kind_label, item) + f" — валюта {currency}, не PLN")
            continue
        extra = {}
        if item.get("link"):
            extra["link"] = AVIASALES + item["link"]
        if not currency_observed:
            extra["currency_observed"] = False
        route = f"{item.get('origin_airport') or item.get('origin')}-{item.get('destination_airport') or item.get('destination')}"
        rows.append(journal.observation(
            run, "fare" if dates == wanted_dates else "other_date", SOURCE_ID, url, route, dates,
            float(price), currency, "ORIENTIR", _raw(kind_label, item), **extra,
        ))
    return rows, skipped


def response_currency(data, requested):
    cur = data.get("currency")
    return (cur.upper(), True) if isinstance(cur, str) and cur else (requested, False)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["dates", "calendar", "offers"])
    ap.add_argument("origin")
    ap.add_argument("dest", nargs="?")
    ap.add_argument("depart", nargs="?", help="dates: YYYY-MM-DD или YYYY-MM; calendar: YYYY-MM")
    ap.add_argument("back", nargs="?", help="dates: дата возврата (YYYY-MM-DD или YYYY-MM)")
    ap.add_argument("--direct", action="store_true", help="только прямые")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--nights", help="calendar: длина поездки в днях, 3-4")
    ap.add_argument("--airline", help="offers: код авиакомпании")
    ap.add_argument("--market", help="рынок кэша (по умолчанию — по origin)")
    journal.add_common_args(ap)
    args = ap.parse_args()
    run = journal.run_id(args)
    token = load_token()
    currency = "PLN"

    if args.mode == "dates":
        if not (args.dest and args.depart):
            ap.error("dates: нужны ORIGIN DEST DEPART [BACK]")
        params = {"origin": args.origin, "destination": args.dest, "departure_at": args.depart,
                  "return_at": args.back, "one_way": "false" if args.back else "true",
                  "direct": str(args.direct).lower(), "currency": currency, "limit": args.limit,
                  "sorting": "price", "market": args.market}
        url, data = request("prices_for_dates", params, token)
        items = data.get("data") or []
        wanted = f"{args.depart}/{args.back}" if args.back else args.depart
        label = "prices_for_dates"
    elif args.mode == "calendar":
        if not (args.dest and args.depart):
            ap.error("calendar: нужны ORIGIN DEST MONTH")
        params = {"origin": args.origin, "destination": args.dest, "departure_at": args.depart,
                  "group_by": "departure_at", "direct": str(args.direct).lower(), "currency": currency,
                  "market": args.market}
        if args.nights:
            lo, _, hi = args.nights.partition("-")
            params["min_trip_duration"], params["max_trip_duration"] = int(lo), int(hi or lo)
        url, data = request("grouped_prices", params, token)
        items = list((data.get("data") or {}).values())
        wanted = None
        label = "grouped_prices"
    else:
        params = {"origin": args.origin, "destination": args.dest, "airline": args.airline,
                  "currency": currency, "locale": "en", "market": args.market}
        url, data = request("get_special_offers", params, token)
        items = data.get("data") or []
        if args.airline:
            # серверный фильтр airline ответ не сужает (проверено 13.09.2026) — режем сами
            items = [i for i in items if (i.get("airline") or "").upper() == args.airline.upper()]
        wanted = None
        label = "special_offer"

    cur, observed = response_currency(data, currency)
    print(f"{label}: записей {len(items)}, валюта ответа {cur if observed else 'не указана'}")
    rows, skipped = rows_for_items(run, items, label, url, cur, observed, wanted)
    for line in skipped:
        print(f"не в журнал: {line}", file=sys.stderr)
    if not rows:
        print(f"{label}: в кэше пусто для этого запроса", file=sys.stderr)
    journal.finish(args, run, rows)
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
