#!/usr/bin/env python3
"""Письма рассылок перевозчиков → ярлык `avia-sale` и вон из Входящих.

    HERMES_HOME=/opt/data /opt/hermes/.venv/bin/python3 mail_sales.py [--days 3] [--label avia-sale] [--dry-run]
        [--parse]

Владелец подписан на рассылки перевозчиков на свой Gmail. Фильтры через Gmail API требуют права
`gmail.settings.basic`, которого в токене агента нет — потому уборку делает код правом `gmail.modify`:
письма с доменов `SENDERS` за `--days` дней, ещё без ярлыка, получают ярлык и теряют `INBOX` одним
`batchModify`. Ярлык ищется по имени, нет — создаётся.
Cron Hermes зовёт каждый час без агента (обёртка `scripts/travel/mail_sales.sh` в HERMES_HOME, доставка
`local`): stdout — одна строка «сколько и от кого», пустой — нечего убирать; ошибка — код 1 и stderr.
`--parse` — разбор писем под ярлыком за `--days` дней, ещё не разобранных: «продажа до», окно полёта и скидка
вынимаются по числам (письма на польском и английском), строка письма — в `travel/mail/offers.jsonl`
(`deal_feeds.py` читает оттуда цены по городам как ленту `mail`), акция — строкой в Sheet «Распродажи».
Ходит токеном агента через `build_service` скилла google-workspace — потому питон Hermes.
"""

import argparse
import base64
import calendar
import collections
import datetime
import html
import json
import os
import re
import subprocess
import sys
import zoneinfo
from pathlib import Path

import deal_feeds

LABEL = "avia-sale"
# Домены рассылок ≠ основные домены перевозчиков (`e.emirates.email`, `services.ryanairemails.com`) —
# список по реальным письмам-подтверждениям 18.09.2026; новый перевозчик = новая строка здесь.
# Ключ — слово перевозчика, как в `deal_feeds.NOTABLE` (колонка «слово» таблицы «Распродажи»).
SENDERS = {
    "finnair": ("email.finnair.com",),
    "lot": ("mailing.lot.com",),
    "qatar": ("info.qatarairways.com",),
    # 01.10.2026: с того же домена `onlineticket@` прислал код входа (18.09 ушёл под ярлык) — оттуда же идут
    # билеты; адрес рассылки Turkish ещё неизвестен, взят адрес письма о согласии на рассылку
    "turkish": ("mail@mail.turkishairlines.com",),
    "thai": ("tg.thaiairways.com", "thaiairways.com"),
    "aegean": ("news.aegeanair.com",),
    "emirates": ("e.emirates.email",),
    "ana": ("mail.ana.co.jp",),
    "china airlines": ("email.china-airlines.com",),
    "ryanair": ("services.ryanairemails.com",),
    "scoot": ("promotion.flyscoot.com",),    # надзор 19.09.2026: письмо во Входящих
    # подписки владельца 19.09.2026 — только адреса РАССЫЛОК: с тех же доменов ходят коды входа, активация
    # аккаунта и «подтвердите подписку» (csair.com OTP, flypgs.com BolBol, Travel ID Lufthansa/Austrian) — их из
    # Входящих убирать нельзя. Домен один на рассылку и аккаунт (TAP) — запись полным адресом.
    "eurowings": ("news.eurowings.com",),
    "sky express": ("campaigns.skyexpress.com",),
    "tap": ("promo@tapmilesandgo.com", "mkt.flytap.com"),
    # надзор 01.10.2026 (скриншот владельца): рассылки в «Промоакциях» мимо списка. Pegasus: аккаунт BolBol пишет
    # с `flypgs.com` — берутся только поддомен `crm.` и отдельный домен `e-flypgs.com`
    "wizz": ("travel.wizznews.com",),
    "pegasus": ("crm.flypgs.com", "e-flypgs.com"),
    "etihad": ("choose.etihad.com",),
    "singapore airlines": ("email.singaporeair.com",),
    "air france": ("enews-airfrance.com",),
}


def query(days, label):
    domains = [d for doms in SENDERS.values() for d in doms]
    return f"from:({' OR '.join(domains)}) newer_than:{days}d -label:{label}"


def carrier_of(from_header):
    """Слово перевозчика по домену (или полному адресу) `From:`; чужой домен — сам домен."""
    addr = from_header.rsplit("<", 1)[-1].rstrip(">").strip().lower()
    domain = addr.rsplit("@", 1)[-1]
    for word, doms in SENDERS.items():
        if any(addr == d if "@" in d else (domain == d or domain.endswith("." + d)) for d in doms):
            return word
    return domain


