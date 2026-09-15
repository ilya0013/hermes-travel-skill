"""Общий модуль скриптов-источников: строка журнала наблюдений собирается здесь,
а не руками роли. Контракт полей — references/journal-contract.md.

Скрипт-источник получает данные, зовёт observation() на каждое наблюдение и
finish(): она либо дописывает строки в журнал, либо (--dry-run) только печатает.
id и ts ставятся здесь, run — из --run или ts первой строки прогона.
"""

import json
import os
import random
import string
import urllib.request
from datetime import datetime

# Маркер рекламного «от» берётся из verify_v3 (E200/E201): один регэксп на запись и проверку.
from verify_v3 import FROM_MARKER_RE

# журнал — под HERMES_HOME, как reports у report.py: роль ставится другому агенту (воркшоп 16.09.2026)
JOURNAL = os.path.join(os.environ.get("HERMES_HOME", "/opt/data"), "travel", "observations.jsonl")
NBP_RATE_URL = "https://api.nbp.pl/api/exchangerates/rates/a/{code}/?format=json"
# публичные API перевозчиков (Ryanair, Flixbus) отвечают только браузерному User-Agent
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

_ID_ALPHABET = string.digits + string.ascii_lowercase


def now():
    """Локальное время с зоной, секунды: '2026-09-13T07:55:37+00:00'."""
    return datetime.now().astimezone().replace(microsecond=0)


def compact(ts):
    return ts.strftime("%Y%m%dT%H%M%S")


def new_id(ts):
    return f"{compact(ts)}-{''.join(random.choices(_ID_ALPHABET, k=4))}"


def observation(run, kind, source_id, url, route, dates, value, currency, status, raw,
                pax=1, price_prefix=None, **extra):
    """Строка журнала в порядке полей контракта; ts и id — момент вызова."""
    ts = now()
    row = {
        "id": new_id(ts),
        "kind": kind,
        "run": run,
        "ts": ts.isoformat(),
        "source_id": source_id,
        "url": url,
        "route": route,
        "dates": dates,
        "pax": pax,
        "value": value,
        "currency": currency,
        "status": status,
        "price_prefix": price_prefix,
        "raw": raw,
    }
    row.update(extra)
    return row


def calc(run, kind, route, dates, operator, inputs, raw, pax=1, **extra):
    """Строка CALC: value считается из строк-операндов, не передаётся."""
    values = [r["value"] for r in inputs]
    currency = inputs[0]["currency"]
    if operator == "sum":
        value = round(sum(values), 2)
    elif operator == "diff":
        if len(values) != 2:
            raise ValueError("diff требует ровно два операнда")
        value = round(values[0] - values[1], 2)
    elif operator == "convert":
        # наблюдение в чужой валюте × строка rate с парой «валюта наблюдения-PLN»
        if len(inputs) != 2 or inputs[1].get("kind") != "rate":
            raise ValueError("convert требует наблюдение и строку rate")
        if inputs[1]["route"] != f"{currency}-PLN":
            raise ValueError(f"курс {inputs[1]['route']} не для валюты {currency}")
        value = round(values[0] * values[1], 2)
        currency = "PLN"
    else:
        raise ValueError(f"operator неизвестен: {operator!r}")
    return observation(
        run, kind, "CALC", None, route, dates, value, currency, "CALC", raw,
        pax=pax, operator=operator, inputs=[r["id"] for r in inputs], **extra,
    )


def rate_row(run, code):
    """Курс валюты к злотому: таблица A НБП (средний курс), публичный API без ключа.
    Проверено 13.09.2026: api.nbp.pl/en.html, ответ {"code","rates":[{"no","effectiveDate","mid"}]}."""
    url = NBP_RATE_URL.format(code=code.lower())
    with urllib.request.urlopen(url, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    rate = data["rates"][0]
    raw = f"NBP {rate['no']} {rate['effectiveDate']} {data['code']} mid {rate['mid']}"
    return observation(run, "rate", "nbp_api", url, f"{data['code']}-PLN", rate["effectiveDate"],
                       float(rate["mid"]), "PLN", "QUOTED", raw, pax=None)


def convert(run, row, rate):
    """Строка convert для наблюдения row по курсу rate; kind, route и dates — наблюдения."""
    return calc(run, row["kind"], row["route"], row["dates"], "convert", [row, rate],
                f"{row['value']:.2f} {row['currency']} × {rate['value']}", pax=row.get("pax", 1))


def price_prefix_of(raw):
    """Маркер «от» из текста источника, как его увидит verify_v3, иначе None."""
    m = FROM_MARKER_RE.search(raw or "")
    return m.group(0) if m else None


def add_common_args(parser):
    parser.add_argument("--run", help="id прогона; без него — ts первой строки, печатается")
    parser.add_argument("--journal", default=JOURNAL, help=f"файл журнала (по умолчанию {JOURNAL})")
    parser.add_argument("--dry-run", action="store_true", help="только напечатать, в журнал не писать")


def run_id(args):
    return args.run or compact(now())


def append(rows, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def describe(row):
    value = row["value"]
    shown = f"{value:.2f}" if isinstance(value, float) else str(value)
    return (
        f"{row['id']}  {row['kind']:<10} {row['route']:<9} {row['dates']:<21} "
        f"{shown:>9} {row['currency']}  {row['status']:<8} {row['raw']}"
    )


def finish(args, run, rows):
    """Запись в журнал (если не --dry-run) и печать строк с их id — сначала запись:
    оборванный вывод не должен терять строки."""
    if not args.dry_run:
        append(rows, args.journal)
    for row in rows:
        print(describe(row))
    if args.dry_run:
        print(f"run: {run}; --dry-run — в журнал не записано ({len(rows)} строк)")
        return
    print(f"run: {run}; записано строк: {len(rows)} → {args.journal}")


def load_rows(path, run=None):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if run is None or row.get("run") == run:
                rows.append(row)
    return rows


def iso_to_ddmmyyyy(iso):
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y")


def parse_args_file(value):
    """Аргументы JSON строкой или `@путь` к файлу — кавычки через ssh/docker не доходят."""
    if value.startswith("@"):
        with open(value[1:], encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(value)
