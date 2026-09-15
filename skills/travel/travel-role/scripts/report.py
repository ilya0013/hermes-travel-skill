#!/usr/bin/env python3
"""Цифры прогона одним вызовом: курс раз, злотые, сумма до двери по вариантам, таблица всех
строк на Диск. Текст для владельца пишет Лиза сама — скрипт печатает только факты.

    HERMES_HOME=/opt/data /opt/hermes/.venv/bin/python3 report.py --run R --list
    HERMES_HOME=/opt/data /opt/hermes/.venv/bin/python3 report.py --run R \\
        --title "WAW→BUD, октябрь, 2–3 ночи" --variant "A=1+3" --variant "B=2" \\
        [--failed wizzair_api="429 бот-гейт"] [--no-drive]

`--list` печатает строки прогона с номерами; вариант — имя и номера (или хвосты id) строк
через `+`; строка CALC-суммы раскладывается на слагаемые. Дорога до аэропорта — строка
`kind=ground` (плюс её `fee`) или живой источник `live_ground` из `profile.yaml`; для домашнего
аэропорта без живой строки — цифра профиля. Плечо дороги дешевле `report.ground_min_pln`
в сумму не входит и называется отдельно. Чужая валюта пересчитывается по курсу НБП один раз
на прогон (строки rate и convert ложатся в журнал); нет курса — сумма без этой строки,
с пометкой. Перевозка прогона строго дешевле самой дешёвой в вариантах называется.
Печатается всегда; Диск не ответил — пометка и путь черновика. Диск — токеном Лизы
(google-workspace), потому питон Hermes, не venv источников.
"""

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import journal  # noqa: E402

PROFILE = HERE.parent / "profile.yaml"
REPORTS = Path(os.environ.get("HERMES_HOME", "/opt/data")) / "travel" / "reports"
FLIGHT_KINDS = ("fare", "other_date")


def ground_sources(profile):
    """Источники живой дороги до аэропорта — из профиля, не из кода."""
    airports = profile.get("home", {}).get("airports", {})
    return {ap["live_ground"] for ap in airports.values() if ap.get("live_ground")}


def is_ground(row, sources):
    return row.get("kind") == "ground" or row.get("source_id") in sources


def ground_min(profile):
    raw = profile.get("report", {}).get("ground_min_pln", 0)
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise SystemExit(f"profile.yaml report.ground_min_pln: нужно число, сейчас {raw!r}")


RUN_SPAN_MAX = datetime.timedelta(hours=3)   # дольше — в run, скорее всего, попали два запроса


def run_span(rows):
    """Первая и последняя метка времени строк прогона."""
    stamps = sorted(datetime.datetime.fromisoformat(r["ts"]) for r in rows if r.get("ts"))
    return (stamps[0], stamps[-1]) if stamps else (None, None)