def label_id(gmail, name):
    labels = gmail.users().labels().list(userId="me").execute().get("labels", [])
    for lab in labels:
        if lab["name"] == name:
            return lab["id"]
    created = gmail.users().labels().create(
        userId="me", body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}).execute()
    print(f"ярлык {name} создан", file=sys.stderr)
    return created["id"]


def list_ids(gmail, q):
    ids, token = [], None
    while True:
        page = gmail.users().messages().list(userId="me", q=q, maxResults=100, pageToken=token).execute()
        ids += [m["id"] for m in page.get("messages", [])]
        token = page.get("nextPageToken")
        if not token:
            return ids


# Месяцы словами — как в письмах 18.09–01.10.2026: польский (родительный, именительный, сокращение «29 wrz 26»)
# и английский. Форма — слово целиком: «3 marki» не март.
MONTHS = {}
for _n, _forms in enumerate((
        "stycznia styczeń styczen sty january jan", "lutego luty lut february feb", "marca marzec mar march",
        "kwietnia kwiecień kwiecien kwi april apr", "maja maj may", "czerwca czerwiec cze june jun",
        "lipca lipiec lip july jul", "sierpnia sierpień sierpien sie august aug",
        "września wrzesień wrzesnia wrzesien wrz september sept sep",
        "października październik pazdziernika pazdziernik paź paz october oct", "listopada listopad lis november nov",
        "grudnia grudzień grudzien gru december dec"), 1):
    MONTHS.update(dict.fromkeys(_forms.split(), _n))
_MON = "|".join(sorted(MONTHS, key=len, reverse=True))
# «01.11.2026», «30/09/2026», «4 października 2026», «29 wrz 26», «24 September», «March 2027» (= конец месяца)
DATE_RE = re.compile(
    r"(?<![\d/.])(?P<d>\d{1,2})[./](?P<m>\d{1,2})[./](?P<y>\d{4}|\d{2})(?![\d/])"
    rf"|(?<!\d)(?P<d2>\d{{1,2}})\.?\s+(?P<mon>{_MON})\.?(?!\w)(?:,?\s+(?P<y2>\d{{4}}|\d{{2}})(?![\d:]))?"
    rf"|(?<!\w)(?P<mon3>{_MON})\.?\s+(?P<y3>\d{{4}})(?!\d)", re.IGNORECASE)
# между датами диапазона — тире или «do/to/until», время и пояс («30/09/2026 00:00 CET - 30/09/2026 23:59 CET»),
# слово перед «do» (Emirates 28.09.2026: «Podróżuj z 28 wrz 26, Podróżuj do 21 gru 26»)
RANGE_GAP_RE = re.compile(r"\s*(?:\d{1,2}[:.]\d{2}\s*(?:CET|CEST|UTC|GMT)?\s*)?,?\s*(?:\w+\s+)?(?:[-–—]|do|to|until|till)\s*",
                          re.IGNORECASE)
WARSAW = zoneinfo.ZoneInfo("Europe/Warsaw")   # акция «30/09 00:00 CET» — по часам владельца
DISCOUNT_RE = re.compile(r"(?<![\d.,])(\d{1,2})\s?%")


