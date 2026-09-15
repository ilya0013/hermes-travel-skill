#!/usr/bin/env python3
"""Ленты охотников за распродажами (RSS и превью Telegram-каналов): наводки по направлениям
и авиакомпаниям из задачи.

    /opt/data/travel/lib/venv/bin/python deal_feeds.py \
        --match "tajlandia,thailand,bangkok,finnair,klm,lot" [--from WAW] [--feeds fly4free_pl,pepper] [--days 14]
        [--from-poland] [--seen /opt/data/travel/interest/seen.txt]

Совпадение ищется без учёта регистра в заголовке, рубриках и описании записи. Цена берётся из
заголовка («od 2073 PLN», «za 299 zł», «from €478», «115 евро (449 злотых)» — из двух валют
берётся PLN). Источник `tg:<канал>` — публичное превью `t.me/s/<канал>`: последние ~20 постов,
заголовок — начало текста, ссылка — `t.me/<канал>/<id>`. `--from-poland` оставляет только записи,
где вылет из Польши назван словами («z Warszawy», «из Польши»): у наводок нет кодов IATA.
`--seen` — файл уже показанных ссылок: они пропускаются, новые дописываются (не при --dry-run).

Режим интереса (cron Hermes каждый день как `--script`; пустой stdout = агент не зовётся):

    deal_feeds.py --interest /opt/data/travel/interest [--seen .../seen.txt]
        [--pending .../pending.md] [--digest-weekday 1]

`--interest` — файл или каталог файлов `<имя>.json` (нет пути — интересов нет):
    {"name": "turcja", "match": "турци,turcja,antalya", "from": "WAW", "until": "2026-11-30",
     "max_pln": 1500, "from_poland": true}
`match` — строка через запятую (без запятых — через пробел) или список. Интерес действует по `until` (ISO `YYYY-MM-DD`)
включительно, потом гаснет сам. `from_poland` по умолчанию true. Файл, который не разобрать
(не JSON, `until` не дата, `max_pln` не число) — пропускается со строкой в stderr, остальные
работают. Наводки: заголовок, дата, ссылка, сгруппированы по интересу; с ценой в PLN выше
`max_pln` отбрасываются (и не считаются показанными). Превью Telegram — только последние ~20
постов (у wakacyjnipiracipl это ~3 дня, снято 14.09.2026), поэтому сбор ежедневный:
с `--pending` новые наводки копятся в файле, а в stdout идут одним сводом в день
`--digest-weekday` (ISO: 1 — понедельник); в остальные дни stdout пуст. Без `--pending` —
печать сразу. Нет действующих интересов или новых наводок — stdout пуст.
Служебные строки (записи журнала, «записей N») в этом режиме идут в stderr.
В журнал идут наводки с ценой в заголовке,
в валюте как есть (злотые считает report.py курсом НБП — решение 14.09.2026) — строкой kind=lead,
статус ORIENTIR, dates — дата публикации (даты поездки в raw), route — вылет задачи и совпавшее
слово (`WAW-tajlandia`), не коды IATA: направление наводки известно только словами. Наводки без
цены печатаются, но не журналируются. Слова совпадения — по началу слова: `tajland` найдёт
Tajlandia и Tajlandii, `azja` не найдёт `okazja` (подстрока дала это в живом прогоне 15.09.2026);
слово до трёх букв (`lot`, `goa`) — только целиком, иначе `lot` находит «loty».
Число в заголовке — реклама, а не выдача: дальше роль проверяет его источником (Kiwi и др.).
"""

import argparse
import contextlib
import html
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime

import journal

FEEDS = {
    "fly4free_pl": "https://www.fly4free.pl/feed/",
    "fly4free_com": "https://www.fly4free.com/feed/",
    "wakacyjnipiraci": "https://www.wakacyjnipiraci.pl/feed",
    "holidaypirates": "https://www.holidaypirates.com/feed",
    "pepper": "https://www.pepper.pl/rss/grupa/podroze",
    "vandrouki_by": "https://vandrouki.by/feed/",
    "tg_vandroukiby": "tg:vandroukiby",
    "tg_wakacyjnipiracipl": "tg:wakacyjnipiracipl",
}
# вылет из Польши словами — по-польски, по-русски (Вандроўкі) и по-английски (fly4free.com, holidaypirates)
POLAND_WORDS = ("z warszawy", "warszaw", "z krakowa", "krakow", "kraków", "gdańsk", "gdansk", "katowic",
                "wrocław", "wroclaw", "poznań", "poznan", "z polski", "polska", "из польши", "из варшавы",
                "варшав", "краков", "гданьск", "катовиц", "вроцлав", "познан", "warsaw", "poland", "polish")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) travel-role deal_feeds"

