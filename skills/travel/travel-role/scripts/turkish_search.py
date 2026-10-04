#!/usr/bin/env python3
"""Turkish Airlines MCP (официальный сервер перевозчика; с 04.10.2026 — вход по OAuth) из скрипта: выдача — строками журнала.

    /opt/data/travel/lib/venv/bin/python turkish_search.py WAW BKK 2026-11-05 [2026-11-19] \
        [--adults 1] [--bags 1] [--top 3] --run R
    /opt/data/travel/lib/venv/bin/python turkish_search.py WAW BKK 2026-11-05 --calendar [--weeks 2]

Цены в PLN, тарифы с багажом и ссылка на покупку у самого перевозчика (deeplink тарифа). Одна сторона —
`search_flights`: строка на вариант, тариф — самый дешёвый (`--bags 1` — самый дешёвый со сдаваемым багажом).
Туда-обратно (международное) — два шага сервера: самый дешёвый вариант туда, к нему варианты обратно
(`search_inbound_flights`); строка — вся поездка, value — итог ответа «обратно» (= цена в deeplink), url — ссылка на всю
поездку. Туда-обратно у Turkish дешевле двух билетов в одну сторону (01.10.2026 WAW→BKK 05.11–19.11: 4805 PLN
против 3168 × 2). `--calendar` — дешёвые даты вокруг даты (`find_flight_price_calendar`), только печать: цена
календаря округлена (3200 против 3168), в журнал не идёт. Сервер — mcp-app.turkishtechlab.com (каталог
коннекторов Claude, проверено 01.10.2026 с VPS: PLN); инструменты брони (PNR, фамилия, оплата) не зовутся.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

import airports
import journal

SOURCE_ID = "turkish_mcp"
MCP_URL = "https://mcp-app.turkishtechlab.com/mcp"
_SESSION = {}


class TurkishError(RuntimeError):
    """Сервер ответил ошибкой или не ответил."""


def post(body):
    """POST JSON-RPC в сессию MCP (streamable HTTP): ответ — JSON или поток `data:`."""
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "User-Agent": "hermes-travel-role turkish_search", "MCP-Protocol-Version": "2025-06-18"}
    if _SESSION.get("id"):
        headers["Mcp-Session-Id"] = _SESSION["id"]
    req = urllib.request.Request(MCP_URL, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            _SESSION.setdefault("id", resp.headers.get("Mcp-Session-Id"))
            text = resp.read().decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:          # 04.10.2026: сервер закрыт входом (OAuth, аккаунт Turkish), ключа у нас нет
            raise TurkishError("сервер требует вход аккаунтом Turkish Airlines (401) — источника нет, не повторяй; "
                               "Turkish есть в google_flights.py и Kiwi") from exc
        raise TurkishError(f"HTTPError: {str(exc)[:80]}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise TurkishError(f"{type(exc).__name__}: {str(exc)[:80]}") from exc
    if not text or text.startswith("{"):
        return json.loads(text) if text else {}
    for line in text.splitlines():
        if line.startswith("data:") and line[5:].strip().startswith("{"):
            return json.loads(line[5:])
    raise TurkishError(f"ответ не JSON: {text[:80]}")


def call(tool, args):
    """structuredContent инструмента; сессия открывается при первом вызове."""
    if "id" not in _SESSION:
        post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "travel-role", "version": "1"}}})
        post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    answer = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}})
    res = answer.get("result") or {}
    if answer.get("error") or res.get("isError"):
        text = " ".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        raise TurkishError(f"{tool}: {str(answer.get('error') or text)[:160]}")
    return res.get("structuredContent") or {}


def options(data):
    infos = (data.get("availabilityData") or {}).get("originDestinationInformations") or []
    return infos[-1].get("originDestinationOptions", []) if infos else []


def checked_bag(option, fare):
    """Сдаваемый багаж тарифа (кг) по miniRulesInfoMap: у CL в Европу 0, в Азию 20 (01.10.2026)."""
    rules = (option.get("miniRulesInfoMap") or {}).get((fare.get("brandCodeList") or [""])[0], {})
    bag = rules.get("checkedBaggageAllowance") or {}
    return max(int(bag.get("kilos") or 0), 23 * int(bag.get("pieces") or 0))


def total(fare):
    return float(fare["passengerFare"]["grandTotalFare"]["amount"])     # за всех; totalFare — за одного


def pick_fare(option, bags):
    """Самый дешёвый тариф варианта; `bags` — только со сдаваемым багажом. Нет такого — None."""
    fares = [f for f in option.get("bookingPriceInfos", []) if not bags or checked_bag(option, f) > 0]
    return min(fares, key=total, default=None)


def leg_text(option):
    """«WAW→IST→BKK 09:35→04:15 12h40» — как у Kiwi: report.py считает по нему ночь в пути."""
    segs = option["flightSegments"]
    airports = [segs[0]["departureAirportCode"]] + [s["arrivalAirportCode"] for s in segs]
    mins = int(option.get("journeyDuration") or 0) // 60000
    return (f"{'→'.join(airports)} {segs[0]['departureDateTime'][11:16]}→{segs[-1]['arrivalDateTime'][11:16]}"
            f" {mins // 60}h{mins % 60:02d}")


def flights(option):
    return [s["flightCode"]["airlineCode"] + s["flightCode"]["flightNumber"] for s in option["flightSegments"]]


def fare_text(option, fare):
    kg = checked_bag(option, fare)
    carry = ((option.get("miniRulesInfoMap") or {}).get(fare["brandCodeList"][0], {})
             .get("carryOnBaggageAllowance") or {}).get("pieces", 1)
    return f"fare {fare['brandCodeList'][0]}; bags p0/c{carry}/h{1 if kg else 0}" + (f" {kg} kg" if kg else "")


def link(option, fare):
    links = option.get("deeplinks") or []
    i = option.get("bookingPriceInfos", []).index(fare)
    return links[i] if i < len(links) else (links[0] if links else MCP_URL)


def rows_one_way(run, opts, bags, pax, top, date):
    rows = []
    for option in sorted(opts, key=lambda o: total(pick_fare(o, bags)) if pick_fare(o, bags) else 1e12)[:top]:
        fare = pick_fare(option, bags)
        if not fare:
            continue
        segs = option["flightSegments"]
        raw = (f"{total(fare):.2f} PLN; {leg_text(option)}; TK {' '.join(flights(option))}; {fare_text(option, fare)}; "
               f"stops {len(segs) - 1}")
        rows.append(journal.observation(run, "fare", SOURCE_ID, link(option, fare),
                                        f"{segs[0]['departureAirportCode']}-{segs[-1]['arrivalAirportCode']}",
                                        date, total(fare), "PLN", "QUOTED", raw, pax=pax))
    return rows


def rows_round(run, out_opt, out_fare, back_opts, bags, pax, top, dates):
    """Поездка = выбранный вариант туда + вариант обратно. Цена — grandTotalFare ответа «обратно»: в нём уже вся
    поездка за всех (= `prc` deeplink; totalFare — только обратно за одного). Сложение с «туда» дало 7441 вместо
    4805 PLN на живом прогоне 01.10.2026."""
    rows = []
    priced = [(o, pick_fare(o, bags)) for o in back_opts]
    priced = sorted([p for p in priced if p[1]], key=lambda p: total(p[1]))[:top]
    for option, fare in priced:
        value = total(fare)
        o_segs, b_segs = out_opt["flightSegments"], option["flightSegments"]
        route = f"{o_segs[0]['departureAirportCode']}-{o_segs[-1]['arrivalAirportCode']}"
        if (b_segs[0]["departureAirportCode"], b_segs[-1]["arrivalAirportCode"]) != tuple(route.split("-"))[::-1]:
            route += f"/{b_segs[0]['departureAirportCode']}-{b_segs[-1]['arrivalAirportCode']}"
        raw = (f"{value:.2f} PLN; {leg_text(out_opt)} / {leg_text(option)}; TK {' '.join(flights(out_opt) + flights(option))}; "
               f"{fare_text(out_opt, out_fare)} / {fare_text(option, fare)}; stops {len(o_segs) - 1}/{len(b_segs) - 1}")
        rows.append(journal.observation(run, "fare", SOURCE_ID, link(option, fare), route, dates, value, "PLN",
                                        "QUOTED", raw, pax=pax))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("origin")
    ap.add_argument("dest")
    ap.add_argument("date_out")
    ap.add_argument("date_back", nargs="?")
    ap.add_argument("--adults", type=int, default=1)
    ap.add_argument("--bags", type=int, choices=(0, 1), default=0, help="1 — только тарифы со сдаваемым багажом")
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--calendar", action="store_true", help="дешёвые даты вокруг DATE_OUT, без журнала")
    ap.add_argument("--weeks", type=int, choices=(1, 2, 3, 4), default=2)
    journal.add_common_args(ap)
    ns = ap.parse_args()
    run = journal.run_id(ns)
    # код города сервер не знает: TYO — «рейсов нет», NRT — рейсы и в HND (эвал 02.10.2026)
    origin, dest = airports.main_airport(ns.origin.upper()), airports.main_airport(ns.dest.upper())
    pax = [{"passengerType": "ADT", "quantity": ns.adults}]
    try:
        if ns.calendar:
            cal = call("find_flight_price_calendar", {"originAirportCode": origin, "destinationAirportCode": dest,
                                                      "departureDate": ns.date_out, "timeRange": f"{ns.weeks}_WEEK"
                                                      + ("S" if ns.weeks > 1 else "")})
            print(f"Turkish календарь {origin}→{dest} (эконом, в одну сторону, округлено — для выбора дат, не тариф):")
            for p in cal.get("prices", []):
                print(f"  {p['date']}  {p['amount']} {p.get('currency', '')}{'  ★' if p.get('isBestPrice') else ''}")
            return 0
        args = {"from": origin, "to": dest, "departureDate": ns.date_out, "passengers": pax}
        if ns.date_back:
            args["returnDate"] = ns.date_back
        found = call("search_flights", args)
        out = options(found)
        if ns.date_back and len((found.get("availabilityData") or {}).get("originDestinationInformations") or []) > 1:
            # внутренний рейс: сервер отдал оба плеча сразу, search_inbound_flights звать нельзя (описание инструмента)
            raise TurkishError("внутренний рейс туда-обратно — ищи плечи порознь, двумя вызовами в одну сторону")
        if not ns.date_back:
            rows = rows_one_way(run, out, ns.bags, ns.adults, ns.top, ns.date_out)
        else:
            best = min(((o, pick_fare(o, ns.bags)) for o in out if pick_fare(o, ns.bags)), key=lambda p: total(p[1]),
                       default=None)
            rows = []
            if best:
                back = options(call("search_inbound_flights", {
                    "searchId": found.get("searchId"), "optionId": int(best[0]["optionId"]),
                    "recommendationId": int(best[1]["recommendationId"]), "returnDate": ns.date_back}))
                rows = rows_round(run, best[0], best[1], back, ns.bags, ns.adults, ns.top,
                                  f"{ns.date_out}/{ns.date_back}")
    except TurkishError as exc:                       # без трейсбека: причина одной строкой, exit 1
        print(f"Turkish: {exc}", file=sys.stderr)
        return 1
    if not rows:
        print("Turkish: рейсов нет" + (" с багажом" if ns.bags else ""), file=sys.stderr)
    journal.finish(ns, run, rows)
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
