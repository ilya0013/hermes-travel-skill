#!/bin/bash
# Установка travel-роли в Hermes Agent (образ Hostinger, HERMES_HOME=/opt/data).
#
#   git clone <url> /tmp/hermes-travel-skill && bash /tmp/hermes-travel-skill/install.sh [--check]
#
# Ставит: skills/travel/travel-role (роль), travel/lib/venv (venv источников через uv),
# каталоги travel/{reports,interest,watch}, обёртку scripts/travel/interest_digest.sh для cron.
# Повторный запуск безопасен: роль перезаписывается, profile.yaml и журнал не трогаются.
# --check — живой вызов Kiwi (без ключа) на пробные даты, строки в журнал не пишутся.
# Ключи не нужны: Kiwi MCP, Ryanair, Google Flights (fast-flights), AZair, Flixbus и ленты — без них.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
H="${HERMES_HOME:-/opt/data}"
CHECK=0
for a in "$@"; do
  case "$a" in
    --check) CHECK=1 ;;
    --home=*) H="${a#--home=}" ;;
    *) echo "неизвестный аргумент: $a (есть --check, --home=DIR)"; exit 2 ;;
  esac
done
ROLE="$H/skills/travel/travel-role"
VENV="$H/travel/lib/venv"
PY="$VENV/bin/python"

echo "HERMES_HOME=$H"
[ -d "$H" ] || { echo "нет каталога $H — задай HERMES_HOME или --home=DIR"; exit 1; }
command -v uv >/dev/null || { echo "нет uv (в образе Hostinger он есть: /usr/local/bin/uv)"; exit 1; }
# uv читает pyproject.toml от cwd вверх: из /opt/hermes он берёт exclude-newer="14 days" и не находит
# flywizz 0.1.0 (проверено 15.09.2026). Все пути ниже абсолютные — работаем из корня, без чужих конфигов.
cd /
UV="uv --no-config"

# 1. роль: файлы перезаписываются, profile.yaml владельца остаётся
mkdir -p "$ROLE"
if [ -f "$ROLE/profile.yaml" ]; then
  cp "$ROLE/profile.yaml" "/tmp/travel-profile.$$"
fi
cp -r "$SRC/skills/travel/travel-role/." "$ROLE/"
if [ -f "/tmp/travel-profile.$$" ]; then
  mv "/tmp/travel-profile.$$" "$ROLE/profile.yaml"
  echo "роль обновлена, profile.yaml прежний"
else
  echo "роль установлена, profile.yaml — Варшава по умолчанию (Шопен 4,40 zł, Модлин 35 zł, 1 взрослый)"
fi
find "$ROLE" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

# 2. venv источников
if [ ! -x "$PY" ]; then
  $UV venv --python 3.13 "$VENV" >/dev/null
  echo "venv создан: $VENV"
fi
$UV pip install --python "$PY" -q -r "$SRC/requirements.txt"
"$PY" -c "import mcp, fast_flights, flywizz, ryanair, requests" || { echo "пакеты venv не импортируются"; exit 1; }
echo "venv: $($UV pip list --python "$PY" 2>/dev/null | grep -Ei '^(mcp|fast-flights|flywizz|ryanair-py|requests) ' | tr -s ' ' | tr '\n' ';')"

# 3. каталоги данных и обёртка cron
mkdir -p "$H/travel/reports" "$H/travel/interest" "$H/travel/watch" "$H/scripts/travel"
touch "$H/travel/observations.jsonl"
sed "s|@HERMES_HOME@|$H|g" "$SRC/scripts/travel/interest_digest.sh" > "$H/scripts/travel/interest_digest.sh"
chmod +x "$H/scripts/travel/interest_digest.sh" "$ROLE"/scripts/*.py

# 4. проверка: скрипты запускаются, роль видна Hermes
"$PY" "$ROLE/scripts/kiwi_search.py" --help >/dev/null
"$PY" "$ROLE/scripts/deal_feeds.py" --help >/dev/null
HPY=/opt/hermes/.venv/bin/python3
if [ -x "$HPY" ]; then
  HERMES_HOME="$H" "$HPY" "$ROLE/scripts/report.py" --help >/dev/null \
    || { echo "report.py не запускается под $HPY — установка не завершена"; exit 1; }
  echo "report.py под питоном Hermes: ок"
else
  echo "внимание: нет $HPY — report.py запускается питоном Hermes, путь в SKILL.md поправить"
fi
if command -v hermes >/dev/null; then
  # grep без -q: с -q он закрывает канал на первом совпадении, hermes ловит SIGPIPE и pipefail роняет проверку
  HERMES_HOME="$H" hermes skills list 2>/dev/null | grep travel-role >/dev/null && echo "hermes skills list: travel-role виден" \
    || echo "внимание: travel-role не в hermes skills list — проверь HERMES_HOME агента"
fi
if [ "$CHECK" = 1 ]; then
  D1=$(date -u -d "+30 days" +%Y-%m-%d); D2=$(date -u -d "+33 days" +%Y-%m-%d)
  HERMES_HOME="$H" "$PY" "$ROLE/scripts/kiwi_search.py" WAW BCN "$D1" "$D2" --top 2 --dry-run \
    && echo "Kiwi отвечает: ок" || echo "внимание: Kiwi не ответил (сеть или mcp.kiwi.com), установка при этом цела"
fi

cat <<EOF
готово:
  роль      $ROLE  (SKILL.md, profile.yaml, references/, scripts/)
  venv      $VENV
  журнал    $H/travel/observations.jsonl, отчёты $H/travel/reports/
  интерес   $H/travel/interest/  + cron-обёртка $H/scripts/travel/interest_digest.sh
дальше — INSTALL.md: вопросы человеку и profile.yaml.
EOF
