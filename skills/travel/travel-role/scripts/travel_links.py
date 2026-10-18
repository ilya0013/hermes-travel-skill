#!/usr/bin/env python3
"""Ссылки на покупку у каждого плеча вариантов, названных в ответе агента, — кодом, а не вниманием модели.

Зовёт плагин Hermes `travel-links` (хук `transform_llm_output`, `../plugin/`): ответ о поездке («до двери») в сессии
с файлом прогона `tmp/travel_run.<session_id>` сверяется с выводами `report.py` с последнего `--auto` (блоки «## Напечатано»
черновика `travel/reports/<run>.md`): вариант, чья сумма есть в ответе, — его плечи; ссылка плеча — у авиакомпании
(строка `book` от `slicktrip.py`, `of` → плечо), иначе страница продавца строки (`report.link_of`). Чего в ответе
нет — дописывается блоком «Ссылки на покупку»; всё на месте или ответ не о поездке — None, текст не трогается.
Эвал 18.10.2026: Рим — ни одной ссылки, пара Ryanair — только «туда», хотя report.py и slicktrip.py их печатали.
Тем же проходом ★ ставится у auto-1 (`star_at_auto1`): правило роли «★ одна — у самого дешёвого до двери» модель
обходит, когда просили тепло.
"""

import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import report  # noqa: E402

HEAD = re.compile(r"^(\S+) = (\d+) PLN до двери \(строки ([\d+]+)\)", re.M)
AUTO = re.compile(r"^auto-\d+ = (\d+) PLN до двери \(строки ([\d+]+)\)(?! \(без)", re.M)   # с полной суммой
AUTO_CALL = re.compile(r"^auto: кандидатов", re.M)                                        # вывод вызова с --auto
AMOUNT = re.compile(r"(?<![\d.,])\d+\s*(?:zł|zl|зл|PLN)", re.I)
# дата и время перед суммой не склеиваются: «08.11 200 zł» — не «08.11200» (ревью 18.10.2026)
SPACE_IN_NUMBER = re.compile(r"(?<=\d)(?<![.:]\d)(?<![.:]\d\d)[    ](?=\d{3}(?!\d))")   # «3 330 zł» — как «3330 zł»


URL = re.compile(r"https?://[^\s<>()\[\]«»\"']+")


def _day(text):
    return f"{text[8:10]}.{text[5:7]}"


def _shown(link, urls):
    """Ссылка уже в ответе — та же или укороченная моделью (живьём 18.10.2026: «&discount=0» отрезан): тот же сайт и
    путь, параметры ответа — часть параметров ссылки, не меньше половины."""
    a = urlsplit(link)
    want, place = set(parse_qsl(a.query)), (a.netloc.lower().removeprefix("www."), a.path.rstrip("/"))
    for u in urls:
        b = urlsplit(u)
        got = set(parse_qsl(b.query))
        if (b.netloc.lower().removeprefix("www."), b.path.rstrip("/")) == place and got <= want \
                and 2 * len(got) >= len(want):
            return True
    return False


def _flights(nums, rows, sources):
    """Перелёты варианта по номерам «строки 1+2+3»: дорога и пересчёт — мимо."""
    legs = []
    for n in nums.split("+"):
        if not n.isdigit() or not 1 <= int(n) <= len(rows):
            continue
        r = rows[int(n) - 1]
        if r.get("kind") in report.FLIGHT_KINDS and r.get("source_id") != "CALC" and not report.is_ground(r, sources):
            legs.append(r)
    return legs


def _link(r, books):
    return (books.get(r["id"]) or {}).get("url") or report.link_of(r)


def _dates(r):
    return [d for d in str(r.get("dates") or "").split("/") if len(d) == 10]


def _leg(r):
    return f"{'–'.join(_day(d) for d in _dates(r))} {str(r.get('route') or '').replace('-', '→')}"


def _requoted_dearer(r, rows):
    """Плечо позже в прогоне перепроверено дороже: тот же маршрут и даты, цена в той же валюте выше. Эвал 18.10.2026
    (Рим, 1.3.27): Kiwi 786 на рейсы Wizz, Google на те же даты — 836; агент честно взял 836."""
    same = lambda x: (x.get("route"), x.get("dates"), x.get("currency"), x.get("pax"), report.has_bags(x))  # noqa: E731
    return any(x.get("kind") in report.FLIGHT_KINDS and x.get("status") != "ORIENTIR"
               and x.get("source_id") not in report.NOT_A_PRICE_SOURCES and isinstance(x.get("value"), (int, float))
               and same(x) == same(r) and x["value"] > (r.get("value") or 0) for x in rows[rows.index(r) + 1:])


