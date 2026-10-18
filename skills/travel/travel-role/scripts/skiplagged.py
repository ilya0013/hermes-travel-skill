#!/usr/bin/env python3
"""Skiplagged MCP (`mcp.skiplagged.com/mcp`, официальный, без ключа и входа): «куда угодно» по всем авиакомпаниям,
рейсы на даты и календарь цен — строками в журнал.

    /opt/data/travel/lib/venv/bin/python skiplagged.py anywhere WAW 2026-11-27 2026-11-30 [--pax 2] [--top 20] [--confirm 3]
    /opt/data/travel/lib/venv/bin/python skiplagged.py anywhere WAW 2026-11-01 --out-to 2026-11-30 --nights 2-3 --weekends \
        [--confirm 3]
    /opt/data/travel/lib/venv/bin/python skiplagged.py search WAW VCE 2026-11-27 [2026-11-30] [--pax 2] [--top 5] \
        [--max-stops none|one] [--self-transfer]
    /opt/data/travel/lib/venv/bin/python skiplagged.py calendar WAW BCN 2026-11-27 [2026-11-30] \
        [--window 2026-11-01/2026-11-30] [--top 10] [--pax 2]

anywhere — `sk_destinations_anywhere`: города по цене туда-обратно на даты, все авиакомпании (Ryanair ANY — только
  Ryanair). Цена равна минимуму поиска на те же даты (живьём 18.10.2026: Венеция 67 $ = Ryanair FR551/FR550, Будапешт
  113 $ = Wizz), но аэропорта вылета в ответе нет: WAW у Skiplagged — город, Шопен или Модлин, а от аэропорта зависит
  сумма до двери. Поэтому строки ORIENTIR. С --weekends — пары «на выходные» окна (как Kiwi --weekends), по городу
  одна строка — самая дешёвая пара. `--confirm N` — N самых дешёвых городов сразу проверяются поиском (`search` на их
  пару, по 2 рейса): строки QUOTED с аэропортами идут в `report.py --auto` без лишнего вызова агента.
search — `sk_flights_search`: рейсы на даты, строки QUOTED с фактическими аэропортами (`WAW-VCE`; обратно в другой —
  `WAW-BCN/BCN-WMI`), перевозчиком, номерами рейсов и временем; туда-обратно — одна строка на поездку, это часто два
  билета разных перевозчиков (KLM туда, Ryanair обратно). Аэропорты Skiplagged верные: FR551 WAW→VCE — Шопен, это
  подтвердил fare-finder Ryanair (192 zł из WAW, из WMI пусто; 18.10.2026). Скрытый город (hidden city) выключен
  всегда: билет теряет обратное плечо и багаж; раздельные билеты внутри плеча (virtual interlining) — только с
  --self-transfer, тогда в строках self_transfer_allowed: true; тот же флаг — у плеча с пересадкой между разными
  перевозчиками и под пометкой standard («Wizz Air, Ryanair», живьём 18.10.2026).
calendar — `sk_flex_departure_calendar`: минимум по дню вылета ±30 дней (с BACK — поездка той же длины). Сам
  Skiplagged пишет «for flexible-date price discovery, not exact itinerary selection» — строки other_date ORIENTIR,
  самые дешёвые --top внутри --window; цену выбранных дат даёт живой поиск.

Цены в USD и на всех пассажиров (на двоих Венеция 133 = 2 × 67, живьём 18.10.2026), злотые считает report.py
курсом НБП. url строки — страница Skiplagged с этим поиском (покупка — у них, редиректом к продавцу).
Частота: одна сессия MCP на запуск, вызовы по очереди — см. `Paced`.
"""

import argparse
import contextlib
import re
import sys
import time
from datetime import date

import airports
import journal
from kiwi_search import _leaves, refusal_reason, weekend_pairs