def load_profile(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def run_rows(path, run):
    """Строки прогона в порядке файла; битые строки считаются, не роняют."""
    rows, broken = [], 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                broken += 1
                continue
            if isinstance(row, dict) and row.get("run") == run and row.get("id"):
                rows.append(row)
    return rows, broken


def pick(token, rows):
    """Номер строки в прогоне (1..n) или хвост id → строка."""
    token = token.strip()
    if token.isdigit():
        n = int(token)
        if not 1 <= n <= len(rows):
            raise SystemExit(f"строки {n} в прогоне нет (строк: {len(rows)})")
        return rows[n - 1]
    hits = [r for r in rows if r["id"].endswith(token)]
    if len(hits) != 1:
        raise SystemExit(f"по «{token}» найдено строк: {len(hits)}, нужна одна")
    return hits[0]


def expand(row, by_id):
    """CALC-сумма → слагаемые (рекурсивно), convert → наблюдение: считаются листья, иначе
    дорога до аэропорта внутри суммы удвоится профилем, а ссылки плеч пропадут."""
    op = row.get("operator")
    if op == "sum":
        return [leaf for i in row.get("inputs") or [] if i in by_id for leaf in expand(by_id[i], by_id)]
    if op == "convert" and row.get("inputs") and row["inputs"][0] in by_id:
        return [by_id[row["inputs"][0]]]
    return [row]


def parse_variant(spec, rows):
    name, _, items = spec.partition("=")
    if not items:
        raise SystemExit(f"вариант без строк: {spec!r} — нужно «A=1+3»")
    by_id = {r["id"]: r for r in rows}
    picked = [leaf for t in items.split("+") for leaf in expand(pick(t, rows), by_id)]
    for r in picked:
        if not isinstance(r.get("value"), (int, float)) or r.get("kind") in ("rate", "diff", "lead"):
            raise SystemExit(f"строка {r['id']} ({r.get('kind')}) в вариант не складывается")
        if r.get("status") == "ORIENTIR":                  # кэш AZair/Aviasales — ориентир, не цена
            raise SystemExit(f"строка {r['id']} ({r.get('source_id')}) ORIENTIR — в вариант не идёт: "
                             "подтверди цену у продавца и сложи его строку")
    return name.strip(), picked


def pln_of(row, rows, run, new_rows, rates, failures):
    """Цена строки в злотых. Чужая валюта: convert из журнала, иначе курс НБП (раз на валюту)."""
    cur = row.get("currency")
    if cur == "PLN":
        return float(row["value"])
    for r in rows + new_rows:
        if r.get("operator") == "convert" and row["id"] in (r.get("inputs") or []):
            return float(r["value"])
    if cur not in rates:
        rate = next((r for r in rows if r.get("kind") == "rate" and r.get("route") == f"{cur}-PLN"), None)
        if rate is None:
            try:
                rate = journal.rate_row(run, cur)
                new_rows.append(rate)
            except Exception as exc:                       # noqa: BLE001 — сеть; причина в пометку
                failures.setdefault("nbp_api", f"{cur}: {type(exc).__name__}")
                rate = False
        rates[cur] = rate
    if not rates[cur]:
        return None
    conv = journal.convert(run, row, rates[cur])
    new_rows.append(conv)
    return float(conv["value"])


def is_api_url(url):
    url = (url or "").lower()
    return "/api/" in url or bool(re.search(r"//([\w-]+\.)*[\w-]*api\.", url))


def link_of(row):
    """Страница продавца: link, иначе url — но не эндпоинт API и не ORIENTIR (кэш, реклама)."""
    if row.get("status") == "ORIENTIR":
        return None
    page = row.get("link") or row.get("url")
    return page if page and not is_api_url(page) else None


def home_ground(variant_rows, profile, pax):
    """Дорога дом↔аэропорт по профилю: плечо × домашний аэропорт × пассажиры.
    В варианте есть строка источника `live_ground` этого аэропорта — профиль для него не
    считается. Цифра профиля ниже порога — не в сумме, а в заметке."""
    airports = profile.get("home", {}).get("airports", {})
    sources = ground_sources(profile)
    live_legs = {}                                     # источник живой дороги → сколько плеч в варианте
    for src in [k[0] for k in {(r.get("source_id"), r.get("route"), r.get("dates"))
                               for r in variant_rows if is_ground(r, sources)}]:
        live_legs[src] = live_legs.get(src, 0) + 1
    legs_by_code = {}
    for r in variant_rows:
        if is_ground(r, sources) or r.get("kind") not in FLIGHT_KINDS:
            continue                                   # доплата и ориентир плеч не добавляют
        segments = (r.get("route") or "").split("/")        # WAW-BUD/BUD-WMI — обратно в другой аэропорт
        if len(segments) == 1 and "/" in (r.get("dates") or ""):
            segments *= 2                                  # туда-обратно теми же аэропортами
        for code in (c for seg in segments for c in seg.split("-")):
            ap = airports.get(code)
            if ap and isinstance(ap.get("ground_pln"), (int, float)):
                legs_by_code[code] = legs_by_code.get(code, 0) + 1
    total, notes, floor = 0.0, [], ground_min(profile)
    for code, legs in legs_by_code.items():
        ap = airports[code]
        legs -= live_legs.get(ap.get("live_ground"), 0)    # живые плечи закрыты строками, остаток — профилем
        if legs <= 0:
            continue
        if ap["ground_pln"] < floor:
            notes.append(f"{ap.get('name', code)}: дешевле порога, не в сумме")
            continue
        total += round(ap["ground_pln"]) * legs * pax        # целыми, как в заметке: «2×4» и есть 8
        notes.append(f"{ap.get('name', code)} {legs}×{round(ap['ground_pln'])}" + (f"×{pax}" if pax > 1 else ""))
    return total, notes


def ground_off_dates(variant_rows, sources):
    """Дорога в варианте на дату, которой нет ни у одного перелёта варианта — заметка, не отказ
    (ночной автобус накануне законен). Блокнот Лизы 15.09.2026: автобус на другой день складывался молча."""
    flight_days = {d for r in variant_rows if r.get("kind") in FLIGHT_KINDS and not is_ground(r, sources)
                   for d in (r.get("dates") or "").split("/")}
    notes = []
    for r in variant_rows:
        if is_ground(r, sources) and flight_days:
            off = [d for d in (r.get("dates") or "").split("/") if d and d not in flight_days]
            if off:
                notes.append(f"{r.get('route')} {r.get('source_id')} на {', '.join(off)} — перелёта в этот день нет")
    return notes


def cheap_ground(variant_rows, plns, profile):
    """id строк живой дороги, чьё плечо (строка + доплаты того же источника, маршрута и дат)
    дешевле порога профиля: в сумму не входят. Возвращает (ids, заметки)."""
    sources, floor = ground_sources(profile), ground_min(profile)
    key_of = {r["id"]: (r.get("source_id"), r.get("route"), r.get("dates"))
              for r in variant_rows if is_ground(r, sources)}
    legs = {}
    for r in variant_rows:
        key = key_of.get(r["id"]) or key_of.get(r.get("of"))    # доплата — по of или тем же ключом
        if key:
            legs.setdefault(key, []).append(r)
    ids, notes = set(), []
    for (src, route, dates), rows in legs.items():
        if any(plns.get(r["id"]) is None for r in rows):
            continue                                   # курса нет — плечо остаётся «без курса», не «дешёвое»
        pax = rows[0].get("pax") or 1                  # строка живой дороги — за всех, порог — на одного
        per_person = sum(plns[r["id"]] for r in rows) / pax
        if per_person < floor:
            ids.update(r["id"] for r in rows)
            # без чисел: «4,40 zł ниже порога» из дайджеста ушло в сообщение владельцу (прогон 15.09.2026)
            note = f"{route} {src}: дешевле порога, не в сумме"
            if note not in notes:                      # два плеча одного маршрута — одна заметка
                notes.append(note)
    return ids, notes


def fact(row, num, pln):
    # злотые — целые: гроши из дайджеста попали в сообщение владельцу (прогон 15.09.2026); таблица точная
    cur = row.get("currency")
    price = f"{round(row['value'])} PLN" if cur == "PLN" else f"{row['value']:.2f} {cur}"
    if cur != "PLN":
        price += " (курса нет)" if pln is None else f" = {round(pln)} PLN"
    line = (f"  {num:>3}  {row.get('kind')} {row.get('route')} {row.get('dates')} {row.get('source_id')} "
            f"{price} {row.get('status')}")
    if row.get("pax") not in (None, 1):
        line += f" pax={row['pax']}"
    if row.get("source_id") == "web_site":
        line += f" «{str(row.get('raw') or '')[:60]}»"
    link = link_of(row)
    return line + (f"\n       {link}" if link else "")


def build(args, rows, profile):
    failures = dict(kv.split("=", 1) if "=" in kv else (kv, "сбой") for kv in (args.failed or []))
    new_rows, rates = [], {}
    variants = [parse_variant(v, rows) for v in args.variant]
    pax = args.pax or profile.get("travelers", {}).get("adults", 1)
    num = {r["id"]: i for i, r in enumerate(rows, 1)}
    results = []
    sources = ground_sources(profile)
    used_flights = []                                  # злотые перевозок, вошедших в варианты
    for name, vrows in variants:
        plns = {r["id"]: pln_of(r, rows, args.run, new_rows, rates, failures) for r in vrows}
        skip, notes = cheap_ground(vrows, plns, profile)
        lines, total, missing = [], 0.0, 0
        for r in vrows:
            pln = plns[r["id"]]
            if pln is None:
                missing += 1
            elif r["id"] in skip:
                continue                               # плечо дешевле порога — в заметке варианта, не строкой
            else:
                total += round(pln)                    # итог — сумма напечатанных целых, иначе «100+100=201»
            lines.append(fact(r, num.get(r["id"], 0), pln))
            if pln is not None and r.get("kind") in FLIGHT_KINDS and not is_ground(r, sources):
                used_flights.append(pln)
        ground, ground_notes = home_ground(vrows, profile, pax)
        total += ground
        results.append({"name": name, "total": total, "missing": missing, "lines": lines, "rows": vrows,
                        "ground": ground, "notes": ground_notes + notes + ground_off_dates(vrows, sources)})

    complete = [v for v in results if not v["missing"]]
    best = min(complete, key=lambda v: v["total"]) if complete else None

    in_variants = {(r.get("route"), r.get("dates"), r.get("value")) for v in results for r in v["rows"]}
    cheapest, used_min = None, min(used_flights, default=None)   # перевозка строго дешевле вошедших в варианты
    for r in rows:
        if r.get("kind") in FLIGHT_KINDS and r.get("source_id") != "CALC" and not is_ground(r, sources) \
                and r.get("status") != "ORIENTIR" and isinstance(r.get("value"), (int, float)) \
                and (r.get("route"), r.get("dates"), r.get("value")) not in in_variants:
            pln = pln_of(r, rows, args.run, new_rows, rates, failures)
            if pln is not None and (cheapest is None or pln < cheapest[0]) \
                    and (used_min is None or pln < used_min):
                cheapest = (pln, r)

    ok = sorted({r.get("source_id") for r in rows if r.get("source_id") not in (None, "CALC", "nbp_api")})
    out = [f"{args.title} · {pax} чел. · цены на {datetime.date.today().isoformat()} · run {args.run}"]
    first, last = run_span(rows)
    if first and last - first > RUN_SPAN_MAX:
        out.append(f"внимание: строки прогона с {first:%d.%m %H:%M} по {last:%d.%m %H:%M} — "
                   "не смешаны ли два запроса в одном run?")
    for v in results:
        head = f"{v['name']} = {v['total']:.0f} PLN до двери"
        if v["missing"]:
            head += f" (без {v['missing']} строк: курса нет)"
        if v["notes"]:
            head += "; дорога: " + ", ".join(v["notes"])
        out.append(head)
        out.extend(v["lines"])
    out.append("самый дешёвый до двери: " + (best["name"] if best else "не определён — нет полной суммы"))
    if cheapest:
        pln, r = cheapest
        out.append(f"дешевле в журнале, не в вариантах: {pln:.0f} PLN строка {num[r['id']]} "
                   f"({r.get('route')} {r.get('dates')} {r.get('source_id')})")
    leads = [r for r in rows if r.get("kind") == "lead" and r.get("source_id") != "CALC"
             and isinstance(r.get("value"), (int, float))]     # convert наследует kind=lead — это не наводка
    if leads:                                          # эвал 15.09.2026: LOT WAW→BKK €558 лежал в журнале и не был назван
        plns = {r["id"]: pln_of(r, rows, args.run, new_rows, rates, failures) for r in leads}   # злотые — курсом НБП
        by_route = {}                                  # по слову совпадения: чужие слова не заслонят нужное
        for r in leads:
            by_route.setdefault(r.get("route"), []).append(r)
        out.append(f"наводки лент (ORIENTIR, цену проверить источником): {len(leads)} строк, до двух на слово:")
        for route, group in by_route.items():
            group.sort(key=lambda x: (plns[x["id"]] is None, plns[x["id"]] or 0))
            for r in group[:2]:
                cur, pln = r.get("currency"), plns[r["id"]]
                price = f"{r['value']:.0f} {cur}" if cur == "PLN" else (
                    f"{r['value']:.0f} {cur}" + (" (курса нет)" if pln is None else f" = {pln:.0f} PLN"))
                out.append(f"  {num[r['id']]:>3}  {route} {price} {r.get('source_id')} «{str(r.get('raw') or '')[:70]}»")
    for r in rows + new_rows:
        if r.get("kind") == "rate":
            out.append(f"курс {r.get('route')}: {r.get('value')} ({r.get('raw')})")
    src = "источники: ✅ " + (", ".join(ok) or "—")
    if failures:
        src += " · ❌ " + ", ".join(f"{k} ({v})" for k, v in failures.items())
    out.append(src)
    return out, results, new_rows


def table(rows, new_rows, pln_by_id):
    head = "| № | источник | маршрут | даты | цена | PLN | статус | пасс. | ссылка | текст источника |\n|---|---|---|---|---|---|---|---|---|---|"
    out = [head]
    for i, r in enumerate(rows + new_rows, 1):
        v = r.get("value")
        price = f"{v:.2f} {r.get('currency')}" if isinstance(v, (int, float)) else "—"
        pln = pln_by_id.get(r["id"])
        link = link_of(r) or r.get("link") or r.get("url") or ""   # у ORIENTIR — страница кэша (связка AZair)
        raw = str(r.get("raw") or "").replace("|", "¦").replace("\n", " ")
        out.append(f"| {i} | {r.get('source_id')} | {r.get('route')} | {r.get('dates')} | {price} | "
                   f"{'' if pln is None else f'{pln:.2f}'} | {r.get('status')} | {r.get('pax')} | "
                   f"{link} | {raw} |")
    return "\n".join(out)


def to_drive(title, text, profile):
    from drive_report import create_doc, find_or_create_folder   # noqa: E402 — импорт google только здесь
    from google_api import build_service                          # noqa: E402

    drive = build_service("drive", "v3")
    folder = find_or_create_folder(drive, profile.get("report", {}).get("drive_folder", "Поездки"))
    return create_doc(drive, folder["id"], title, text).get("webViewLink", "")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--journal", default=journal.JOURNAL)
    ap.add_argument("--profile", default=str(PROFILE))
    ap.add_argument("--list", action="store_true", help="напечатать строки прогона с номерами")
    ap.add_argument("--title", help="маршрут и окно — имя документа")
    ap.add_argument("--variant", action="append", default=[], help="«A=1+3»: имя и строки через +")
    ap.add_argument("--failed", action="append", help="источник, который не ответил: id=причина")
    ap.add_argument("--pax", type=int, help="пассажиров; по умолчанию из профиля")
    ap.add_argument("--no-drive", action="store_true")
    ap.add_argument("--reports-dir", default=str(REPORTS))
    args = ap.parse_args()

    rows, broken = run_rows(args.journal, args.run)
    if not rows:
        sys.exit(f"в журнале {args.journal} нет строк прогона {args.run}")
    if args.list:
        for i, r in enumerate(rows, 1):
            v = r.get("value")
            shown = f"{v:.2f}" if isinstance(v, (int, float)) else "—"
            print(f"{i:>3} {r.get('kind') or '':10} {r.get('source_id') or '':16} {r.get('route') or '':8} "
                  f"{r.get('dates') or '':21} {shown:>9} {r.get('currency') or '':3} {r.get('status') or '':9} "
                  f"{str(r.get('raw') or '')[:60]}")
        print(f"прогон {args.run}: {len(rows)} строк" + (f"; битых строк в файле: {broken}" if broken else ""))
        return 0
    if not args.title or not args.variant:
        ap.error("нужны --title и хотя бы один --variant (или --list)")

    profile = load_profile(args.profile)
    out, results, new_rows = build(args, rows, profile)
    if new_rows:
        journal.append(new_rows, args.journal)
    pln_by_id = {}
    for r in rows + new_rows:
        if r.get("currency") == "PLN" and isinstance(r.get("value"), (int, float)):
            pln_by_id[r["id"]] = float(r["value"])
        if r.get("operator") == "convert" and r.get("inputs"):
            pln_by_id[r["inputs"][0]] = float(r["value"])   # первый вход — наблюдение, второй — курс
    if broken:
        out.append(f"битых строк в журнале: {broken} — в таблицу не вошли")

    sys.path.append(str(Path(os.environ.get("HERMES_HOME", "/opt/data")) / "skills" / "productivity"
                        / "google-workspace" / "scripts"))
    doc_text = f"# {args.title}\n\n" + "\n".join(out) + "\n\n## Все строки прогона\n\n" + table(rows, new_rows, pln_by_id)
    os.makedirs(args.reports_dir, exist_ok=True)
    draft = Path(args.reports_dir) / f"{args.run}.md"
    draft.write_text(doc_text + "\n", encoding="utf-8")

    if args.no_drive:
        out.append(f"таблица: {draft}")
    else:
        try:
            out.append("таблица: " + to_drive(f"{datetime.date.today().isoformat()} {args.title}", doc_text, profile))
        except Exception as exc:                                   # noqa: BLE001 — Диск не должен глушить ответ
            out.append(f"таблица на Диск не загрузилась ({type(exc).__name__}: {str(exc)[:80]}); черновик: {draft}")
    text = "\n".join(out)
    with open(draft, "a", encoding="utf-8") as fh:    # что получила Лиза — след в файле прогона
        fh.write("\n## Напечатано\n\n" + text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