# «2073 PLN», «299 zł», «1 299,99 zł», «€478», «£52», «478 EUR», «$550», «115 евро», «449 злотых»
_NUM = r"(\d{1,3}(?:[ \u00a0]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
_CUR_AFTER = r"(PLN|zł|zl|złotych|EUR|USD|GBP|€|\$|£|евро|злотых|злотые|злотый|зл)"
_CUR_BEFORE = r"(€|\$|£)"
PRICE_RE = re.compile(rf"{_NUM}\s*{_CUR_AFTER}(?![\w])|{_CUR_BEFORE}\s*{_NUM}", re.IGNORECASE)
CURRENCY = {"pln": "PLN", "zł": "PLN", "zl": "PLN", "złotych": "PLN", "eur": "EUR", "€": "EUR",
            "usd": "USD", "$": "USD", "gbp": "GBP", "£": "GBP",
            "евро": "EUR", "злотых": "PLN", "злотые": "PLN", "злотый": "PLN", "зл": "PLN"}


def parse_price(title):
    """(значение, валюта) из заголовка или (None, None); из нескольких цен — первая в PLN, иначе первая."""
    found = []
    for m in PRICE_RE.finditer(title or ""):
        num = m.group(1) or m.group(4)
        cur = m.group(2) or m.group(3)
        value = float(num.replace("\u00a0", "").replace(" ", "").replace(",", "."))
        found.append((value, CURRENCY[cur.lower()]))
    if not found:
        return None, None
    return next((f for f in found if f[1] == "PLN"), found[0])


def parse_feed(xml_text):
    """Записи RSS 2.0 / Atom: title, link, published (date), text (для поиска совпадений)."""
    root = ET.fromstring(xml_text)
    atom = "{http://www.w3.org/2005/Atom}"
    items = root.findall(".//item") or root.findall(f".//{atom}entry")
    out = []
    for it in items:
        title = (it.findtext("title") or it.findtext(f"{atom}title") or "").strip()
        link = it.findtext("link") or ""
        if not link and it.find(f"{atom}link") is not None:
            link = it.find(f"{atom}link").get("href", "")
        stamp = (it.findtext("pubDate") or it.findtext(f"{atom}published")
                 or it.findtext(f"{atom}updated") or "")
        published = None
        if stamp:
            try:
                published = parsedate_to_datetime(stamp).date().isoformat()
            except (TypeError, ValueError):
                try:
                    published = datetime.fromisoformat(stamp.replace("Z", "+00:00")).date().isoformat()
                except ValueError:
                    published = None
        cats = [c.text or "" for c in it.findall("category")]
        desc = html.unescape(re.sub(r"<[^>]+>", " ", it.findtext("description") or ""))
        out.append({"title": html.unescape(title), "link": link.strip(), "published": published,
                    "text": " ".join([title, *cats, desc]).lower()})
    return out


def parse_telegram(page):
    """Посты публичного превью t.me/s/<канал> в той же форме, что записи RSS; `price_text` —
    весь текст поста: цена в нём может стоять дальше 200 символов заголовка."""
    out = []
    for wrap in re.split(r'<div class="tgme_widget_message_wrap', page)[1:]:
        post = re.search(r'data-post="([^"]+)"', wrap)
        stamp = re.search(r'<time datetime="([^"]+)"', wrap)
        start = wrap.find('class="tgme_widget_message_text')
        if not post or start < 0:
            continue
        end = min(i for i in (wrap.find('class="tgme_widget_message_reactions', start),
                              wrap.find('class="tgme_widget_message_footer', start), len(wrap)) if i > 0)
        text = html.unescape(re.sub(r"<[^>]+>", " ", wrap[start:end].split(">", 1)[1]))
        text = re.sub(r"\s+", " ", text).strip()
        published = None
        if stamp:
            try:
                published = datetime.fromisoformat(stamp.group(1)).date().isoformat()
            except ValueError:
                published = None
        out.append({"title": text[:200], "link": f"https://t.me/{post.group(1)}", "published": published,
                    "text": text.lower(), "price_text": text})
    return out


def split_terms(s):
    """Слова `--match`: через запятую, а без единой запятой — через пробел (15.09.2026 Лиза дала семь
    слов через пробел, и одна «фраза» не совпала ни с чем). В списке через запятую фраза с пробелом
    («costa brava» в файле интереса) остаётся фразой."""
    parts = s.split(",") if "," in s else s.split()
    return [t.strip() for t in parts if t.strip()]