SOURCE_ID = "skiplagged_mcp"
MCP_URL = "https://mcp.skiplagged.com/mcp"
SITE = "https://skiplagged.com/flights/"
# Лимит частоты по IP — Cloudflare (замеры с VPS 18.10.2026): ~11 запросов HTTP подряд, дальше 429 и блок ~60 с; запросы
# во время блока тоже считаются — после снятия прошло 4. Сессия клиента mcp — ещё несколько запросов на открытие и
# закрытие: новая сессия на вызов — 2 вызова из 8; в одной сессии 6 подряд, седьмой — отказ; раз в 10 с — 10 из 10.
# Клиент mcp 2.2.0 отдаёт отказ как MCPError −32603 «Server returned an error response» без кода HTTP
# (mcp/client/streamable_http.py) — повтор по MCPError и обрыву связи, как у Kiwi, после всего блока.
PACE = 10             # секунд между вызовами сессии: 6 в минуту и открытие — в лимите
REFUSAL_WAITS = (65, 65)   # блок ~60 с; бить сервер во время блока — продлевать его


async def _tool(client, name, args):
    res = await client.call_tool(name, args)
    if res.is_error:
        raise RuntimeError(f"{name}: " + " ".join(getattr(c, "text", str(c)) for c in res.content))
    data = getattr(res, "structured_content", None)
    if not data:
        raise RuntimeError(f"{name}: ответ без structuredContent")
    return data


class Refused(RuntimeError):
    """Сервер отказал на все попытки — блок не снят: следующие вызовы прогона тоже упрутся в него."""


def with_retries(fn, waits=REFUSAL_WAITS, sleep=time.sleep):
    """`fn()`; отказ сервера (MCPError, обрыв) — пауза и повтор, остальные ошибки — наружу."""
    reason = None
    for i, wait in enumerate((0,) + tuple(waits)):
        if wait:
            print(f"Skiplagged: сервер отказал ({reason}) — пауза {wait} с, повтор {i}/{len(waits)}", file=sys.stderr)
            sleep(wait)
        try:
            return fn()
        except Exception as exc:                       # noqa: BLE001 — чужие ошибки пробрасываются
            reason = refusal_reason(exc)
            if reason is None:
                raise
    raise Refused(f"сервер отказал {len(waits) + 1} раза подряд ({reason}) — лимит частоты или сбой Skiplagged, "
                  "подождать минуту и повторить")


def call_tool(name, args, call, waits=REFUSAL_WAITS, sleep=time.sleep):
    return with_retries(lambda: call(name, args), waits, sleep)


@contextlib.contextmanager
def session(url=MCP_URL):
    """Одна сессия официального клиента MCP на весь запуск: синхронный вызов `call(name, args)` через портал anyio.
    Отказ на открытии — тот же MCPError внутри ExceptionGroup (замер 18.10.2026) — с повтором."""
    from anyio.from_thread import start_blocking_portal
    from mcp import Client
    with start_blocking_portal() as portal, contextlib.ExitStack() as stack:
        client = with_retries(lambda: stack.enter_context(portal.wrap_async_context_manager(Client(url))))
        yield lambda name, args: portal.call(_tool, client, name, args)


class Paced:
    """Вызовы сессии по очереди, не чаще раза в PACE секунд (от конца прошлого); каждый — с повторами."""

    def __init__(self, call, sleep=time.sleep, clock=time.monotonic):
        self.call, self.sleep, self.clock, self.last = call, sleep, clock, None

    def __call__(self, name, args):
        if self.last is not None:
            self.sleep(max(0.0, PACE - (self.clock() - self.last)))
        try:
            return call_tool(name, args, self.call, sleep=self.sleep)
        finally:
            self.last = self.clock()


def page(origin, dest, out, back=None):
    """Страница поиска Skiplagged на даты — та же форма, что `searchUrl` ответа (живьём 18.10.2026)."""
    return f"{SITE}{origin}/{dest}/{out}" + (f"/{back}" if back else "")


