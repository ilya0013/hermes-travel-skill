#!/usr/bin/env python3
"""Kiwi.com MCP (search-flight) из скрипта: выдача сразу ложится строками в журнал.

    /opt/data/travel/lib/venv/bin/python kiwi_search.py @args.json [--top 5]
    /opt/data/travel/lib/venv/bin/python kiwi_search.py WAW BCN 2026-10-09 [2026-10-12] \
        [--out-to 2026-10-31] [--nights 3-4] [--adults 1] [--max-stops 0] [--self-transfer] \
        [--airlines FR,W6] [--hold-bags 1] [--top 5]

Аргументы — как у инструмента search-flight (даты там dd/mm/yyyy, здесь ISO: скрипт переводит).
По умолчанию currency PLN и allow_self_transfer false; с --self-transfer в строках стоит
self_transfer_allowed: true — вариант может быть связкой раздельных билетов, риск называется.
Одна строка QUOTED на маршрут: route — фактические аэропорты (Kiwi подставляет WMI за WAW),
dates — даты этого маршрута; kind=fare, если они совпали с запрошенными, иначе other_date.
url — bookingUrl Kiwi; CONFIRMED только со страницы тарифа продавца.
Тот же поиск в MCP-инструменте агента даёт то же самое, но строки журнала — только отсюда.
"""

import argparse
import asyncio
import json
import sys
import time

import journal

SOURCE_ID = "kiwi_mcp"
MCP_URL = "https://mcp.kiwi.com/"
# холодный вызов Kiwi отдаёт неполную выдачу: 15.09.2026 WAW→CPT searchTimeMs 9479 → минимум 4930 PLN,
# повтор через 2 с (889 мс) → 3400; WAW→SIN 3352 → 3083. Тёплый вызов — 0,9–2,6 с.
COLD_MS = 5000
# сервер Kiwi отказывает эпизодами на десятки секунд, от частоты не зависит (зонд 16.09.2026 на чистом
# агенте: 12:41–12:52 и 14:44–14:50 UTC — MCPError -32603 без data, попутно обрывы SSL; в 12:53 12 вызовов
# впритык прошли). Установка и поиск у участника попали в такой эпизод и упали трассой. Паузы и повторы
# перекрывают эпизод; пауза перед повтором холодного — чтобы не бить впритык.
REFUSAL_WAITS = (20, 40)
WARM_PAUSE = 3


class KiwiRefused(RuntimeError):
    """Сервер MCP отказал на все попытки."""


def _leaves(exc):
    if isinstance(exc, BaseExceptionGroup):
        for e in exc.exceptions:
            yield from _leaves(e)
    else:
        yield exc


def refusal_reason(exc):
    """Чем отказал сервер (MCPError, обрыв соединения) — или None, если ошибка другая и её надо показать."""
    for e in _leaves(exc):
        name = type(e).__name__
        if name == "MCPError":
            return f"MCPError code {getattr(e, 'code', '?')}"
        if isinstance(e, (ConnectionError, OSError)) or name in ("ConnectError", "ReadTimeout", "RemoteProtocolError"):
            return f"{name}: {str(e)[:60]}"
    return None


def call_with_retries(args, call=None, waits=REFUSAL_WAITS, sleep=time.sleep):
    """Один вызов search-flight; отказ сервера — пауза и повтор, после всех попыток KiwiRefused."""
    call = call or (lambda a: asyncio.run(search(a)))
    reason = None
    for i, wait in enumerate((0,) + tuple(waits)):
        if wait:
            print(f"Kiwi: сервер отказал ({reason}) — пауза {wait} с, повтор {i}/{len(waits)}", file=sys.stderr)
            sleep(wait)
        try:
            return call(args)
        except Exception as exc:                       # noqa: BLE001 — чужие ошибки пробрасываются ниже
            reason = refusal_reason(exc)
            if reason is None:
                raise
    raise KiwiRefused(f"сервер mcp.kiwi.com отказал {len(waits) + 1} раза подряд ({reason}) — "
                      "сбой на стороне Kiwi, подождать минуту и повторить")