def term_hits(text, terms):
    """Слова совпадения — по началу слова: `tajland` найдёт Tajlandia и Tajlandii, `azja` не найдёт
    okazja (живой прогон 15.09.2026: подстрока дала 23 чужих наводки из 24). Слово до трёх букв —
    только целиком: `lot` (перевозчик) по началу совпадало с «loty» — 22 наводки из 24 (эвал 15.09.2026)."""
    return [t for t in terms if re.search(r"(?<!\w)" + re.escape(t) + (r"(?!\w)" if len(t) <= 3 else ""), text)]


def match_items(items, terms, since=None, from_poland=False, seen=()):
    terms = [t.strip().lower() for t in terms if t.strip()]
    for it in items:
        if since and it["published"] and it["published"] < since:
            continue
        if it["link"] in seen:
            continue
        if from_poland and not any(w in it["text"] for w in POLAND_WORDS):
            continue
        hit = term_hits(it["text"], terms)
        if hit:
            yield it, hit


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def read_feed(feed_id):
    url = FEEDS[feed_id]
    if url.startswith("tg:"):
        return parse_telegram(fetch(f"https://t.me/s/{url[3:]}"))
    return parse_feed(fetch(url))


def load_seen(path):
    if not path or not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as fh:
        return {line.strip() for line in fh if line.strip()}