def best_by_city(answers, skip_country=None):
    """Город назначения → (карточка, вылет, возврат) самой дешёвой пары из ответов `[((вылет, возврат), ответ)]`.
    Город «Unknown» (у Skiplagged вокзалы и автостанции: ZSB, QKL), не Standard (скрытый город) и своя страна
    `skip_country` (живьём 18.10.2026 первым шёл Краков самолётом LOT, 85 $) — мимо."""
    best, skipped = {}, []
    for (out, back), data in answers:
        for card in data.get("results") or []:
            dest, price = card.get("destination") or {}, card.get("price") or {}
            code, amount = dest.get("airport"), price.get("amount")
            if not code or not isinstance(amount, (int, float)):
                continue
            if card.get("flightType") != "Standard" or (dest.get("city") or "Unknown") == "Unknown" or (
                    skip_country and str(dest.get("region") or "").casefold() == skip_country.casefold()):
                skipped.append(f"{code} {dest.get('city')} {card.get('flightType')}")
                continue
            if code not in best or amount < best[code][0]["price"]["amount"]:
                best[code] = (card, out, back)
    return sorted(best.values(), key=lambda t: t[0]["price"]["amount"]), skipped


def anywhere_rows(run, origin, best, pax, top, kind):
    """Строки ORIENTIR по городам, самые дешёвые `top`."""
    rows = []
    for card, out, back in best[:top]:
        dest, price = card["destination"], card["price"]
        raw = (f"{dest.get('city')} ({dest['airport']}), {dest.get('region')}; {card.get('flightType')}; "
               f"{price['amount']} {price.get('currency')}; {out}→{back}")
        rows.append(journal.observation(
            run, kind, SOURCE_ID, card.get("deepLink") or page(origin, dest["airport"], out, back),
            f"{origin}-{dest['airport']}", f"{out}/{back}", float(price["amount"]), price.get("currency") or "USD",
            "ORIENTIR", raw, pax=pax))
    return rows


def _numbers(card):
    """Номера рейсов туда и обратно из id карточки: `…trip=KL1314-KL1519,W61478`."""
    trip = str(card.get("id") or "").partition("trip=")[2]
    there, _, back = trip.partition(",")
    return there.replace("-", "+"), back.replace("-", "+")


def _duration(text):
    """«3h 10m» → «3h10»: форма Kiwi, по ней report.py видит прилёт на следующий день."""
    m = re.fullmatch(r"\s*(\d+)h(?:\s*(\d+)m)?\s*", str(text or ""))
    return f"{m.group(1)}h{int(m.group(2) or 0):02d}" if m else str(text or "")


def leg_text(leg, numbers):
    dep, arr = leg.get("departure") or {}, leg.get("arrival") or {}
    t0, t1 = str(dep.get("dateTime") or ""), str(arr.get("dateTime") or "")
    return (f"{leg.get('airlines')} {numbers} {dep.get('airport')}→{arr.get('airport')} {t0[:10]} "
            f"{t0[11:16]}→{t1[11:16]} {_duration(leg.get('duration'))}, пересадок {leg.get('layovers')}")


def search_args(origin, dest, out, back, pax, top, self_transfer, max_stops=None):
    """Аргументы sk_flights_search: дешёвые первыми, скрытый город — никогда; карточек с запасом — часть отсеет
    `search_rows` (WAW→LIS 18.10.2026: 25 из 60 — раздельные билеты)."""
    args = {"origin": origin, "destination": dest, "departureDate": out, "adults": pax, "sort": "price",
            "limit": max(top * 4, 20), "includeHiddenCity": False, "includeVirtualInterlining": self_transfer}
    if back:
        args["returnDate"] = back
    if max_stops:
        args["maxStops"] = max_stops
    return args


