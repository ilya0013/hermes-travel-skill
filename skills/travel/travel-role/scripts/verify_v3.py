#!/usr/bin/env python3
"""verify_v3 — проверка журнала наблюдений и сверка отчёта с журналом.

Запуск:
    python3 verify_v3.py --journal observations.jsonl [--run RUNID] [--report reports/RUNID.md]

Без --run берётся последний прогон, встреченный в файле.
Без --report слой 4 (сверка текста отчёта) не выполняется.
Код возврата: 0 — ошибок нет, 1 — есть ошибки (предупреждения на код не влияют).
"""

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta

TOL = 0.01
# слагаемое суммы может лежать на сутки вне её дат: ночной автобус к утреннему рейсу
SUM_DATES_SLACK = timedelta(days=1)

SOURCE_VOCAB = {
    "google_flights",
    "google_flights_trains",
    "ryanair_site",
    "ryanair_api",
    "flixbus_api",
    "wizzair_site",
    "wizzair_api",
    "lot_site",
    "kiwi_mcp",
    "azair_site",
    "fly4free_pl_feed",
    "fly4free_com_feed",
    "wakacyjnipiraci_feed",
    "holidaypirates_feed",
    "pepper_feed",
    "travelpayouts_api",
    "nbp_api",
    "CALC",
}

STATUS_VOCAB = {"ORIENTIR", "QUOTED", "CONFIRMED", "CALC"}

DATES_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(/\d{4}-\d{2}-\d{2})?$")

# маркеры «от» в сыром тексте источника
FROM_MARKER_RE = re.compile(
    r"(?<![\w])(od|from|ab|desde|a\s+partir|à\s+partir|от)(?![\w])", re.IGNORECASE
)


class Finding:
    def __init__(self, code, line_no, obj_id, text):
        self.code = code
        self.line_no = line_no
        self.obj_id = obj_id
        self.text = text

    @property
    def is_error(self):
        return self.code.startswith("E")

    def __str__(self):
        where = f"строка {self.line_no}"
        if self.obj_id:
            where += f", id {self.obj_id}"
        return f"{self.code}  {where}: {self.text}"


def load_journal(path, findings):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for n, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as exc:
                findings.append(Finding("E001", n, None, f"строка не разбирается как JSON: {exc}"))
                continue
            if not isinstance(obj, dict):
                findings.append(Finding("E001", n, None, "строка не является объектом JSON"))
                continue
            rows.append((n, obj))
    return rows