def load_interests(path, today):
    """Действующие интересы из файла или каталога *.json; нет пути — нет интересов.
    Истёкшие и неразобранные файлы — строкой в stderr, остальные работают."""
    if os.path.isdir(path):
        paths = sorted(os.path.join(path, f) for f in os.listdir(path) if f.endswith(".json"))
    elif os.path.isfile(path):
        paths = [path]
    else:
        return []
    active = []
    for fp in paths:
        name = os.path.splitext(os.path.basename(fp))[0]
        try:
            with open(fp, encoding="utf-8") as fh:
                cfg = json.load(fh)
            cfg.setdefault("name", name)
            m = cfg["match"]
            cfg["terms"] = [str(t) for t in m] if isinstance(m, list) else split_terms(str(m))
            if cfg.get("until"):
                cfg["until"] = date.fromisoformat(cfg["until"]).isoformat()
            if cfg.get("max_pln") is not None:
                cfg["max_pln"] = float(cfg["max_pln"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"интерес {name}: файл не разобран ({exc}) — пропущен", file=sys.stderr)
            continue
        if cfg.get("until") and cfg["until"] < today:
            print(f"интерес {cfg['name']}: истёк {cfg['until']}", file=sys.stderr)
            continue
        active.append(cfg)
    return active


def price_of(item):
    """(значение, валюта) наводки: у постов Telegram — по всему тексту, у RSS — по заголовку."""
    return parse_price(item.get("price_text") or item["title"])


def above_ceiling(item, max_pln):
    """Цена в PLN выше потолка интереса; другая валюта или нет цены — не выше."""
    value, currency = price_of(item)
    return currency == "PLN" and value > max_pln


def scan(run, feed_ids, terms, since, origin, from_poland, seen_links, max_pln=None):
    """Читает ленты (с кэшем на процесс). Возвращает (строки журнала, строки наводок, сводка по лентам,
    показанные ссылки, прочитано лент)."""
    rows, shown, leads, summary, read = [], [], [], [], 0
    for feed_id in feed_ids:
        if feed_id not in FEEDS:
            print(f"лента неизвестна: {feed_id} (есть: {', '.join(FEEDS)})", file=sys.stderr)
            continue
        if feed_id not in _CACHE:
            try:
                _CACHE[feed_id] = read_feed(feed_id)
            except Exception as exc:  # noqa: BLE001 — лента недоступна, остальные читаем
                print(f"[{feed_id}] не прочитана: {exc}", file=sys.stderr)
                _CACHE[feed_id] = None
        items = _CACHE[feed_id]
        if items is None:
            continue
        read += 1
        matches = list(match_items(items, terms, since, from_poland, seen_links))
        if max_pln is not None:  # выше потолка — не показана, значит и не «показанная»
            matches = [(it, hit) for it, hit in matches if not above_ceiling(it, max_pln)]
        shown.extend(it["link"] for it, _ in matches)
        feed_rows, seen = rows_for_matches(run, feed_id, matches, origin)
        summary.append(f"[{feed_id}] записей {len(items)}, совпадений {len(seen)}")
        leads.extend("  " + line for line in seen)
        rows.extend(feed_rows)
    return rows, leads, summary, shown, read


_CACHE = {}


def rows_for_matches(run, feed_id, matches, origin):
    rows, seen = [], []
    for it, hit in matches:
        value, currency = price_of(it)
        note = f"[{feed_id}] {it['published'] or '????-??-??'} {it['title']} — {', '.join(hit)} → {it['link']}"
        if value is None:
            seen.append(note + " (цены в заголовке нет — не в журнал)")
            continue
        if not it["published"]:
            seen.append(note + " (нет даты публикации — не в журнал)")
            continue
        rows.append(journal.observation(
            run, "lead", f"{feed_id}_feed", it["link"], f"{origin}-{hit[0]}", it["published"], value, currency,
            "ORIENTIR", it["title"], price_prefix=journal.price_prefix_of(it["title"]),
            matched=hit,
        ))
        seen.append(note)
    return rows, seen


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="origin", default="WAW", help="откуда летим по задаче")
    ap.add_argument("--match", help="слова через запятую или пробел: города, страны, авиакомпании")
    ap.add_argument("--interest", help="файл или каталог интересов *.json — режим cron, см. выше")
    ap.add_argument("--feeds", default=",".join(FEEDS), help="какие ленты, через запятую")
    ap.add_argument("--days", type=int, default=14, help="не старше N дней по дате публикации")
    ap.add_argument("--from-poland", action="store_true", help="только записи с вылетом из Польши словами")
    ap.add_argument("--seen", help="файл уже показанных ссылок: пропустить и дописать новые")
    ap.add_argument("--pending", help="режим интереса: копить наводки здесь, печатать сводом раз в неделю")
    ap.add_argument("--digest-weekday", type=int, default=1, help="день свода по ISO (1 — понедельник)")
    journal.add_common_args(ap)
    args = ap.parse_args()
    if not args.match and not args.interest:
        ap.error("нужен --match или --interest")
    run = journal.run_id(args)
    _CACHE.clear()

    today = date.today().isoformat()
    since = (date.today() - timedelta(days=args.days)).isoformat()
    seen_links = load_seen(args.seen)
    feed_ids = [f.strip() for f in args.feeds.split(",") if f.strip()]

    if args.interest:
        interests = load_interests(args.interest, today)
        rows, shown, read, out = [], [], 0, []
        for cfg in interests:
            i_rows, leads, summary, i_shown, i_read = scan(
                run, feed_ids, cfg["terms"], since, cfg.get("from", args.origin),
                cfg.get("from_poland", True), seen_links | set(shown), cfg.get("max_pln"))
            read = max(read, i_read)
            rows.extend(i_rows)
            shown.extend(i_shown)
            if leads:
                head = f"## интерес {cfg['name']}: {','.join(cfg['terms'])}"
                head += f", до {cfg['until']}" if cfg.get("until") else ""
                head += f", потолок {cfg['max_pln']:g} PLN" if cfg.get("max_pln") is not None else ""
                out.extend([head, *leads, ""])
            print("\n".join(summary), file=sys.stderr)
        if not interests:
            print("действующих интересов нет — тишина", file=sys.stderr)
        digest = "\n".join(out)
        if args.pending:
            digest = pending_digest(args.pending, digest, date.today().isoweekday() == args.digest_weekday,
                                    args.dry_run)
        print(digest, end="")
        with contextlib.redirect_stdout(sys.stderr):
            _save_seen(args, shown)
            if interests:
                journal.finish(args, run, rows)
        return 0 if not interests or read else _no_feeds()
    rows, leads, summary, shown, read = scan(run, feed_ids, split_terms(args.match), since, args.origin,
                                             args.from_poland, seen_links)
    print("\n".join(summary + leads))
    _save_seen(args, shown)
    journal.finish(args, run, rows)
    return 0 if read else _no_feeds()


def _no_feeds():
    print("ни одна лента не прочитана — источник не проверен", file=sys.stderr)
    return 1


def pending_digest(path, fresh, digest_day, dry_run):
    """Копит свежие наводки в файле; в день свода возвращает всё накопленное и очищает файл,
    в остальные дни — пустую строку. При --dry-run файл не меняется."""
    kept = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            kept = fh.read()
    if dry_run:
        return kept + fresh if digest_day else ""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w" if digest_day else "a", encoding="utf-8") as fh:
        fh.write("" if digest_day else fresh)
    return kept + fresh if digest_day else ""


def _save_seen(args, shown):
    if args.seen and shown and not args.dry_run:
        os.makedirs(os.path.dirname(os.path.abspath(args.seen)), exist_ok=True)
        with open(args.seen, "a", encoding="utf-8") as fh:
            fh.write("".join(link + "\n" for link in shown))


if __name__ == "__main__":
    sys.exit(main())