def search_rows(run, data, wanted, pax, top, self_transfer):
    """Строка QUOTED на карточку рейса (туда-обратно — одна строка на поездку), аэропорты — фактические, `top` первых.
    Пометки карточек живьём 18.10.2026 (WAW→LIS, WAW→MAD, скрытый город разрешён) — только `standard` и
    `virtual-interline` (раздельные билеты внутри плеча): берутся они, вторая — с --self-transfer; иная пометка
    (скрытый город, если сервер не послушает `includeHiddenCity: false`) — мимо."""
    rows = []
    for card in data.get("flights") or []:
        if len(rows) == top:
            break
        amount = (card.get("price") or {}).get("amount")
        dep, arr = card.get("departure") or {}, card.get("arrival") or {}
        attrs = card.get("attributes") or []
        split = "virtual-interline" in attrs
        if not isinstance(amount, (int, float)) or not dep.get("airport") or not arr.get("airport"):
            continue
        if not ("standard" in attrs or split and self_transfer):
            continue
        there, back_no = _numbers(card)
        back = card.get("returnFlight")
        route, dates = f"{dep['airport']}-{arr['airport']}", str(dep.get("dateTime") or "")[:10]
        raw = leg_text(card, there)
        if back:
            b0, b1 = back.get("departure") or {}, back.get("arrival") or {}
            if (b0.get("airport"), b1.get("airport")) != (arr["airport"], dep["airport"]):
                route += f"/{b0.get('airport')}-{b1.get('airport')}"
            dates += "/" + str(b0.get("dateTime") or "")[:10]
            raw += "; обратно " + leg_text(back, back_no)
        raw += f"; {amount} {card['price'].get('currency')}; {', '.join(card.get('attributes') or [])}"
        # пересадка между разными перевозчиками в одном плече — может быть два билета, даже под пометкой standard:
        # живьём 18.10.2026 WAW→CIA «Wizz Air, Ryanair W61423+FR4225» пришло как standard, обратно — 28 ч 50 мин
        mixed = any("," in str(leg.get("airlines") or "") and (leg.get("layovers") or 0) > 0 for leg in (card, back) if leg)
        extra = {"self_transfer_allowed": True} if split or mixed else {}
        rows.append(journal.observation(
            run, "fare" if dates == wanted else "other_date", SOURCE_ID, card.get("deepLink") or data.get("searchUrl"),
            route, dates, float(amount), card["price"].get("currency") or "USD", "QUOTED", raw, pax=pax, **extra))
    return rows


def calendar_rows(run, data, window, pax, top):
    """Самые дешёвые дни календаря внутри окна (поездка целиком в нём) — строки other_date ORIENTIR. Валюты в точках
    календаря нет — USD, как у карточек рейсов того же сервера (`currency_observed: false`)."""
    chart = data.get("chart") or {}
    origin, dest = chart.get("origin") or data.get("origin"), chart.get("destination") or data.get("destination")
    points = []
    for p in chart.get("points") or []:
        out, back = p.get("departure"), p.get("return")
        if not out or not isinstance(p.get("price"), (int, float)):
            continue
        if window and not (window[0] <= out and (back or out) <= window[1]):
            continue
        points.append(p)
    rows = []
    for p in sorted(points, key=lambda p: (p["price"], p["departure"]))[:top]:
        out, back = p["departure"], p.get("return")
        rows.append(journal.observation(
            run, "other_date", SOURCE_ID, page(origin, dest, out, back), f"{origin}-{dest}",
            f"{out}/{back}" if back else out, float(p["price"]), "USD", "ORIENTIR",
            f"{chart.get('title') or 'calendar'}: {out}" + (f"→{back}" if back else "") + f" {p['price']}",
            pax=pax, currency_observed=False))
    return rows


def _iso(s):
    return date.fromisoformat(s).isoformat()


def anywhere_pairs(ns, ap):
    """Пары (вылет, возврат) запроса: одна из аргументов или «на выходные» окна."""
    if ns.weekends:
        if len(ns.rest) != 1 or not (ns.out_to and ns.nights):
            ap.error("anywhere --weekends: ORIGIN ВЫЛЕТ --out-to ДАТА --nights N-M, без ВОЗВРАТА")
        lo, _, hi = ns.nights.partition("-")
        return [(a.isoformat(), b.isoformat())
                for a, b in weekend_pairs(_iso(ns.rest[0]), _iso(ns.out_to), (int(lo), int(hi or lo)))]
    if len(ns.rest) != 2:
        ap.error("anywhere: ORIGIN ВЫЛЕТ ВОЗВРАТ или ORIGIN ВЫЛЕТ --out-to ДАТА --nights N-M --weekends")
    return [(_iso(ns.rest[0]), _iso(ns.rest[1]))]


