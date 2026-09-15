#!/usr/bin/env python3
"""AZair: связки лоукостеров по окну дат — серверный HTML без ключа и без JS, PLN.

    /opt/data/travel/lib/venv/bin/python azair_search.py WAW BCN --out-from 2026-10-01 --out-to 2026-10-31 \\
        --nights 2-4 [--extra-from WMI] [--extra-to GRO] [--max-changes 1] [--adults 1] [--top 5] [--one-way]

Аэропорты — кодами IATA: серверу нужен только код в скобках (`X [WAW] (+WMI)` — оба аэропорта,
проверено 15.09.2026), имя он не читает. Словарь AZair — 304 аэропорта Европы, Средиземноморья и
Ближнего Востока (Дубай, Стамбул есть; Бангкока, Сингапура, Дохи нет): дальний маршрут даёт 0 строк.
`--nights` — ночи; AZair считает дни включительно (1 ночь = «2 days»), скрипт переводит сам.
Каждая строка — вся связка одной ценой (`tp` из разметки), плечи с перевозчиком, рейсом и ценой в
`raw`; связка из нескольких билетов — `self_transfer_allowed: true`. Цена — кэш AZair, в `raw`
возраст проверки («checked 193 h ago»), потому статус ORIENTIR: живую цену подтверждает перевозчик
(`ryanair_fares.py`, Google `--airline`). Связка, где хоть одно плечо проверялось давнее
`--max-age-h` (14 суток; живьём 15.09.2026 встретилось плечо easyJet 5355 h) или без отметки
проверки, не пишется. `--one-way`: те же параметры с `isOneway=oneway`, `--out-to` — вылет не позже
(проверено 15.09.2026: WAW+WMI→BCN октябрь — 77 связок, даты 02.10–21.10).
`link` — постоянная ссылка на эту связку (`flightsID`): она идёт в таблицу отчёта, в сообщение
ORIENTIR не попадает; покупка — по кнопке у каждого плеча, редиректом на сайт перевозчика.
"""

import argparse
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request

import journal

SOURCE_ID = "azair_site"
BASE = "https://www.azair.eu/"
SEARCH = BASE + "azfin.php"

RESULT_RE = re.compile(r'<div class="result[ "]', re.I)
TP_RE = re.compile(r"""\btp=(["'])(.*?)\1""")                                  # в сыром HTML — одинарные кавычки
DATE_RE = re.compile(r'<span class="date">\w{2,3} (\d\d)/(\d\d)/(\d\d)</span>')
# плечо — один <p> внутри div.detail; каждое поле ищется отдельно, чтобы плечо без ссылки
# flightradar или без отметки проверки не склеивалось со следующим
LEG_P_RE = re.compile(r"<p>(.*?)</p>", re.S)
FROM_RE = re.compile(r'class="from"[^>]*>(?:\w\w&nbsp;)?(\d\d:\d\d)&nbsp;[^<]*<span class="code">(\w{3})')
TO_RE = re.compile(r'class="to"[^>]*>(\d\d:\d\d)&nbsp;[^<]*<span class="code">(\w{3})')
FLIGHT_RE = re.compile(r'flightradar24\.com/data/flights/\w+"[^>]*>([A-Z0-9]{2}(?:\s|&nbsp;)?\d{1,4})</a>')
AIRLINE_RE = re.compile(r'class="airline iata\w*">([^<]*)</span>')
AGE_RE = re.compile(r'data-age="(\d+)"')
PRICE_RE = re.compile(r'class="legPrice">((?:\d|&nbsp;| )+) (zł|€|[A-Z]{3})')
BOOKMARK_RE = re.compile(r'<div class="bookmark"[^>]*>\s*<a href="([^"]+)"')
BLOCK_END = '<div class="close">'                                            # дальше — хвост страницы