def letter_text(payload):
    """Текст письма: HTML без стилей и без зачёркнутого (старая цена LOT — `line-through`, письмо 30.09.2026);
    строчные теги убираются без пробела (LOT режет «P</span><span>LN»), блочные — переводом строки;
    HTML нет — text/plain."""
    found = {}

    def walk(part):
        for sub in part.get("parts") or []:
            walk(sub)
        data = (part.get("body") or {}).get("data")
        if data and part.get("mimeType") in ("text/html", "text/plain"):
            found.setdefault(part["mimeType"], base64.urlsafe_b64decode(data).decode("utf-8", "replace"))

    walk(payload)
    text = found.get("text/plain", "")
    if "text/html" in found:
        h = re.sub(r"(?is)<(style|script|head)\b.*?</\1>|<!--.*?-->", " ", found["text/html"])
        h = re.sub(r"(?is)<(\w+)\b[^>]*line-through[^>]*>.*?</\1>|<(s|del|strike)\b[^>]*>.*?</\2>", " ", h)
        h = re.sub(r"(?i)<br\s*/?>|</?(?:p|div|td|tr|th|li|h\d|table|tbody|thead|ul|ol)\b[^>]*>", "\n", h)
        text = html.unescape(re.sub(r"<[^>]*>", "", h))
    lines = (" ".join(line.replace(" ", " ").split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _date(d, m, y):
    try:
        return datetime.date(y, m, d)
    except ValueError:
        return None


def letter_dates(text, sent):
    """(продажа до, полёт с, полёт по) по числам письма от `sent`; нет — None. Продажа до — ближайший конец
    диапазона, идущего в день письма («Sale Dates: 25 September - 2 October»), или одиночная дата позже письма;
    одиночная дата самого письма — «цены на день 28 Sep 26» (Emirates), продажей она будет, только если другой
    нет. Полёт — диапазон, кончающийся позже продажи, или одиночная дата позже неё, самые поздние.
    Год не указан — год конца диапазона или письма (дата раньше письма на месяц — следующий год)."""
    found = []   # [день, месяц, год или None, начало, конец]
    for m in DATE_RE.finditer(text):
        if m["d"]:
            d, mon, y = int(m["d"]), int(m["m"]), m["y"]
        elif m["d2"]:
            d, mon, y = int(m["d2"]), MONTHS[m["mon"].lower()], m["y2"]
        else:
            mon, y = MONTHS[m["mon3"].lower()], m["y3"]
            d = calendar.monthrange(int(y), mon)[1]
        found.append([d, mon, (int(y) + 2000 if len(y) == 2 else int(y)) if y else None, m.start(), m.end()])
    spans, singles, k = [], [], 0
    while k < len(found):
        a = found[k]
        b = found[k + 1] if k + 1 < len(found) else None
        if b and RANGE_GAP_RE.fullmatch(text[a[4]:b[3]]):
            yb = b[2] or sent.year
            ya = a[2] or (yb - 1 if (a[1], a[0]) > (b[1], b[0]) else yb)
            start, end = _date(a[0], a[1], ya), _date(b[0], b[1], yb)
            if start and end:
                spans.append((start, end))
            k += 2
            continue
        one = _date(a[0], a[1], a[2] or sent.year)
        if one and not a[2] and one < sent - datetime.timedelta(days=30):
            one = _date(a[0], a[1], sent.year + 1)
        if one:
            singles.append(one)
        k += 1
    sale = min([e for s, e in spans if s <= sent <= e] + [d for d in singles if d > sent],
               default=sent if sent in singles else None)
    later = [(s, e) for s, e in spans if sale and e > sale]
    if later:
        start, end = max(later, key=lambda p: p[1])
        return sale, start, end
    end = max([d for d in singles if sale and d > sale], default=None)
    return sale, None, end


def discount_of(text):
    """Наибольшая скидка в процентах 5–90 («do 15% zniżki», «up to 30%», «40% off»); 100% — не скидка."""
    return max((int(x) for x in DISCOUNT_RE.findall(text) if 5 <= int(x) <= 90), default=None)


def offer_of(msg, word):
    """Строка offers.jsonl по письму Gmail (`format=full`): акция = есть «продажа до» и цена или скидка;
    у не-акции текста нет — строка нужна, чтобы письмо не разбиралось повторно."""
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    sender = headers.get("from", "").rsplit("<", 1)[0].strip().strip('"').replace("​", "").strip() or word
    sent = datetime.datetime.fromtimestamp(int(msg.get("internalDate", 0)) / 1000, WARSAW).date()   # день владельца, не UTC
    text = letter_text(msg.get("payload", {}))
    sale, start, end = letter_dates(text, sent)
    prices = deal_feeds.mail_prices(text)
    discount = None if prices else discount_of(text)   # при ценах «до 50%» — про места, не билеты (LOT 23.09)
    offer = bool(sale and (prices or discount))
    iso = lambda d: d.isoformat() if d else None
    return {"id": msg["id"], "date": sent.isoformat(), "carrier": word, "sender": sender,
            "subject": headers.get("subject", ""), "link": f"https://mail.google.com/mail/#all/{msg.get('threadId', msg['id'])}",
            "offer": offer, "sale_until": iso(sale) if offer else None, "travel_from": iso(start) if offer else None,
            "travel_to": iso(end) if offer else None, "discount": discount if offer else None,
            "text": text[:20000] if offer else None}


def sheet_row(offer):
    """Строка для `sales_sheet.py`: самая низкая цена письма (PLN первыми), «продажа до», «полёт когда»."""
    prices = [deal_feeds.parse_price(m.group(0)) for m in deal_feeds.mail_prices(offer["text"])]
    pln = [p for p in prices if p[1] == "PLN"]
    value, currency = min(pln or prices, default=(None, None))
    travel = (f"{offer['travel_from'] or ''} – {offer['travel_to']}" if offer["travel_from"] else
              f"до {offer['travel_to']}" if offer["travel_to"] else "")
    title = f"{offer['sender']}: {offer['subject']}" + ("" if prices else " · цены на сайте")
    if offer["discount"]:
        title += f" · скидка до {offer['discount']}%"
    return {"block": "письмо", "dates": offer["date"], "source_id": "mail", "matched": [offer["carrier"]],
            "value": value, "currency": currency, "raw": title, "url": offer["link"],
            "sale_until": offer["sale_until"], "travel": travel}


def parse_new(gmail, days, label, path):
    """Письма под ярлыком за `days` дней, которых нет в `path`, — разобрать, дописать, акции — в таблицу.
    Возвращает строку-итог или None, если разбирать нечего."""
    known = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            known = {m[1] for m in (re.search(r'"id": "([^"]+)"', line) for line in fh) if m}   # битая строка — не повод встать
    new = [mid for mid in list_ids(gmail, f"label:{label} newer_than:{days}d") if mid not in known]
    if not new:
        return None
    offers = []
    for mid in new:
        msg = gmail.users().messages().get(userId="me", id=mid, format="full").execute()
        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        offers.append(offer_of(msg, carrier_of(headers.get("from", ""))))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.writelines(json.dumps(o, ensure_ascii=False) + "\n" for o in offers)
    found = [o for o in offers if o["offer"]]
    if found:
        push_sheet([sheet_row(o) for o in found])
    names = ", ".join(f"{o['carrier']} до {o['sale_until'][8:]}.{o['sale_until'][5:7]}" for o in found)
    return f"разобрано писем {len(offers)}, акций {len(found)}" + (f": {names}" if names else "")


def push_sheet(rows):
    """Акции — в Sheet «Распродажи» через `sales_sheet.py` тем же питоном; таблица — витрина: отказ — строка
    в stderr, разбор не страдает."""
    script = Path(__file__).resolve().parent / "sales_sheet.py"
    try:
        proc = subprocess.run([sys.executable, str(script)], input=json.dumps(rows, ensure_ascii=False),
                              capture_output=True, text=True, encoding="utf-8", timeout=120)
        if proc.returncode:
            raise RuntimeError((proc.stderr or proc.stdout).strip()[-200:])
    except Exception as exc:  # noqa: BLE001 — витрина не должна ронять разбор
        print(f"таблица распродаж не обновлена ({type(exc).__name__}: {str(exc)[:160]})", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=3, help="окно писем, дней (по умолчанию 3)")
    ap.add_argument("--label", default=LABEL, help=f"имя ярлыка (по умолчанию {LABEL})")
    ap.add_argument("--dry-run", action="store_true", help="показать, что убрал бы, ничего не меняя")
    ap.add_argument("--parse", action="store_true", help="разобрать новые письма под ярлыком в travel/mail/offers.jsonl")
    args = ap.parse_args()

    sys.path.append(str(Path(os.environ.get("HERMES_HOME", "/opt/data")) / "skills" / "productivity"
                        / "google-workspace" / "scripts"))   # в конец: скилл агента не должен затенять наши модули
    from google_api import build_service

    gmail = build_service("gmail", "v1")
    found = list_ids(gmail, query(args.days, args.label))
    by_carrier, strangers, ids = collections.Counter(), collections.Counter(), []
    for mid in found:
        msg = gmail.users().messages().get(userId="me", id=mid, format="metadata", metadataHeaders=["From"]).execute()
        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        word = carrier_of(headers.get("from", ""))
        if word in SENDERS:                       # поиск Gmail `from:` шире точного домена — убираем только сопоставленное
            by_carrier[word] += 1
            ids.append(mid)
        else:
            strangers[word] += 1
    if strangers:
        print("не тронуты (нашлись по from:, но вне SENDERS): " + ", ".join(f"{d} {n}" for d, n in strangers.most_common()),
              file=sys.stderr)
    summary = ", ".join(f"{word} {n}" for word, n in by_carrier.most_common())
    if not ids:
        print("новых писем перевозчиков нет", file=sys.stderr)
    elif args.dry_run:
        print(f"[dry-run] {len(ids)} писем → {args.label}: {summary}", file=sys.stderr)
    else:
        gmail.users().messages().batchModify(
            userId="me", body={"ids": ids, "addLabelIds": [label_id(gmail, args.label)], "removeLabelIds": ["INBOX"]}).execute()
        print(f"{args.label}: {len(ids)} писем из Входящих — {summary}")
    if args.parse and not args.dry_run:
        done = parse_new(gmail, args.days, args.label, deal_feeds.mail_offers_path())
        if done:
            print(done)
    return 0


if __name__ == "__main__":
    sys.exit(main())