def run_anywhere(ns, run, origin, pairs, call):
    answers, refused = [], False
    for out, back in pairs:
        try:
            answers.append(((out, back), call("sk_destinations_anywhere",
                                              {"from": origin, "depart": out, "return": back, "adults": ns.pax})))
        except Refused as exc:   # ревью 18.10.2026: каждая следующая пара ждала бы ещё 140 с — 12 пар дольше 600 с терминала
            print(f"Skiplagged {out}–{back}: {exc}; остальные пары не спрашиваю, пишу собранное", file=sys.stderr)
            refused = True
            break
        except Exception as exc:  # noqa: BLE001 — одна пара упала, остальные ищутся
            print(f"Skiplagged {out}–{back}: {why(exc)}", file=sys.stderr)
    best, skipped = best_by_city(answers, ns.skip_country)
    rows = anywhere_rows(run, origin, best, ns.pax, ns.top or 20, "other_date" if ns.weekends else "fare")
    print(f"anywhere {origin}: пар дат {len(pairs)}, ответили {len(answers)}, городов {len(rows)}; "
          f"{origin} у Skiplagged — город (Шопен или Модлин): аэропорт и рейсы — search" +
          (f", проверка {min(ns.confirm, len(best))} самых дешёвых" if ns.confirm else ""))
    if skipped:
        print("мимо (не Standard, город Unknown, своя страна): " + ", ".join(sorted(set(skipped))), file=sys.stderr)
    for card, out, back in [] if refused else best[:ns.confirm]:
        dest = card["destination"]["airport"]
        try:
            data = call("sk_flights_search", search_args(origin, dest, out, back, ns.pax, 2, False))
        except Refused as exc:
            print(f"Skiplagged search {dest} {out}–{back}: {exc}; проверку остальных городов не делаю", file=sys.stderr)
            break
        except Exception as exc:  # noqa: BLE001 — город без проверки остаётся ориентиром
            print(f"Skiplagged search {dest} {out}–{back}: {why(exc)}", file=sys.stderr)
            continue
        rows += search_rows(run, data, f"{out}/{back}", ns.pax, 2, False)
    return rows


def why(exc):
    """Причина словами: ошибка, вылетевшая сквозь сессию MCP, приходит обёрнутой в ExceptionGroup — эвал 18.10.2026:
    агент видел «ExceptionGroup: unhandled errors in a TaskGroup», а внутри было «Invalid value for to» (код города ROM)."""
    return "; ".join(f"{type(e).__name__}: {str(e)[:200]}" for e in _leaves(exc))


def airports_of(code):
    """Аэропорты города из `airports.CITIES`; код, который сам аэропорт (WAW, VCE), или не город — пусто."""
    aps = airports.CITIES.get(code, ())
    return () if code in aps else aps