def parse_ts(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def date_span(value):
    """`dates` строки → (первый день, последний день); не ISO — None (это E101)."""
    if not isinstance(value, str) or not DATES_RE.match(value):
        return None
    try:
        days = [date.fromisoformat(p) for p in value.split("/")]
    except ValueError:
        return None
    return days[0], days[-1]


def seller_page(row):
    """Страница продавца для [ссылка id]: link (у wizzair_api url — эндпоинт), иначе url.
    ORIENTIR (кэш чужих поисков, link — выдача Aviasales) страницей покупки не бывает."""
    if not row or row.get("status") == "ORIENTIR":
        return None
    return row.get("link") or row.get("url")


def is_api_url(url):
    if not isinstance(url, str):
        return False
    url = url.lower()
    if "/api/" in url:
        return True
    return bool(re.search(r"//([\w-]+\.)*[\w-]*api\.", url))


def as_number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# карта минимумов Wizz — матрица дат, не тариф (14.09.2026: карта 31,99 EUR, Google и сайт 174 zł);
# её число в отчёте — ни само, ни через convert/sum/diff
NOT_A_PRICE_SOURCES = {"wizzair_api"}


def origin_sources(row, by_id, seen=None):
    """source_id наблюдений, из которых сложено число: у CALC — по inputs до листьев."""
    seen = seen if seen is not None else set()
    if not row or row.get("id") in seen:
        return set()
    seen.add(row.get("id"))
    if row.get("source_id") != "CALC":
        return {row.get("source_id")}
    out = set()
    for ref in row.get("inputs") or []:
        out |= origin_sources(by_id.get(ref), by_id, seen)
    return out


# --------------------------------------------------------------------------
# слой 1 — структура строки
# --------------------------------------------------------------------------

def check_structure(rows, run, findings):
    for n, obj in rows:
        oid = obj.get("id")
        if obj.get("run") != run:
            continue

        dates = obj.get("dates")
        if not isinstance(dates, str) or not DATES_RE.match(dates):
            findings.append(Finding("E101", n, oid, f"формат dates не ISO: {dates!r}"))

        src = obj.get("source_id")
        if src not in SOURCE_VOCAB:
            findings.append(Finding("E102", n, oid, f"source_id вне словаря: {src!r}"))
        else:
            url = obj.get("url")
            api = is_api_url(url)
            if src.endswith("_api") and not api:
                findings.append(
                    Finding("E103", n, oid, f"source_id {src} при url не-API: {url}")
                )
            if src.endswith("_site") and api:
                findings.append(
                    Finding("E103", n, oid, f"source_id {src} при url API: {url}")
                )

        if obj.get("status") != "CALC" and not obj.get("url"):
            findings.append(Finding("E104", n, oid, "строка наблюдения без url"))

        value = as_number(obj.get("value"))
        if value is None and not (obj.get("value") is None and obj.get("kind") == "benchmark"):
            findings.append(Finding("E105", n, oid, f"value не число: {obj.get('value')!r}"))


def check_orphans(rows, run, findings):
    stamps = [parse_ts(o.get("ts")) for _, o in rows if o.get("run") == run]
    stamps = [s for s in stamps if s]
    if not stamps:
        return
    start, end = min(stamps), max(stamps)
    for n, obj in rows:
        if obj.get("run") == run:
            continue
        ts = parse_ts(obj.get("ts"))
        if ts and start <= ts <= end:
            other = obj.get("run")
            what = f"чужой run {other!r}" if other else "нет поля run"
            findings.append(
                Finding("E100", n, obj.get("id"), f"строка внутри окна прогона, {what}")
            )


# --------------------------------------------------------------------------
# слой 2 — цена и статус выводятся из raw, а не из полей
# --------------------------------------------------------------------------

def check_price_semantics(rows, run, findings):
    for n, obj in rows:
        if obj.get("run") != run:
            continue
        raw = obj.get("raw")
        if isinstance(raw, str):
            marker = FROM_MARKER_RE.search(raw)
            if marker:
                if obj.get("status") != "ORIENTIR":
                    findings.append(
                        Finding(
                            "E200", n, obj.get("id"),
                            f"в raw маркер «{marker.group(0)}», а status={obj.get('status')!r}",
                        )
                    )
                if not obj.get("price_prefix"):
                    findings.append(
                        Finding(
                            "E201", n, obj.get("id"),
                            f"в raw маркер «{marker.group(0)}», а price_prefix пуст",
                        )
                    )
        if obj.get("currency_observed") is False:
            findings.append(
                Finding("W202", n, obj.get("id"), "валюта из параметра запроса, не наблюдалась")
            )


# --------------------------------------------------------------------------
# слой 3 — арифметика и связи
# --------------------------------------------------------------------------

def check_links(rows, run, findings):
    by_id = {o.get("id"): (n, o) for n, o in rows if o.get("id")}
    run_ids = {o.get("id") for _, o in rows if o.get("run") == run}

    for n, obj in rows:
        if obj.get("run") != run:
            continue
        oid = obj.get("id")
        kind = obj.get("kind")
        status = obj.get("status")

        if kind == "fee":
            of = obj.get("of")
            if not of:
                findings.append(Finding("E305", n, oid, "строка fee без поля of"))
            elif of not in by_id or of not in run_ids:
                findings.append(Finding("E303", n, oid, f"of указывает на неизвестный id {of!r}"))
            else:
                target = by_id[of][1]
                diffs = [
                    f
                    for f in ("route", "dates", "source_id")
                    if obj.get(f) != target.get(f)
                ]
                if diffs:
                    findings.append(
                        Finding("E306", n, oid, f"of ведёт на строку с другим {', '.join(diffs)}")
                    )

        if status != "CALC":
            continue

        inputs = obj.get("inputs")
        if not inputs:
            findings.append(Finding("E300", n, oid, "строка CALC без inputs"))
            continue
        if len(set(inputs)) != len(inputs):
            findings.append(Finding("E308", n, oid, "id повторяется в inputs"))

        operator = obj.get("operator")
        if operator not in ("sum", "diff", "convert"):
            findings.append(Finding("E301", n, oid, f"operator не задан или неизвестен: {operator!r}"))
            # поле не заполнено — арифметику всё равно считаем, по kind
            operator = "diff" if kind == "diff" else "sum"

        missing = [i for i in inputs if i not in by_id or i not in run_ids]
        if missing:
            findings.append(
                Finding("E303", n, oid, f"inputs ссылается за пределы прогона: {', '.join(missing)}")
            )
            continue

        operands = [by_id[i][1] for i in inputs]

        # доплата, привязанная к рейсу, которого нет в этой же сумме
        for op in operands:
            if op.get("kind") == "fee" and op.get("of") and op.get("of") not in inputs:
                findings.append(
                    Finding(
                        "E307", n, oid,
                        f"в inputs доплата {op.get('id')}, а её строка {op.get('of')} в сумму не входит",
                    )
                )

        # слагаемое с других дат: автобус за 26.10 в сумме «до двери» за 20–23.10
        if operator == "sum":
            span = date_span(obj.get("dates"))
            if span:
                for op in operands:
                    op_span = date_span(op.get("dates"))
                    if not op_span:
                        continue
                    # запас в сутки — только автобусу, билет должен лежать в датах суммы
                    slack = SUM_DATES_SLACK if op.get("source_id") == "flixbus_api" else timedelta(0)
                    lo, hi = span[0] - slack, span[1] + slack
                    if op_span[0] < lo or op_span[1] > hi:
                        findings.append(
                            Finding(
                                "E312", n, oid,
                                f"слагаемое {op.get('id')} снято на {op.get('dates')}, сумма — за {obj.get('dates')}",
                            )
                        )

        if kind == "diff":
            if len(operands) != 2:
                findings.append(Finding("E302", n, oid, "diff не с двумя операндами"))
            else:
                mismatched = [
                    f for f in ("route", "dates") if operands[0].get(f) != operands[1].get(f)
                ]
                if mismatched and obj.get("comparable") is not False:
                    findings.append(
                        Finding(
                            "E304", n, oid,
                            f"операнды расходятся по {', '.join(mismatched)}, флага comparable:false нет",
                        )
                    )

        if operator == "convert":
            # наблюдение в чужой валюте × строка rate с парой «валюта наблюдения-PLN» → PLN
            rate = operands[1] if len(operands) == 2 else None
            if (rate is None or rate.get("kind") != "rate"
                    or rate.get("route") != f"{operands[0].get('currency')}-PLN"
                    or obj.get("currency") != "PLN"):
                findings.append(
                    Finding("E311", n, oid, "convert не из наблюдения и строки rate с парой под его валюту")
                )
                continue
        else:
            currencies = {op.get("currency") for op in operands} | {obj.get("currency")}
            if len(currencies) > 1:
                findings.append(
                    Finding("E310", n, oid, f"валюты операндов и строки различаются: {', '.join(sorted(map(str, currencies)))}")
                )
                continue

        values = [as_number(op.get("value")) for op in operands]
        if any(v is None for v in values):
            continue
        value = as_number(obj.get("value"))
        if value is None:
            continue
        if operator == "sum":
            expected = sum(values)
        elif operator == "diff" and len(values) == 2:
            expected = values[0] - values[1]
        elif operator == "convert":
            expected = values[0] * values[1]
        else:
            continue
        if abs(expected - value) > TOL:
            if operator == "diff" and abs(-expected - value) <= TOL:
                findings.append(
                    Finding(
                        "E309", n, oid,
                        f"операнды diff переставлены: {values[0]}-{values[1]} даёт {expected:.2f}",
                    )
                )
            else:
                findings.append(
                    Finding("E302", n, oid, f"пересчёт из inputs даёт {expected:.2f}, в строке {value}")
                )


# --------------------------------------------------------------------------
# слой 4 — отчёт против журнала
# --------------------------------------------------------------------------

NUMBER_RE = re.compile(r"\d[\d \u00a0]*(?:[.,]\d+)?")
TAG_RE = re.compile(r"\s*(?:zł|PLN|zl|€|EUR)?\s*\[([^\]\s]+)\]")
# ссылка на покупку: report_render.py подставляет страницу продавца — link строки (форма Wizz
# у wizzair_api, где url — эндпоинт) или url
LINK_RE = re.compile(r"\[ссылка\s+([^\]\s]+)\]")
# всё до маркера — сообщение человеку, после — приложение; предел строк — на сообщение
MARKER = "--- приложение ---"
MESSAGE_MAX_LINES = 20
# перевозчики, у которых покупают напрямую: ссылка Kiwi на их рейсы — посредник с наценкой
# (владелец, 14.09.2026: «через Kiwi покупать глупо»)
DIRECT_SELLERS = {"wizz air", "ryanair"}
# перевозчик по коду рейса (FR4083, W61412) и по имени в raw → хост его страницы покупки.
# Проверка по данным, не по словам сообщения: прогоны 20260914T192203 и 194706 обходили проверку
# по заголовку — переименовали карточку, убрали « · » и имена перевозчиков из текста
CARRIER_BY_CODE = {"FR": "ryanair.com", "W6": "wizzair.com", "W4": "wizzair.com", "W9": "wizzair.com"}
CARRIER_BY_NAME = {"ryanair": "ryanair.com", "wizz": "wizzair.com"}
# сегменты плеча «WAW→FRA→BCN 10:00→14:00» — пары с перекрытием: (WAW,FRA), (FRA,BCN)
SEGMENT_RE = re.compile(r"\b([A-Z]{3})(?=→([A-Z]{3})\b)")
FLIGHT_RE = re.compile(r"\b([A-Z]\d|[A-Z]{2})(\d{1,4})\b")
# сдвиги дат — цены без ссылок на покупку: заголовок и до двух строк («две-три пары дат», роль);
# больше — уже карточки, и ссылки с них не снимаются
OTHER_DATES_CARD = "Другие даты"
OTHER_DATES_LINES = 3
# домашние аэропорты: «обратно из Модлина» — обратное плечо прилетает в Модлин, а не вылетает
# (живой прогон 20260914T183034, находка владельца)
HOME_BACK_RE = re.compile(r"(?<!туда и )(?<!туда-)обратно\s+из\s+(Модлин\w*|Шопен\w*|Варшав\w*|Окенц\w*|WMI|WAW)",
                          re.IGNORECASE)   # «туда и обратно из Модлина» — круговой билет, верно


def kiwi_carriers(row):
    """Перевозчики строки Kiwi из raw `цена; плечи; Перевозчик+Перевозчик W61411 …; …`; иначе None."""
    if not row or row.get("source_id") != "kiwi_mcp":
        return None
    parts = [p.strip() for p in str(row.get("raw") or "").split(";")]
    if len(parts) < 3:
        return None
    words = [w for w in parts[2].split() if not re.fullmatch(r"[A-Z0-9]{2}\d{1,4}", w)]
    names = {n.strip().lower() for n in " ".join(words).split("+") if n.strip()}
    return names or None


def origin_rows(row, by_id, seen=None):
    """Строки-наблюдения, из которых сложено число: у CALC — по inputs до листьев."""
    seen = seen if seen is not None else set()
    if not row or row.get("id") in seen:
        return []
    seen.add(row.get("id"))
    if row.get("source_id") != "CALC":
        return [row]
    out = []
    for ref in row.get("inputs") or []:
        out += origin_rows(by_id.get(ref), by_id, seen)
    return out


def legs_of(row, host):
    """Плечи строки по route и dates: дата через «/» — туда и обратно."""
    route, dates = str(row.get("route") or ""), str(row.get("dates") or "").split("/")
    if "-" not in route:
        return set()
    a, b = route.split("-", 1)
    out = {(host, route, dates[0])}
    if len(dates) > 1:
        out.add((host, f"{b}-{a}", dates[1]))
    return out


def direct_legs(row):
    """Плечи Wizz/Ryanair в цене: {(хост перевозчика, «ORIG-DEST», дата)}. Kiwi — по плечам и
    кодам рейсов из raw; ryanair_* и google_flights — по маршруту и датам строки."""
    src, raw = row.get("source_id"), str(row.get("raw") or "")
    if src == "kiwi_mcp":
        parts = raw.split(";")
        if len(parts) < 3:
            return set()
        dates = str(row.get("dates") or "").split("/")
        # сегменты по плечам («туда / обратно»), у каждого дата своего плеча; коды рейсов — по сегментам
        segments = [(a, b, dates[min(i, len(dates) - 1)])
                    for i, leg in enumerate(parts[1].split(" / "))
                    for a, b in SEGMENT_RE.findall(leg)]
        hosts = [CARRIER_BY_CODE.get(code) for code, _ in FLIGHT_RE.findall(parts[2])]
        if len(hosts) != len(segments):
            hosts = [hosts[0] if len(set(hosts)) == 1 else None] * len(segments)
        if None in hosts:
            return set()   # связка с другим перевозчиком — товар Kiwi, как в E408
        return {(h, f"{a}-{b}", d) for (a, b, d), h in zip(segments, hosts)}
    if src in ("ryanair_api", "ryanair_site"):
        return legs_of(row, "ryanair.com")
    if src == "google_flights":
        for name, host in CARRIER_BY_NAME.items():
            if name in raw.lower():
                return legs_of(row, host)
    return set()


def check_message_form(message, by_id, findings):
    """Слой 4, форма сообщения: у каждого плеча Wizz/Ryanair в показанной цене — ссылка на сайт
    перевозчика по тому же маршруту и дате; цена Kiwi за рейсы только Wizz/Ryanair в сообщение не идёт
    (Kiwi не продавец прямых перевозчиков); «обратно из <дом>»."""
    covered = set()
    for m in LINK_RE.finditer(message):
        row = by_id.get(m.group(1)) or {}
        page = str(seller_page(row) or "").lower()
        for host in set(CARRIER_BY_CODE.values()):
            if host in page:
                covered |= legs_of(row, host)
    required = {}
    reseller = {}
    skip = 0
    for line in message.splitlines():
        if line.strip().strip("*").startswith(OTHER_DATES_CARD):
            skip = OTHER_DATES_LINES
        if not line.strip():
            skip = 0   # пустая строка закрывает блок сдвигов
        shifted = skip > 0   # сдвиги дат — без ссылок на покупку (роль, «Другие даты»), но не Kiwi
        if skip:
            skip -= 1
        for ref in re.findall(r"\[(?!ссылка)([^\]\s]+)\]", line):
            for leaf in origin_rows(by_id.get(ref), by_id):
                legs = direct_legs(leaf)
                # товар Kiwi — только связка с названным в raw чужим перевозчиком; без кодов и имён — перепродажа
                carriers = kiwi_carriers(leaf)
                if leaf.get("source_id") == "kiwi_mcp" and (legs or not carriers or carriers <= DIRECT_SELLERS):
                    reseller.setdefault(leaf.get("id"), ref)
                elif not shifted:
                    for leg in legs:
                        required.setdefault(leg, ref)
    for (host, route, date), ref in sorted(required.items()):
        if (host, route, date) not in covered:
            findings.append(Finding("E411", 0, ref,
                                    f"плечо {host.split('.')[0]} {route} {date} в цене — нет [ссылка] на "
                                    f"{host} по этому маршруту и дате (Google и Kiwi не продавцы)"))
    for leaf_id, ref in sorted(reseller.items()):
        findings.append(Finding("E412", 0, ref,
                                f"в цене строка Kiwi {leaf_id} за рейсы только Wizz/Ryanair — Kiwi не продавец "
                                f"прямых перевозчиков: цена плеча у перевозчика (Ryanair — ryanair_api/ryanair_site, "
                                f"Wizz — google_flights --airline), выдача Kiwi — в приложение"))
    for m in LINK_RE.finditer(message):
        carriers = kiwi_carriers(by_id.get(m.group(1)))
        if carriers and carriers <= DIRECT_SELLERS:
            findings.append(Finding("E408", 0, m.group(1),
                                    f"ссылка Kiwi на рейсы {', '.join(sorted(carriers))} — посредник; "
                                    f"покупка у перевозчика: страница Ryanair, форма Wizz"))
    for m in HOME_BACK_RE.finditer(message):
        findings.append(Finding("E410", 0, None,
                                f"«{m.group(0)}»: обратное плечо прилетает в аэропорт — «обратно в …»"))


def split_message(text):
    """(сообщение, приложение) по первой строке-маркеру; без маркера всё — сообщение."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == MARKER:
            return "\n".join(lines[:i]), "\n".join(lines[i + 1:])
    return text, ""


MONTHS = (
    r"янв\w*|фев\w*|мар\w*|апр\w*|ма[йя]\w*|июн\w*|июл\w*|авг\w*|сен\w*|окт\w*|ноя\w*|дек\w*|"
    r"stycz\w*|lut\w*|marc\w*|kwiet\w*|maj\w*|czerw\w*|lip\w*|sierp\w*|wrze\w*|paźdz\w*|paz\w*|listop\w*|grud\w*"
)

MASK_PATTERNS = [
    r"\[[^\]]*\]",                                   # сами ссылки на id
    r"\b\d{1,2}:\d{2}\b",                            # время
    r"\b\d{4}-\d{2}-\d{2}\b",                        # ISO-дата
    r"\b\d{1,2}\.\d{1,2}(\.\d{2,4})?\b",             # дата 02.10
    r"\b(?-i:[A-Z]{2})\s?\d{2,4}\b",                 # номер рейса; строчные «od 130», «za 299» — не рейс
    r"\b[^\W\d_]+\d\w*",                             # слово с цифрами внутри: fly4free, W61411, A320
    r"\d+\s*[×xх]\s*\d+(\s*[×xх]\s*\d+)?",           # габариты
    # «3–4 ночи», «1 взр.», «2 чел», «5 miejsc» — так пишет образец роли и выдача продавца;
    # без масок Лиза корёжила текст в «три–четыре ночи» (её заметки 13–14.09.2026)
    r"\b\d+(\s*[–—-]\s*\d+)?\s*(кг|kg|км|km|мин|min|ч|godz|godzin\w*|часа|часов|дн\w*|дней|сут\w*|ноч\w*)\b\.?",
    r"\b\d+(\s*[–—-]\s*\d+)?\s*(взросл\w*|взр\.?|реб\w*|чел\w*|пассажир\w*|adult\w*|osob\w*|мест\w*|miejsc\w*)",
    r"\b\d{8}T\d{6}\b",                              # id прогона в приложении
    r"\d+\s*%",                                      # проценты — не цена
    r"\b\d{1,2}\s*[–—-]\s*\d{1,2}\s+(" + MONTHS + r")",
    r"\b\d{1,2}\s+(" + MONTHS + r")",
    r"\b(19|20)\d{2}\b",                             # год
    r"±\s*\d+",
    r"\b(вариант|wariant|опция|option|шаг|пункт|вар\.)\s*\d+",
    r"\bv\d+\b",
]


def build_mask(text):
    mask = [False] * len(text)
    for pat in MASK_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            for i in range(m.start(), m.end()):
                mask[i] = True
    return mask


def check_report(report_path, rows, run, findings):
    with open(report_path, encoding="utf-8") as fh:
        text = fh.read()

    by_id = {o.get("id"): o for _, o in rows if o.get("id")}
    run_ids = {o.get("id") for _, o in rows if o.get("run") == run}
    used_ids = set()

    message, _ = split_message(text)
    message_lines = [ln for ln in message.splitlines() if ln.strip()]
    if len(message_lines) > MESSAGE_MAX_LINES:
        findings.append(
            Finding("E405", 0, None,
                    f"сообщение до маркера «{MARKER}» — {len(message_lines)} строк, "
                    f"предел {MESSAGE_MAX_LINES}: остальное в приложение")
        )
    check_message_form(message, by_id, findings)

    for line_no, line in enumerate(text.splitlines(), 1):
        for m in LINK_RE.finditer(line):
            ref = m.group(1)
            src = by_id.get(ref)
            page = seller_page(src)
            if ref not in run_ids or not page or is_api_url(page):
                findings.append(
                    Finding("E406", line_no, ref, "ссылка не ведёт на строку прогона со страницей продавца "
                                                  "(link и url пусты, адрес API или строка ORIENTIR)")
                )
            else:
                used_ids.add(ref)
        mask = build_mask(line)
        line_ids = []
        for m in NUMBER_RE.finditer(line):
            if any(mask[i] for i in range(m.start(), m.end())):
                continue
            tag = TAG_RE.match(line, m.end())
            if not tag:
                findings.append(
                    Finding("E400", line_no, None, f"число без ссылки на id: {m.group(0).strip()!r}")
                )
                continue
            ref = tag.group(1)
            used_ids.add(ref)
            line_ids.append(ref)
            if ref not in by_id or ref not in run_ids:
                findings.append(
                    Finding("E401", line_no, ref, "ссылка не резолвится в строку этого прогона")
                )
                continue
            src = by_id[ref]
            shown_text = m.group(0).replace("\u00a0", "").replace(" ", "")
            shown = float(shown_text.replace(",", "."))
            value = as_number(src.get("value"))
            # целое в тексте — округление до злотого (237 за 236,89); с копейками — точно
            tol = TOL if ("," in shown_text or "." in shown_text) else 0.5
            if value is None or abs(value - shown) > tol:
                findings.append(
                    Finding("E402", line_no, ref, f"в тексте {shown}, в журнале {src.get('value')}")
                )
            bad = origin_sources(src, by_id) & NOT_A_PRICE_SOURCES
            if bad:
                findings.append(
                    Finding("E407", line_no, ref,
                            f"число из карты минимумов Wizz ({', '.join(sorted(bad))}) — карта не тариф; "
                            f"цена плеча Wizz — строка google_flights на это плечо и дату")
                )

        labels = {w for w in STATUS_VOCAB if re.search(rf"\b{w}\b", line)}
        if labels and line_ids:
            for ref in line_ids:
                src = by_id.get(ref)
                if src and src.get("status") not in labels:
                    findings.append(
                        Finding(
                            "E403", line_no, ref,
                            f"в тексте ярлык {'/'.join(sorted(labels))}, в журнале {src.get('status')}",
                        )
                    )

    unused = {}
    for _, obj in rows:
        if obj.get("run") != run or obj.get("status") != "QUOTED":
            continue
        oid = obj.get("id")
        if oid in used_ids:
            continue
        cited = any(
            o.get("run") == run and oid in (o.get("inputs") or []) for _, o in rows
        )
        if not cited:
            unused[obj.get("source_id")] = unused.get(obj.get("source_id"), 0) + 1
    if unused:
        by_source = ", ".join(f"{k} {v}" for k, v in sorted(unused.items(), key=lambda kv: -kv[1]))
        findings.append(
            Finding("W404", 0, None,
                    f"{sum(unused.values())} наблюдений прогона не вошли ни в отчёт, ни в расчёты "
                    f"({by_source}) — справка, не требование их вывести")
        )


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", required=True)
    ap.add_argument("--run")
    ap.add_argument("--report")
    ap.add_argument("--codes-only", action="store_true", help="печатать только коды, для тестов")
    args = ap.parse_args()

    findings = []
    rows = load_journal(args.journal, findings)

    run = args.run
    if not run:
        seen = [o.get("run") for _, o in rows if o.get("run")]
        run = seen[-1] if seen else None
    if not run:
        print("не найдено ни одной строки с полем run — прогон не определён")
        return 2

    run_rows = [(n, o) for n, o in rows if o.get("run") == run]

    check_structure(rows, run, findings)
    check_orphans(rows, run, findings)
    check_price_semantics(rows, run, findings)
    check_links(rows, run, findings)
    if args.report:
        check_report(args.report, rows, run, findings)

    errors = [f for f in findings if f.is_error]
    warns = [f for f in findings if not f.is_error]

    if args.codes_only:
        for f in findings:
            print(f.code)
        return 1 if errors else 0

    print(f"файл: {args.journal}")
    print(f"прогон: {run} — строк в прогоне: {len(run_rows)}, всего строк: {len(rows)}")
    if args.report:
        print(f"отчёт: {args.report}")
    print()
    for f in sorted(findings, key=lambda x: (x.line_no, x.code)):
        print(f)
    print()
    if errors:
        print(f"ОШИБОК: {len(errors)}, предупреждений: {len(warns)}")
    else:
        print(f"ОШИБОК НЕТ, предупреждений: {len(warns)}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
