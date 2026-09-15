#!/bin/bash
# Свод по интересам — cron Hermes зовёт каждый день как `--script travel/interest_digest.sh`
# (расписание `0 9 * * *`). Сбор ежедневный: превью Telegram отдаёт ~20 постов (~3 дня).
# Наводки копятся в pending.md, в stdout идут одним сводом по понедельникам; в остальные дни и
# без интересов stdout пуст — агент не просыпается. Ненулевой код (ни одна лента не прочитана) —
# Hermes доложит владельцу сам. Лежит в $HERMES_HOME/scripts/travel/ — Hermes принимает
# cron-скрипты только оттуда. Путь @HERMES_HOME@ подставляет install.sh.
H="@HERMES_HOME@"
exec "$H/travel/lib/venv/bin/python" \
  "$H/skills/travel/travel-role/scripts/deal_feeds.py" \
  --interest "$H/travel/interest" \
  --seen "$H/travel/interest/seen.txt" \
  --pending "$H/travel/interest/pending.md" \
  --digest-weekday 1 \
  --days 14
