#!/usr/bin/env python3
# verify_journal.py v2 — проверка журнала наблюдений travel-role.
# Только stdlib. Проверяет то, что решается из самого файла,
# без обращения к источникам.
# Строки без поля "run" считаются архивом: не проверяются, только считаются.
# Использование: python3 verify_journal.py [путь]
# Код возврата: 0 — ошибок нет, 1 — есть, 2 — файл не прочитан.

import json
import re
import sys
from datetime import datetime

KINDS = {"fare", "forecast", "benchmark", "other_date", "fee", "diff"}
RANK = {"ORIENTIR": 0, "QUOTED": 1, "CONFIRMED": 2}
STATUSES = set(RANK) | {"CALC"}
REQUIRED = ["id", "kind", "run", "ts", "source_id", "url", "route", "dates",
            "pax", "value", "currency", "status", "price_prefix", "raw"]
ID_RE = re.compile(r"^(\d{8}T\d{6})-[0-9A-Za-z]{4}$")
RUN_RE = re.compile(r"^\d{8}T\d{6}$")
TOL = 0.011

errs = []


def err(ln, rid, msg):
    errs.append((ln, rid, msg))


def num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def load(path):
    with open(path, encoding="utf-8") as fh:
        raw_lines = fh.readlines()
    rows = []
    for n, line in enumerate(raw_lines, 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            err(n, "?", "не JSON: %s" % e.msg)
            continue
        if not isinstance(obj, dict):
            err(n, "?", "строка не объект JSON")
            continue
        rows.append((n, obj))
    return rows


def check_row(n, o, rid, by_id, run_ids):
    for f in REQUIRED:
        if f not in o:
            err(n, rid, "нет обязательного поля %s" % f)

    m = ID_RE.match(rid) if rid != "?" else None
    if not m and rid != "?":
        err(n, rid, "id не по формату 20260910T140023-abcd")

    ts = o.get("ts")
    dt = None
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            err(n, rid, "ts не ISO 8601: %r" % ts)
        else:
            if dt.utcoffset() is None:
                err(n, rid, "ts без смещения зоны: %r" % ts)
    else:
        err(n, rid, "ts не строка")

    if m and dt is not None:
        compact = dt.strftime("%Y%m%dT%H%M%S")
        if m.group(1) != compact:
            err(n, rid, "префикс id %s не совпадает с ts %s"
                % (m.group(1), compact))

    kind = o.get("kind")
    if kind not in KINDS:
        err(n, rid, "kind вне словаря: %r" % kind)

    status = o.get("status")
    if status not in STATUSES:
        err(n, rid, "status вне словаря: %r" % status)

    if o.get("currency") != "PLN":
        err(n, rid, "currency не PLN: %r" % o.get("currency"))

    if "currency_observed" in o and not isinstance(o["currency_observed"], bool):
        err(n, rid, "currency_observed не булево: %r" % o["currency_observed"])

    val = o.get("value")
    if val is None:
        if kind != "benchmark":
            err(n, rid, "value null при kind=%r (пусто только у benchmark)" % kind)
    elif not num(val):
        err(n, rid, "value не число: %r" % val)

    if o.get("price_prefix") == "od" and status in RANK \
            and RANK[status] > RANK["ORIENTIR"]:
        err(n, rid, "price_prefix 'od' при status=%s" % status)

    url = o.get("url")
    if o.get("source_id") != "CALC" and isinstance(url, str) and url:
        if "?" not in url:
            err(n, rid, "url без параметров запроса: %s" % url)

    has_of = "of" in o
    if kind == "fee" and not has_of:
        err(n, rid, "kind 'fee' без поля of")
    if has_of and kind != "fee":
        err(n, rid, "поле of при kind=%r" % kind)
    if has_of:
        tgt = o["of"]
        if tgt == rid:
            err(n, rid, "of ссылается на саму строку")
        elif tgt not in run_ids:
            err(n, rid, "of не разрешается внутри прогона: %r" % tgt)
        elif by_id[tgt][1].get("status") == "CALC":
            err(n, rid, "of ссылается на строку CALC: %s" % tgt)

    is_calc_src = o.get("source_id") == "CALC"
    is_calc_st = status == "CALC"
    has_inp = "inputs" in o
    if is_calc_src != is_calc_st:
        err(n, rid, "source_id=%r и status=%r рассогласованы"
            % (o.get("source_id"), status))
    if is_calc_st and not has_inp:
        err(n, rid, "status CALC без inputs")
    if has_inp and not is_calc_st:
        err(n, rid, "inputs при status=%r" % status)
    if not has_inp:
        return

    inp = o["inputs"]
    if not isinstance(inp, list):
        err(n, rid, "inputs не список")
        return
    if rid in inp:
        err(n, rid, "inputs ссылается на саму строку")
    missing = [i for i in inp if i not in run_ids]
    if missing:
        err(n, rid, "inputs не разрешаются внутри прогона: %s"
            % ", ".join(map(str, missing)))
        return
    if len(inp) < 2:
        err(n, rid, "inputs содержит меньше двух id")
        return

    for i in inp:
        src = by_id[i][1]
        if src.get("kind") == "fee":
            of = src.get("of")
            if of is not None and of not in inp:
                err(n, rid, "доплата %s наблюдалась на %s, которой нет в inputs"
                    % (i, of))

    vals = [by_id[i][1].get("value") for i in inp]
    if any(not num(v) for v in vals):
        err(n, rid, "среди слагаемых есть строка без числового value")
        return
    if not num(val):
        return

    total = sum(vals)
    op = None
    if abs(total - val) <= TOL:
        op = "sum"
    elif len(vals) == 2 and (abs((vals[0] - vals[1]) - val) <= TOL
                             or abs((vals[1] - vals[0]) - val) <= TOL):
        op = "diff"
    if op is None:
        err(n, rid, "арифметика не сходится: value=%s, сумма слагаемых=%s"
            % (val, round(total, 2)))
        return
    if op == "diff" and kind != "diff":
        err(n, rid, "разность записана как kind=%r, нужно 'diff'" % kind)
    if op == "sum" and kind == "diff":
        err(n, rid, "сумма записана как kind='diff'")


def main(path):
    try:
        rows = load(path)
    except OSError as e:
        print("НЕ ПРОЧИТАН: %s" % e)
        return 2

    by_id = {}
    for n, o in rows:
        rid = o.get("id")
        if not isinstance(rid, str) or not rid:
            err(n, "?", "нет поля id")
            continue
        if rid in by_id:
            err(n, rid, "id не уникален (уже в строке %d)" % by_id[rid][0])
            continue
        by_id[rid] = (n, o)

    runs = {}
    archive = 0
    for n, o in rows:
        r = o.get("run")
        if r is None:
            archive += 1
            continue
        runs.setdefault(r, []).append((n, o))

    for r, items in sorted(runs.items()):
        if not isinstance(r, str) or not RUN_RE.match(r):
            for n, o in items:
                err(n, o.get("id", "?"), "run не по формату 20260910T140023: %r" % r)
            continue
        stamps = []
        for n, o in items:
            m = ID_RE.match(o.get("id", "")) if isinstance(o.get("id"), str) else None
            if m:
                stamps.append(m.group(1))
        if stamps and r != min(stamps):
            n0, o0 = items[0]
            err(n0, o0.get("id", "?"),
                "run %s не равен первой строке прогона %s" % (r, min(stamps)))

        run_ids = set()
        for n, o in items:
            rid = o.get("id")
            if isinstance(rid, str) and rid in by_id and by_id[rid][0] == n:
                run_ids.add(rid)

        for n, o in items:
            rid = o.get("id") if isinstance(o.get("id"), str) and o.get("id") else "?"
            check_row(n, o, rid, by_id, run_ids)

    print("файл: %s" % path)
    print("строк разобрано: %d" % len(rows))
    print("архивных строк без run (не проверялись): %d" % archive)
    print("прогонов проверено: %d (%s)"
          % (len(runs), ", ".join(sorted(map(str, runs))) if runs else "нет"))
    if not errs:
        print("ОШИБОК НЕТ")
        return 0
    print("ОШИБОК: %d" % len(errs))
    for ln, rid, msg in sorted(errs):
        print("  строка %d  id=%s  %s" % (ln, rid, msg))
    return 1


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else "/opt/data/travel/observations.jsonl"
    sys.exit(main(p))
