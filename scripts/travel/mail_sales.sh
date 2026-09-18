#!/bin/bash
# Письма рассылок перевозчиков в Gmail → ярлык avia-sale мимо Входящих — cron Hermes зовёт
# каждый час как `--script travel/mail_sales.sh` без агента (доставка local: stdout — в журнал
# запусков, человеку не шлётся, ИИ не зовётся). Питон Hermes, не venv источников: скрипт ходит
# токеном агента через скилл google-workspace (нужен Gmail). Лежит в $HERMES_HOME/scripts/travel/ —
# Hermes принимает cron-скрипты только оттуда. Путь @HERMES_HOME@ подставляет install.sh.
H="@HERMES_HOME@"
export HERMES_HOME="$H"
exec /opt/hermes/.venv/bin/python3 \
  "$H/skills/travel/travel-role/scripts/mail_sales.py" \
  --days 3