def build_args(ns):
    """Аргументы search-flight из флагов командной строки."""
    a = {"flyFrom": ns.origin, "flyTo": ns.dest, "departureDate": journal.iso_to_ddmmyyyy(ns.date_out),
         "adults": ns.adults, "currency": "PLN", "allow_self_transfer": ns.self_transfer, "sort": "price"}
    if ns.out_to:
        a["departureDateTo"] = journal.iso_to_ddmmyyyy(ns.out_to)
    if ns.date_back:
        a["returnDate"] = journal.iso_to_ddmmyyyy(ns.date_back)
    if ns.nights:
        lo, _, hi = ns.nights.partition("-")
        a["nights_in_dst_from"], a["nights_in_dst_to"] = int(lo), int(hi or lo)
    if ns.max_stops is not None:
        a["max_sector_stopovers"] = ns.max_stops
    if ns.airlines:
        a["select_airlines"] = ns.airlines
    if ns.hold_bags:
        a["adults_hold_bags"] = [ns.hold_bags] * ns.adults
    return a


async def search(args, url=MCP_URL):
    from mcp import Client
    async with Client(url) as client:
        res = await client.call_tool("search-flight", args)
        if res.is_error:
            raise RuntimeError("search-flight: " + " ".join(getattr(c, "text", str(c)) for c in res.content))
        data = getattr(res, "structured_content", None)
        if not data:
            data = json.loads(next(c.text for c in res.content if getattr(c, "text", None)))
        return data


def search_warm(args, call=None, sleep=time.sleep):
    """Выдача search-flight; холодный вызов (searchTimeMs выше COLD_MS) повторяется один раз, после паузы."""
    call = call or call_with_retries
    data = call(args)
    ms = data.get("searchTimeMs") or 0
    if ms > COLD_MS:
        print(f"Kiwi: холодный вызов ({ms} мс), выдача неполная — повтор через {WARM_PAUSE} с", file=sys.stderr)
        sleep(WARM_PAUSE)
        try:
            data = call(args)
        except Exception as exc:                       # noqa: BLE001 — повтор упал: холодная выдача лучше пустой
            print(f"Kiwi: повтор не ответил ({type(exc).__name__}: {str(exc)[:80]}) — строки из холодного "
                  "вызова, цены могут быть выше", file=sys.stderr)
    return data


def leg_dates(it):
    out = it["outbound"]["departureTime"][:10]
    inb = it.get("inbound")
    return f"{out}/{inb['departureTime'][:10]}" if inb else out


def carriers(it):
    names = []
    for leg in (it["outbound"], it.get("inbound")):
        for s in (leg or {}).get("segments", []):
            if s["carrierName"] not in names:
                names.append(s["carrierName"])
    return names


def flight_numbers(it):
    return [s["flightNumber"] for leg in (it["outbound"], it.get("inbound")) for s in (leg or {}).get("segments", [])]


def leg_text(leg):
    # время плеч — в строке журнала, чтобы отчёт не ходил за ним в MCP или Google Flights
    # (заметка агента 14.09.2026: «kiwi_search.py времена в строку не пишет»)
    dep, arr = leg.get("departureTime", "")[11:16], leg.get("arrivalTime", "")[11:16]
    text = "→".join(leg["route"]) + (f" {dep}→{arr}" if dep and arr else "")
    secs = leg.get("durationSeconds")          # время в пути со стыковками — прогон владельца 15.09.2026 его не назвал
    return text + (f" {int(secs) // 3600}h{int(secs) % 3600 // 60:02d}" if secs else "")


def route_text(it):
    parts = [leg_text(it["outbound"])]
    if it.get("inbound"):
        parts.append(leg_text(it["inbound"]))
    return " / ".join(parts)


def bags_text(it):
    b = it["baggage"]
    return f"bags p{b['personalItem']}/c{b['cabinBag']}/h{b['checkedBag']}"


def describe_itinerary(it, currency):
    return (f"{it['price']:.0f} {currency}  {leg_dates(it)}  {route_text(it)}  "
            f"{'+'.join(carriers(it))}  {bags_text(it)}")