def star_at_auto1(text, blocks, rows, books, sources):
    """★ — у auto-1 последнего `--auto` (шаг 6 роли); плечо auto-1 позже перепроверено дороже — мерка следующий auto
    (★ на самом auto-1 при этом не снимается). Строка со ★ и суммой, где нет ни суммы мерки, ни суммы варианта
    дешевле неё из выводов report.py с того вызова, теряет ★; не осталось верной ★ — перед первой такой строкой
    встаёт ★ мерки с плечами и ссылками. ★ без суммы, прогон без `--auto` и `--auto` без поездок не проверяются.
    Текст или None. Эвал 18.10.2026 «куда дешевле, тепло — плюс»: ★ на Катании 300 «тёплее» при auto-1 Братиславе 200."""
    start = next((i for i in range(len(blocks) - 1, -1, -1) if AUTO_CALL.search(blocks[i])), None)
    if start is None:
        return None
    total, nums = next(((t, n) for t, n in AUTO.findall(blocks[start])
                        if not any(_requoted_dearer(r, rows) for r in _flights(n, rows, sources))), (None, None))
    if total is None:
        return None
    allowed = {int(total)} | {int(t) for b in blocks[start:] for _, t, _ in HEAD.findall(b) if int(t) < int(total)}
    lines = text.split("\n")
    flat = [SPACE_IN_NUMBER.sub("", line) for line in lines]
    right = {i for i, f in enumerate(flat) if any(re.search(rf"(?<![\d.,]){a}(?![\d]|[.,]\d)", f) for a in allowed)}
    stars = [i for i, line in enumerate(lines) if "★" in line]
    wrong = [i for i in stars if i not in right and AMOUNT.search(flat[i])]
    if not wrong:
        return None
    for i in wrong:
        lines[i] = re.sub(r"★\s?", "", lines[i])
    if not right & set(stars):
        legs = [f"  {_leg(r)}" + (f": {link}" if (link := _link(r, books)) else "") for r in _flights(nums, rows, sources)]
        lines[wrong[0]:wrong[0]] = [f"★ Самый дешёвый до двери — {total} zł:", *legs, ""]
    return "\n".join(lines)


def add_links(text, session_id, home):
    """Текст с ★ у auto-1 и дописанными ссылками или None — менять нечего."""
    if not isinstance(text, str) or "до двери" not in text or not session_id:
        return None
    home = Path(home)
    run_file = home / "tmp" / f"travel_run.{session_id}"
    if not run_file.is_file():
        return None
    run = run_file.read_text(encoding="utf-8").strip()
    draft = home / "travel" / "reports" / f"{run}.md"
    journal_path = home / "travel" / "observations.jsonl"
    if not run or not draft.is_file() or not journal_path.is_file():
        return None
    draft_text = draft.read_text(encoding="utf-8")
    blocks = draft_text.split("## Напечатано")[1:]
    # варианты — из выводов с последнего --auto: следом идут свои варианты агента того же запроса (эвал 18.10.2026,
    # 1.3.32: ★ у auto-1 из вывода --auto, последний вызов — только --variant, ссылки у ★ не дописались)
    since = next((i for i in range(len(blocks) - 1, -1, -1) if AUTO_CALL.search(blocks[i])), len(blocks) - 1)
    printed = "\n".join(blocks[since:]) if blocks else draft_text
    rows, _ = report.run_rows(str(journal_path), run)
    sources = report.ground_sources(report.load_profile(str(report.PROFILE)))
    books = {r.get("of"): r for r in rows if r.get("kind") == "book" and r.get("url")}   # последняя — свежая
    starred = star_at_auto1(text, blocks, rows, books, sources)
    text = starred or text
    flat = SPACE_IN_NUMBER.sub("", text)
    urls = [u.rstrip(".,;:!?") for u in URL.findall(text)]
    named = {}
    for name, total, nums in HEAD.findall(printed):
        if re.search(rf"(?<!\d){total}\s*(?:zł|zl|зл|PLN)", flat):
            named.setdefault(total, []).append(_flights(nums, rows, sources))
    lines, done = [], set()
    for total, variants in named.items():
        if len({tuple(r["id"] for r in v) for v in variants}) > 1:
            # равные суммы (--top 8 «куда дешевле» — восемь по 200): тот, чья ссылка уже в ответе, иначе — чьи даты
            # плеч все названы; не различить — ссылок нет: чужое направление хуже пропущенного (ревью 18.10.2026)
            variants = [v for v in variants if any((u := _link(r, books)) and _shown(u, urls) for r in v)] \
                or [v for v in variants if v and all(_day(d) in text for r in v for d in _dates(r))]
        for r in variants[0] if variants else []:
            link = _link(r, books)
            if not link or link in done or _shown(link, urls):
                continue
            done.add(link)
            lines.append(f"  {total} zł · {_leg(r)}: {link}")
    if not lines:
        return starred
    return text.rstrip() + "\n\nСсылки на покупку:\n" + "\n".join(lines)


def main():
    """Для плагина: ответ — stdin, новый текст — stdout; дописывать нечего — stdout пуст."""
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", required=True)
    ap.add_argument("--home", required=True)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    out = add_links(sys.stdin.read(), args.session, args.home)
    if out:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.exit(main())
