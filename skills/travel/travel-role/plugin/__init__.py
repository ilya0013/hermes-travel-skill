"""Плагин Hermes `travel-links`: хук `transform_llm_output` дописывает к ответу о поездке ссылки на покупку, которых
в нём нет. Логика — `skills/travel/travel-role/scripts/travel_links.py`, отдельным процессом: шлюз держит модули в
памяти, а выкатка роли меняет скрипт без перезапуска; сбой скрипта — ответ уходит как есть. Плагин лежит в
`$HERMES_HOME/plugins/travel-links/`, корень Hermes — по своему пути."""

import subprocess
import sys
from pathlib import Path


def _links(response_text=None, session_id=None, **_):
    home = Path(__file__).resolve().parents[2]
    if not isinstance(response_text, str) or "до двери" not in response_text or not session_id \
            or not (home / "tmp" / f"travel_run.{session_id}").is_file():
        return None                                   # не ответ о поездке — процесс не запускается
    script = home / "skills" / "travel" / "travel-role" / "scripts" / "travel_links.py"
    try:
        proc = subprocess.run([sys.executable, str(script), "--session", session_id, "--home", str(home)],
                              input=response_text, capture_output=True, text=True, encoding="utf-8", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 and proc.stdout.strip() else None


def register(ctx):
    ctx.register_hook("transform_llm_output", _links)