def search_url(origin, dest, extra_from, extra_to, out_from, out_to, nights, one_way, max_changes, adults):
    def place(code, extras):
        return f"{code} [{code}]" + (f" (+{','.join(extras)})" if extras else "")
    lo, hi = (0, 0) if one_way else nights
    params = {
        "searchtype": "flexi", "tp": 0, "isOneway": "oneway" if one_way else "return",
        "srcAirport": place(origin, extra_from), "srcTypedText": "", "srcFreeTypedText": "", "srcMC": "",
        "dstAirport": place(dest, extra_to), "dstTypedText": "", "dstFreeTypedText": "", "dstMC": "",
        "depmonth": out_from[:7].replace("-", ""), "depdate": out_from, "aid": 0,
        "arrmonth": out_to[:7].replace("-", ""), "arrdate": out_to,
        "minDaysStay": lo + 1, "maxDaysStay": hi + 1,                      # ночи → дни включительно
        "samedep": "true", "samearr": "true",
        "minHourStay": "0:45", "maxHourStay": "23:20", "minHourOutbound": "0:00", "maxHourOutbound": "24:00",
        "minHourInbound": "0:00", "maxHourInbound": "24:00",
        "autoprice": "true", "adults": adults, "children": 0, "infants": 0, "maxChng": max_changes,
        "currency": "PLN", "lang": "en", "indexSubmit": "Search",
    }
    for i in range(7):
        params[f"dep{i}"] = params[f"arr{i}"] = "true"
    return SEARCH + "?" + urllib.parse.urlencode(params)


def fetch(url):
    """Страница выдачи; пустая выдача один раз повторяется: 15.09.2026 один one-way ответил
    без результатов, тот же запрос через секунды — 77 связок."""
    req = urllib.request.Request(url, headers={"User-Agent": journal.BROWSER_UA, "Accept-Language": "en"})
    for attempt in (1, 2):
        with urllib.request.urlopen(req, timeout=60) as resp:
            page = resp.read().decode("utf-8", "replace")
        if RESULT_RE.search(page) or attempt == 2:
            return page
        time.sleep(3)


def leg_of(chunk):
    """Плечо из своего <p>: без вылета/прилёта это не плечо (None); рейс и возраст — если есть."""
    a, b = FROM_RE.search(chunk), TO_RE.search(chunk)
    if not a or not b:
        return None
    flight, airline, age, price = FLIGHT_RE.search(chunk), AIRLINE_RE.search(chunk), AGE_RE.search(chunk), PRICE_RE.search(chunk)
    return {"dep": a[1], "from": a[2], "arr": b[1], "to": b[2],
            "flight": re.sub(r"\s|&nbsp;", "", flight[1]) if flight else "?",
            "airline": html.unescape(airline[1]).strip() if airline else "?",
            "age_h": int(age[1]) if age else None,
            "price": float(re.sub(r"&nbsp;| ", "", price[1])) if price else None,
            "currency": ("PLN" if price[2] == "zł" else price[2]) if price else None}


def legs_of(chunk):
    return [leg for leg in map(leg_of, LEG_P_RE.findall(chunk)) if leg]


def iso_date(m):
    return f"20{m[2]}-{m[1]}-{m[0]}"


def parse_results(page):
    """Связки со страницы выдачи: цена tp, даты, плечи туда и обратно, постоянная ссылка."""
    page = re.sub(r"\s+", " ", page)
    out = []
    for block in RESULT_RE.split(page)[1:]:
        block = block.split(BLOCK_END, 1)[0]
        tp = TP_RE.search(block)
        dates = DATE_RE.findall(block)
        if not tp or not dates:
            continue
        total = json.loads(html.unescape(tp.group(2)))
        cut = block.find('class="caption sem"')                             # «Back» — начало обратной части
        there, back = (block, "") if cut < 0 else (block[:cut], block[cut:])
        bm = BOOKMARK_RE.search(block)
        out.append({
            "total": float(total["tp"]), "out_date": iso_date(dates[0]),
            "back_date": iso_date(dates[1]) if len(dates) > 1 and back else None,
            "there": legs_of(there), "back": legs_of(back),
            "link": urllib.parse.urljoin(BASE, html.unescape(bm.group(1))) if bm else None,
        })
    return out


