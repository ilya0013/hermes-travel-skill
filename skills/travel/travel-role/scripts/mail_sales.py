#!/usr/bin/env python3
"""Письма рассылок перевозчиков → ярлык `avia-sale` и вон из Входящих.

    HERMES_HOME=/opt/data /opt/hermes/.venv/bin/python3 mail_sales.py [--days 3] [--label avia-sale] [--dry-run]

Владелец подписан на рассылки перевозчиков на свой Gmail. Фильтры через Gmail API требуют права
`gmail.settings.basic`, которого в токене агента нет — потому уборку делает код правом `gmail.modify`:
письма с доменов `SENDERS` за `--days` дней, ещё без ярлыка, получают ярлык и теряют `INBOX` одним
`batchModify`. Ярлык ищется по имени, нет — создаётся.
Cron Hermes зовёт каждый час без агента (обёртка `scripts/travel/mail_sales.sh` в HERMES_HOME, доставка
`local`): stdout — одна строка «сколько и от кого», пустой — нечего убирать; ошибка — код 1 и stderr.
Разбор писем (цена, «продажа до», «полёт когда» → Sheet «Распродажи») — отдельный шаг, когда акции
придут. Ходит токеном агента через `build_service` скилла google-workspace — потому питон Hermes.
"""

import argparse
import collections
import os
import sys
from pathlib import Path

LABEL = "avia-sale"
# Домены рассылок ≠ основные домены перевозчиков (`e.emirates.email`, `services.ryanairemails.com`) —
# список по реальным письмам-подтверждениям 18.09.2026; новый перевозчик = новая строка здесь.
# Ключ — слово перевозчика, как в `deal_feeds.NOTABLE` (колонка «слово» таблицы «Распродажи»).
SENDERS = {
    "finnair": ("email.finnair.com",),
    "lot": ("mailing.lot.com",),
    "qatar": ("info.qatarairways.com",),
    "turkish": ("mail.turkishairlines.com",),
    "thai": ("tg.thaiairways.com", "thaiairways.com"),
    "aegean": ("news.aegeanair.com",),
    "emirates": ("e.emirates.email",),
    "ana": ("mail.ana.co.jp",),
    "china airlines": ("email.china-airlines.com",),
    "ryanair": ("services.ryanairemails.com",),
}


def query(days, label):
    domains = [d for doms in SENDERS.values() for d in doms]
    return f"from:({' OR '.join(domains)}) newer_than:{days}d -label:{label}"


def carrier_of(from_header):
    """Слово перевозчика по домену адреса `From:`; чужой домен — сам домен."""
    domain = from_header.rsplit("@", 1)[-1].rstrip(">").strip().lower()
    for word, doms in SENDERS.items():
        if any(domain == d or domain.endswith("." + d) for d in doms):
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=3, help="окно писем, дней (по умолчанию 3)")
    ap.add_argument("--label", default=LABEL, help=f"имя ярлыка (по умолчанию {LABEL})")
    ap.add_argument("--dry-run", action="store_true", help="показать, что убрал бы, ничего не меняя")
    args = ap.parse_args()

    sys.path.append(str(Path(os.environ.get("HERMES_HOME", "/opt/data")) / "skills" / "productivity"
                        / "google-workspace" / "scripts"))   # в конец: скилл агента не должен затенять наши модули
    from google_api import build_service

    gmail = build_service("gmail", "v1")
    ids = list_ids(gmail, query(args.days, args.label))
    if not ids:
        print("новых писем перевозчиков нет", file=sys.stderr)
        return 0
    by_carrier = collections.Counter()
    for mid in ids:
        msg = gmail.users().messages().get(userId="me", id=mid, format="metadata", metadataHeaders=["From"]).execute()
        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        by_carrier[carrier_of(headers.get("from", ""))] += 1
    summary = ", ".join(f"{word} {n}" for word, n in by_carrier.most_common())
    if args.dry_run:
        print(f"[dry-run] {len(ids)} писем → {args.label}: {summary}", file=sys.stderr)
        return 0
    gmail.users().messages().batchModify(
        userId="me", body={"ids": ids, "addLabelIds": [label_id(gmail, args.label)], "removeLabelIds": ["INBOX"]}).execute()
    print(f"{args.label}: {len(ids)} писем из Входящих — {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