def rows_for_itineraries(run, data, args, top, wanted_dates=None):
    """Строки журнала из ответа search-flight (top по порядку выдачи)."""
    currency = data["currency"]
    pax = args.get("adults", 1) + args.get("children", 0)
    rows = []
    for it in data.get("itineraries", [])[:top]:
        dates = leg_dates(it)
        route = f"{it['outbound']['from']}-{it['outbound']['to']}"
        back = it.get("inbound")
        if back and (back["from"], back["to"]) != (it["outbound"]["to"], it["outbound"]["from"]):
            route += f"/{back['from']}-{back['to']}"      # обратно в другой аэропорт: WAW-BUD/BUD-WMI
        raw = (f"{it['priceFormatted']}; {route_text(it)}; {'+'.join(carriers(it))} "
               f"{' '.join(flight_numbers(it))}; {bags_text(it)}; stops {it['outbound']['stops']}"
               + (f"/{it['inbound']['stops']}" if it.get("inbound") else ""))
        rows.append(journal.observation(
            run, "fare" if dates == wanted_dates else "other_date", SOURCE_ID, it["bookingUrl"],
            route, dates, float(it["price"]), currency, "QUOTED", raw, pax=pax,
            self_transfer_allowed=bool(args.get("allow_self_transfer", True)),
        ))
    return rows


def wanted_dates_of(args):
    """ISO-даты запроса, если он на фиксированные дни (без окна и без nights)."""
    if args.get("departureDateTo") or args.get("nights_in_dst_from") or args.get("departureDateFlexDays"):
        return None
    from datetime import datetime
    out = datetime.strptime(args["departureDate"], "%d/%m/%Y").date().isoformat()
    if args.get("returnDate") and not args.get("returnDateTo") and not args.get("returnDateFlexDays"):
        return f"{out}/{datetime.strptime(args['returnDate'], '%d/%m/%Y').date().isoformat()}"
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("origin", help="IATA/город или @args.json с аргументами search-flight")
    ap.add_argument("dest", nargs="?")
    ap.add_argument("date_out", nargs="?")
    ap.add_argument("date_back", nargs="?")
    ap.add_argument("--out-to", help="последняя дата вылета окна (ISO)")
    ap.add_argument("--nights", help="ночей в пункте назначения: 3-4")
    ap.add_argument("--adults", type=int, default=1)
    ap.add_argument("--max-stops", type=int)
    ap.add_argument("--self-transfer", action="store_true")
    ap.add_argument("--airlines", help="только эти перевозчики: FR,W6")
    ap.add_argument("--hold-bags", type=int, default=0, help="регистрируемых мест на взрослого")
    ap.add_argument("--top", type=int, default=5)
    journal.add_common_args(ap)
    ns = ap.parse_args()
    run = journal.run_id(ns)

    if ns.origin.startswith("@"):
        args = journal.parse_args_file(ns.origin)
        args.setdefault("currency", "PLN")
        args.setdefault("allow_self_transfer", False)
    else:
        if not (ns.dest and ns.date_out):
            ap.error("нужны ORIGIN DEST DATE_OUT или @args.json")
        if ns.date_back and (ns.out_to or ns.nights):
            # 15.09.2026: DATE_BACK вместе с окном ушёл в Kiwi как «01–21.11, обратно 11.11, 10 ночей» → 0 результатов
            ap.error("DATE_BACK не сочетается с --out-to/--nights: окно само задаёт даты возврата — убери DATE_BACK")
        args = build_args(ns)

    try:
        data = search_warm(args)
    except KiwiRefused as exc:                         # без трейсбека: причина одной строкой, exit 1
        print(f"Kiwi: {exc}", file=sys.stderr)
        return 1
    print(f"query: {data.get('query')}  results: {data.get('resultsCount')}  searchTimeMs: {data.get('searchTimeMs')}")
    rows = rows_for_itineraries(run, data, args, ns.top, wanted_dates_of(args))
    if not rows:
        print("Kiwi: выдача пуста", file=sys.stderr)
    journal.finish(ns, run, rows)
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