def describe_legs(legs):
    return " > ".join(f"{l['dep']} {l['from']}-{l['to']} {l['arr']} {l['airline']} {l['flight']} "
                      + ("?" if l["price"] is None else f"{l['price']:g}") for l in legs)


def age_of(legs):
    """Самая старая проверка среди плеч; плечо без отметки считается непроверенным (None)."""
    ages = [l["age_h"] for l in legs]
    return None if any(a is None for a in ages) else max(ages)


def rows_for_results(run, results, url, adults, top, max_age_h):
    rows, stale = [], 0
    for r in sorted(results, key=lambda r: r["total"]):
        legs = r["there"] + r["back"]
        if not r["there"]:
            continue
        age = age_of(legs)
        if age is None or age > max_age_h:
            stale += 1
            continue
        if len(rows) == top:
            break
        currency = {l["currency"] for l in legs}
        route = f"{r['there'][0]['from']}-{r['there'][-1]['to']}"
        dates = r["out_date"] + (f"/{r['back_date']}" if r["back_date"] else "")
        raw = (f"AZair {route} there {r['out_date']}: {describe_legs(r['there'])}"
               + (f"; back {r['back_date']}: {describe_legs(r['back'])}" if r["back"] else "")
               + f"; total {r['total']:g} zł ({adults} adult); checked {age} h ago"
               + ("; separate tickets" if len(legs) > (2 if r["back"] else 1) else ""))
        rows.append(journal.observation(
            run, "fare", SOURCE_ID, url, route, dates, r["total"], "PLN", "ORIENTIR", raw, pax=adults,
            link=r["link"] or url, currency_observed=(currency == {"PLN"}),
            self_transfer_allowed=len(legs) > (2 if r["back"] else 1),
        ))
    return rows, stale


def nights_arg(value):
    lo, _, hi = value.partition("-")
    return int(lo), int(hi or lo)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("origin", help="код IATA: WAW")
    ap.add_argument("dest", help="код IATA: BCN")
    ap.add_argument("--extra-from", default="", help="ещё аэропорты вылета через запятую: WMI")
    ap.add_argument("--extra-to", default="", help="ещё аэропорты прилёта: GRO,REU")
    ap.add_argument("--out-from", required=True, help="вылет не раньше, ISO")
    ap.add_argument("--out-to", required=True, help="возврат (у --one-way — вылет) не позже, ISO")
    ap.add_argument("--nights", type=nights_arg, default=(0, 0), help="ночей на месте: 2-4")
    ap.add_argument("--one-way", action="store_true")
    ap.add_argument("--max-changes", type=int, default=1, choices=(0, 1, 2, 3))
    ap.add_argument("--adults", type=int, default=1)
    ap.add_argument("--top", type=int, default=5, help="сколько самых дешёвых связок писать")
    ap.add_argument("--max-age-h", type=int, default=14 * 24, help="цена проверялась не давнее, часов")
    journal.add_common_args(ap)
    args = ap.parse_args()
    run = journal.run_id(args)
    if not args.one_way and args.nights == (0, 0):
        ap.error("--nights обязателен для round-trip")

    url = search_url(args.origin, args.dest, [c for c in args.extra_from.split(",") if c],
                     [c for c in args.extra_to.split(",") if c], args.out_from, args.out_to,
                     args.nights, args.one_way, args.max_changes, args.adults)
    results = parse_results(fetch(url))
    rows, stale = rows_for_results(run, results, url, args.adults, args.top, args.max_age_h)
    print(f"AZair {args.origin}->{args.dest} {args.out_from}..{args.out_to}: связок {len(results)}, "
          f"с ценой давнее {args.max_age_h} h: {stale}, пишу {len(rows)}", file=sys.stderr)
    journal.finish(args, run, rows)
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
