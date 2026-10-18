#!/usr/bin/env python3
"""Ссылки на покупку у каждого плеча вариантов, названных в ответе агента, — кодом, а не вниманием модели.

Зовёт плагин Hermes `travel-links` (хук `transform_llm_output`, `../plugin/`): ответ о поездке («до двери») в сессии
с файлом прогона `tmp/travel_run.<session_id>` сверяется с последним выводом `report.py` (блок «## Напечатано»
черновика `travel/reports/<run>.md`): вариант, чья сумма есть в ответе, — его плечи; ссылка плеча — у авиакомпании
(строка `book` от `slicktrip.py`, `of` → плечо), иначе страница продавца строки (`report.link_of`). Чего в ответе
нет — дописывается блоком «Ссылки на покупку»; всё на месте или ответ не о поездке — None, текст не трогается.
Эвал 18.10.2026: Рим — ни одной ссылки, пара Ryanair — только «туда», хотя report.py и slicktrip.py их печатали.
"""

import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import report  # noqa: E402

HEAD = re.compile(r"^(\S+) = (\d+) PLN до двери \(строки ([\d+]+)\)", re.M)
SPACE_IN_NUMBER = re.compile(r"(?<=\d)[    ](?=\d{3}(?!\d))")   # «3 330 zł» — как «3330 zł»


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


def add_links(text, session_id, home):
    """Текст с дописанными ссылками или None — дописывать нечего."""
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
    printed = draft.read_text(encoding="utf-8").rpartition("## Напечатано")[2]
    rows, _ = report.run_rows(str(journal_path), run)
    sources = report.ground_sources(report.load_profile(str(report.PROFILE)))
    books = {r.get("of"): r for r in rows if r.get("kind") == "book" and r.get("url")}   # последняя — свежая
    flat = SPACE_IN_NUMBER.sub("", text)
    urls = [u.rstrip(".,;:!?") for u in URL.findall(text)]
    lines, done = [], set()
    for name, total, nums in HEAD.findall(printed):
        if not re.search(rf"(?<!\d){total}\s*(?:zł|zl|PLN)", flat):
            continue
        for n in nums.split("+"):
            if not n.isdigit() or not 1 <= int(n) <= len(rows):
                continue
            r = rows[int(n) - 1]
            if r.get("kind") not in report.FLIGHT_KINDS or r.get("source_id") == "CALC" or report.is_ground(r, sources):
                continue
            link = (books.get(r["id"]) or {}).get("url") or report.link_of(r)
            if not link or link in done or _shown(link, urls):
                continue
            done.add(link)
            dates = str(r.get("dates") or "").split("/")
            when = "–".join(_day(d) for d in dates if len(d) == 10)
            lines.append(f"  {total} zł · {when} {str(r.get('route') or '').replace('-', '→')}: {link}")
    if not lines:
        return None
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