def run_route(ns, run, origin, call):
    """search и calendar на маршрут; (строки, был ли сбой). Код назначения — как дан: живьём 18.10.2026 Skiplagged
    принял LON, PAR, MIL, TYO (сразу по аэропортам города), а ROM отверг — «Invalid value for to» (эвал, Рим). Отвергнут
    код города — по его аэропортам из `airports.CITIES` (ROM → FCO, CIA), `--top` на каждый. Ошибка аэропорта — строка
    в stderr, остальные ищутся; отказ сервера (блок) — остальные не спрашиваются."""
    out = _iso(ns.rest[1])
    back = _iso(ns.rest[2]) if len(ns.rest) == 3 else None
    window = tuple(_iso(d) for d in ns.window.split("/")) if ns.window else None
    code = ns.rest[0].upper()
    dests, rows, failed = [code], [], False
    for dest in dests:          # список растёт: отвергнутый код города → его аэропорты
        try:
            if ns.mode == "search":
                data = call("sk_flights_search", search_args(origin, dest, out, back, ns.pax, ns.top or 5,
                                                             ns.self_transfer, ns.max_stops))
                found = search_rows(run, data, f"{out}/{back}" if back else out, ns.pax, ns.top or 5, ns.self_transfer)
                print(f"search {origin}→{dest} {out}" + (f"–{back}" if back else "")
                      + f": рейсов {len(data.get('flights') or [])}, строк {len(found)}")
            else:
                args = {"origin": origin, "destination": dest, "departureDate": out, "adults": ns.pax, "sort": "price"}
                if back:
                    args["returnDate"] = back
                data = call("sk_flex_departure_calendar", args)
                found = calendar_rows(run, data, window, ns.pax, ns.top or 10)
                print(f"calendar {origin}→{dest}: дней в ответе {len((data.get('chart') or {}).get('points') or [])}, "
                      f"в окне — самых дешёвых {len(found)} (ориентир для выбора дат, не тариф)")
        except Refused as exc:
            print(f"Skiplagged {origin}→{dest}: {exc}; остальные не спрашиваю", file=sys.stderr)
            failed = True
            break
        except Exception as exc:  # noqa: BLE001 — один аэропорт упал, остальные ищутся
            print(f"Skiplagged {origin}→{dest}: {why(exc)}", file=sys.stderr)
            failed = True
            if dest == code and airports_of(code):
                dests += airports_of(code)
                print(f"Skiplagged: код города {code} не принят — ищу по аэропортам {', '.join(airports_of(code))}",
                      file=sys.stderr)
            continue
        rows += found
    return rows, failed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["anywhere", "search", "calendar"])
    ap.add_argument("origin")
    ap.add_argument("rest", nargs="*", help="anywhere: ВЫЛЕТ [ВОЗВРАТ]; search, calendar: КУДА ВЫЛЕТ [ВОЗВРАТ]")
    ap.add_argument("--pax", type=int, default=1, help="взрослых; цена в строке — на всех")
    ap.add_argument("--top", type=int, help="строк: anywhere 20, search 5, calendar 10")
    ap.add_argument("--confirm", type=int, default=0, help="anywhere: N самых дешёвых городов проверить поиском")
    ap.add_argument("--skip-country", help="anywhere: свою страну мимо — как пишет Skiplagged: Poland")
    ap.add_argument("--out-to", help="anywhere --weekends: последняя дата вылета окна")
    ap.add_argument("--nights", help="anywhere --weekends: ночей, 2-3")
    ap.add_argument("--weekends", action="store_true", help="anywhere: пары «на выходные» окна ВЫЛЕТ..--out-to")
    ap.add_argument("--window", help="calendar: окно опроса FROM/TO — поездка целиком в нём")
    ap.add_argument("--max-stops", choices=["none", "one", "many"], help="search: пересадок не больше")
    ap.add_argument("--self-transfer", action="store_true", help="search: и раздельные билеты внутри плеча")
    journal.add_common_args(ap)
    ns = ap.parse_args()
    run = journal.run_id(ns)
    origin = ns.origin.upper()
    pairs = anywhere_pairs(ns, ap) if ns.mode == "anywhere" else None
    if ns.mode != "anywhere" and len(ns.rest) not in (2, 3):
        ap.error(f"{ns.mode}: ORIGIN КУДА ВЫЛЕТ [ВОЗВРАТ]")
    rows, failed = [], False
    try:
        with session() as raw:
            call = Paced(raw)
            rows, failed = ((run_anywhere(ns, run, origin, pairs, call), False) if ns.mode == "anywhere"
                            else run_route(ns, run, origin, call))
    except Exception as exc:  # noqa: BLE001 — сбой источника одной строкой: роль ставит его в --failed
        print(f"Skiplagged: {why(exc)}", file=sys.stderr)
        if not rows:            # сбой при закрытии сессии — собранное пишется
            return 1
    if not rows:                # «выдача пуста» после сбоя читалась бы как «рейсов нет» (ревью 18.10.2026)
        print("Skiplagged: строк нет — причина выше" if failed else "Skiplagged: выдача пуста", file=sys.stderr)
    journal.finish(ns, run, rows)
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
