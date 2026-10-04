#!/usr/bin/env python3
"""SlickTrip (MCP, бесплатный аккаунт): вход один раз и вызов инструмента — токен живёт только на сервере.

    /opt/data/travel/lib/venv/bin/python slicktrip.py login
    /opt/data/travel/lib/venv/bin/python slicktrip.py call search_flights @args.json
    /opt/data/travel/lib/venv/bin/python slicktrip.py book --run R 12+40 7

`book` — ссылка на оплату у авиакомпании по строкам вариантов (номера — как «строки 12+40» у report.py): те же рейсы
ищутся в SlickTrip (по номерам рейсов из строки Kiwi или по вылетам из строки Google), продавец — сама авиакомпания;
строка журнала `book` (USD, `of` → плечо) — ссылка, не цена: в сумму варианта не идёт, ссылка живёт сутки.

`login` печатает ссылку: открыть в браузере, войти в SlickTrip. Браузер уйдёт на http://127.0.0.1:53682/callback?code=…
и покажет «не удаётся подключиться» — так и должно быть: адрес из строки браузера целиком вставить сюда, Enter.
Ввода нет (агент без терминала) — `login` оставляет вход в ожидании, человек присылает адрес в чат, агент завершает:
`slicktrip.py login --url '<адрес>'`. Код из адреса без PKCE-verifier, который живёт только на сервере, бесполезен.
Токен — `$HERMES_HOME/travel/slicktrip_token.json` (0600), на экран не выводится; истёк — `call` сам обновит его
по refresh_token, не вышло — «вход SlickTrip истёк: slicktrip.py login».
Сервер (проверено живьём 04.10.2026): регистрация клиента на лету (/oauth/register), PKCE S256, клиент публичный,
grant authorization_code + refresh_token; без входа инструменты отвечают 401.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlencode

import journal

SOURCE_ID = "slicktrip_mcp"
ISSUER = "https://mcp.slicktrip.com"
MCP_URL = ISSUER + "/mcp"
REDIRECT = "http://127.0.0.1:53682/callback"
SCOPE = "openid email profile"
EARLY = 120   # обновить токен за 2 минуты до конца срока
PROTOCOL = "2025-06-18"


def token_path():
    return os.path.join(os.environ.get("HERMES_HOME", "/opt/data"), "travel", "slicktrip_token.json")


class LoginNeeded(RuntimeError):
    """Входа нет или он истёк — нужен `slicktrip.py login`."""


def _post(url, data, form=False):
    body = urlencode(data).encode() if form else json.dumps(data).encode()
    ctype = "application/x-www-form-urlencoded" if form else "application/json"
    req = urllib.request.Request(url, body, {"Content-Type": ctype, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def register(post=_post):
    return post(ISSUER + "/oauth/register", {
        "client_name": "travel-role", "redirect_uris": [REDIRECT], "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none", "scope": SCOPE})["client_id"]


def authorize_url(client_id, challenge, state):
    return ISSUER + "/oauth/authorize?" + urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "scope": SCOPE, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": MCP_URL})


def code_from(pasted, state):
    """Код из вставленного адреса (целиком или `?code=…`); чужой state или ошибка входа — ValueError."""
    query = parse_qs(pasted.strip().split("?", 1)[-1])
    if query.get("error"):
        raise ValueError(f"SlickTrip отказал во входе: {query['error'][0]}")
    if query.get("state", [None])[0] != state or not query.get("code"):
        raise ValueError("в адресе нет кода этого входа — скопируйте адрес из строки браузера целиком")
    return query["code"][0]


def save(token, path):
    """Токен с моментом получения: временный файл 0600 и атомарная замена — читатель не застанет пустой файл, старый
    файл с правами шире не останется (ревью 04.10.2026). Запуск от root — владельцем станет хозяин каталога (hermes)."""
    token = dict(token, obtained_at=int(time.time()))
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(token, fh)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        st = os.stat(folder)
        os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, path)
    return token


def _pending_path(path):
    """Вход в ожидании (client_id, verifier, state) — рядом с токеном."""
    return os.path.join(os.path.dirname(path or token_path()), "slicktrip_login.json")


def login(read=input, post=_post, path=None, pending=None):
    """Ссылка на вход и адрес из браузера. Ввода нет — вход остаётся в ожидании до `finish_login` (`login --url`)."""
    pending = pending or _pending_path(path)
    client_id = register(post)
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(16)
    save({"client_id": client_id, "verifier": verifier, "state": state}, pending)
    print("1. Откройте ссылку в браузере и войдите в SlickTrip:\n\n" + authorize_url(client_id, challenge, state) +
          "\n\n2. Браузер покажет «не удаётся подключиться» — это норма. Скопируйте адрес из строки браузера целиком "
          "и вставьте сюда, затем Enter:")
    try:
        pasted = read()
    except EOFError:
        print("\nввода нет — вход ждёт: адрес из браузера передать командой slicktrip.py login --url '<адрес>'")
        return 0
    finish_login(pasted, post, path, pending)
    return 0


def finish_login(pasted, post=_post, path=None, pending=None):
    """Токен по адресу из браузера и входу в ожидании; чужой адрес — ValueError, вход ждёт дальше."""
    pending = pending or _pending_path(path)
    try:
        with open(pending, encoding="utf-8") as fh:
            wait = json.load(fh)
    except FileNotFoundError:
        raise ValueError("входа в ожидании нет: сначала slicktrip.py login") from None
    code = code_from(pasted, wait["state"])
    token = post(ISSUER + "/oauth/token", {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                           "client_id": wait["client_id"], "code_verifier": wait["verifier"],
                                           "resource": MCP_URL}, form=True)
    if not token.get("access_token"):
        raise ValueError("SlickTrip не выдал токен — повторите вход")
    save(dict(token, client_id=wait["client_id"]), path or token_path())
    os.remove(pending)
    print("вход SlickTrip есть: токен сохранён на сервере" + ("" if token.get("refresh_token") else
          "; refresh_token сервер не дал — вход придётся повторять, когда токен истечёт"))


_REFRESH = threading.Lock()


def access_token(path=None, post=None, now=time.time, force=False):
    """Действующий токен; истёк (или `force` — сервер ответил 401) — обновить по refresh_token и сохранить. Нет файла
    или вход отозван (400/401 на обновлении) — LoginNeeded; сбой SlickTrip (5xx, 429, сеть) — RuntimeError.
    Под замком: потоки `book` не обновляют токен разом (ревью 04.10.2026: временный файл один на процесс — Errno 17)."""
    with _REFRESH:
        return _access_token(path, post, now, force)


def _access_token(path, post, now, force):
    path = path or token_path()
    try:
        with open(path, encoding="utf-8") as fh:
            tok = json.load(fh)
    except FileNotFoundError:
        raise LoginNeeded("входа SlickTrip нет: slicktrip.py login") from None
    if not force and now() < tok["obtained_at"] + int(tok.get("expires_in") or 3600) - EARLY:
        return _checked(tok["access_token"])
    if not tok.get("refresh_token"):
        raise LoginNeeded("вход SlickTrip истёк, refresh_token нет: slicktrip.py login")
    try:
        new = (post or _post)(ISSUER + "/oauth/token", {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                             "client_id": tok["client_id"], "resource": MCP_URL}, form=True)
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401):
            raise LoginNeeded(f"вход SlickTrip истёк (HTTP {exc.code}): slicktrip.py login") from None
        raise RuntimeError(f"SlickTrip не обновил токен: HTTP {exc.code} — сбой сервиса, повторить позже") from None
    if not new.get("access_token"):
        raise LoginNeeded("SlickTrip не выдал новый токен: slicktrip.py login")
    # refresh_token может не прийти заново — тогда действует прежний
    tok = save({**tok, **new, "refresh_token": new.get("refresh_token") or tok["refresh_token"]}, path)
    return _checked(tok["access_token"])


def _checked(token):
    """Токен с переводом строки уронил бы http.client трейсбеком с самим токеном внутри (ревью 04.10.2026)."""
    if not isinstance(token, str) or not token.isprintable() or " " in token:
        raise LoginNeeded("токен SlickTrip испорчен: slicktrip.py login")
    return token


def _rpc(body, token, sid=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "Authorization": f"Bearer {token}"}
    if sid:                                       # после initialize — сессия и версия протокола (MCP 2025-06-18)
        headers.update({"Mcp-Session-Id": sid, "MCP-Protocol-Version": PROTOCOL})
    req = urllib.request.Request(MCP_URL, json.dumps(body).encode(), headers)
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode("utf-8")
        sid = resp.headers.get("Mcp-Session-Id") or sid
        stream = "text/event-stream" in (resp.headers.get("Content-Type") or "")
    if stream:                                    # поток SSE: ответ — событие с id нашего запроса, уведомления мимо
        events = ["\n".join(l[5:].lstrip() for l in block.splitlines() if l.startswith("data:"))
                  for block in raw.replace("\r\n", "\n").split("\n\n")]
        answers = [json.loads(e) for e in events if e.strip()]
        return next((a for a in answers if a.get("id") == body.get("id")), {}), sid
    return (json.loads(raw) if raw.strip() else {}), sid


def _session(name, arguments, token):
    _, sid = _rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "travel-role", "version": "1"}}}, token)
    _rpc({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, token, sid)
    return _rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}}, token, sid)[0]


def call_tool(name, arguments, path=None):
    """Результат инструмента: structuredContent, иначе текст. 401 — токен обновляется и вызов повторяется один раз;
    снова 401 — LoginNeeded (вход отозван на стороне SlickTrip)."""
    try:
        res = _session(name, arguments, access_token(path))
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise
        try:
            res = _session(name, arguments, access_token(path, force=True))
        except urllib.error.HTTPError as again:
            if again.code == 401:
                raise LoginNeeded("вход SlickTrip отозван: slicktrip.py login") from None
            raise
    if "error" in res:
        raise RuntimeError(f"SlickTrip {name}: {res['error'].get('message')}")
    result = res.get("result") or {}
    if result.get("isError"):
        raise RuntimeError(f"SlickTrip {name}: " + " ".join(c.get("text", "") for c in result.get("content", [])))
    return result.get("structuredContent") or "\n".join(c.get("text", "") for c in result.get("content", []))


# у строки уже ссылка на покупку у перевозчика — SlickTrip не нужен
CARRIER_SOURCES = {"ryanair_api", "ryanair_site", "turkish_mcp", "wizzair_api", "wizzair_site", "lot_site"}
FLIGHT_NO = re.compile(r"\b(?:[A-Z]{2}|[A-Z]\d|\d[A-Z])\d{1,4}\b")     # Kiwi: «LOT Polish Airlines+Wizz Air LO171 W61552»
DEPARTURE = re.compile(r"\b([A-Z]{3}) (\d{4}-\d\d-\d\d \d\d:\d\d)→")    # Google: «WAW 2026-11-20 19:20→CDG …»
CLOCK = re.compile(r"\b(\d\d):(\d\d)\b")                                 # первое время в строке — вылет туда


def _window(clock):
    """Фильтр вылета у SlickTrip строгий — 19:20–19:20 пусто (живьём 04.10.2026): минута до и после."""
    t = int(clock.group(1)) * 60 + int(clock.group(2))
    out = {}
    if t > 0:
        out["depart_after"] = f"{(t - 1) // 60:02d}:{(t - 1) % 60:02d}"
    if t < 24 * 60 - 1:
        out["depart_before"] = f"{(t + 1) // 60:02d}:{(t + 1) % 60:02d}"
    return out


def _same(part, numbers, departures):
    """Те же рейсы: номера из строки (Kiwi) или вылеты «аэропорт дата время» (Google)."""
    segs = part.get("segments") or []
    if numbers:
        return bool(segs) and all(s.get("flight_number") in numbers for s in segs)
    return [(s.get("origin"), str(s.get("departs_at")).replace("T", " ")) for s in segs] == departures


def _flights(part):
    return ", ".join(f"{s.get('flight_number')} {s.get('origin')} {str(s.get('departs_at')).replace('T', ' ')}"
                     for s in part.get("segments") or [])


def _book_row(row, head, call):
    """(строка вывода, строка журнала или None) для одного плеча варианта."""
    route, dates, raw = str(row.get("route") or ""), str(row.get("dates") or "").split("/"), str(row.get("raw") or "")
    if row.get("source_id") in CARRIER_SOURCES:
        return f"{head}: ссылка перевозчика уже в строке", None
    if "/" in route or len(route.split("-")) != 2:
        return f"{head}: открытая связка — SlickTrip ищет туда-обратно в один город; ссылка из строки", None
    numbers, departures, clock = set(FLIGHT_NO.findall(raw)), DEPARTURE.findall(raw), CLOCK.search(raw)
    if not (numbers or departures) or not clock:
        return f"{head}: в строке нет ни номеров рейсов, ни вылетов — сверять не по чему; ссылка из строки", None
    origin, dest = route.split("-")
    args = {"origin": origin, "destination": dest, "depart_date": dates[0], "travelers": row.get("pax") or 1,
            "max_results": 50, **_window(clock)}
    if len(dates) == 2:
        args["return_date"] = dates[1]
    out = next((it for it in call("search_flights", args).get("itineraries") or []
                if _same(it.get("outbound") or {}, numbers, departures)), None)
    if not out:
        return f"{head}: этих рейсов в SlickTrip нет; ссылка из строки", None
    trip, back = out, ""
    if len(dates) == 2:                  # в строке Google только рейсы туда: обратно — самый дешёвый, так считает и Google
        found = call("search_return_flights", {"itinerary_id": out["itinerary_id"], "max_results": 50})
        rets = [r for r in found.get("itineraries") or [] if isinstance((r.get("price") or {}).get("total_usd"), (int, float))
                and (not numbers or _same(r.get("return") or {}, numbers, None))]
        trip = min(rets, key=lambda r: r["price"]["total_usd"], default=None)
        if not trip:
            return f"{head}: обратного рейса этой строки в SlickTrip нет; ссылка из строки", None
        back = f"; обратно {_flights(trip.get('return') or {})}"
    sellers = call("booking_options", {"itinerary_id": trip["itinerary_id"]}).get("sellers") or []
    # перевозчик рейса под своим именем — у перевозчика, даже без флага: эвал 04.10.2026 — Wizz Air с airline_direct
    # false, ссылка 302 на wizzair.com
    carriers = {str(s.get("airline") or "").lower() for part in (out.get("outbound"), trip.get("return"))
                for s in (part or {}).get("segments") or []} - {""}
    direct = [s for s in sellers if (s.get("airline_direct") or str(s.get("seller") or "").lower() in carriers)
              and not s.get("separate_tickets") and s.get("book_url")
              and isinstance(s.get("price_usd"), (int, float))]   # раздельные билеты — ссылка не на всю поездку (ревью)
    if not direct:                       # агентство — не «у перевозчика»: связку агентства и так даёт Kiwi
        others = ", ".join(f"{s.get('seller')} {s.get('price_usd')} USD" for s in sellers) or "продавцов нет"
        return f"{head}: у авиакомпании не продаётся — {others}; ссылка из строки", None
    s = min(direct, key=lambda s: s["price_usd"])
    text = f"{s['seller']} напрямую {s['price_usd']} USD; туда {_flights(out.get('outbound') or {})}{back}"
    new = journal.observation(row["run"], "book", SOURCE_ID, s["book_url"], row["route"], row["dates"],
                              float(s["price_usd"]), "USD", "QUOTED", f"{text}; SlickTrip, ссылка на сутки",
                              pax=row.get("pax") or 1, of=row["id"], seller=s["seller"])
    return f"{head} → у перевозчика {text}: {s['book_url']}", new


def book(run, specs, journal_path, call=None, workers=4):
    """Ссылки у авиакомпании для перелётов среди строк `specs` («12+40», номера или хвосты id, как у report.py):
    (строки вывода, строки журнала `book`). Дорога, доплаты и CALC — мимо; сбой SlickTrip на плече — словами,
    остальные плечи идут; вход потерян — LoginNeeded."""
    import report                        # номера строк — те же, что в `--list` и «строки …» вариантов
    call = call or call_tool
    rows, _ = report.run_rows(journal_path, run)
    num = {r["id"]: i for i, r in enumerate(rows, 1)}
    picked = {}
    for spec in specs:
        for token in re.split(r"[+,\s]+", spec.strip()):
            if token:
                r = report.pick(token, rows)
                picked.setdefault(r["id"], r)
    legs = [r for r in picked.values() if r.get("kind") in report.FLIGHT_KINDS and r.get("source_id") != "CALC"]

    def one(r):
        head = f"строка {num[r['id']]} {r.get('source_id')} {r.get('route')} {r.get('dates')}"
        try:
            return _book_row(r, head, call)
        except LoginNeeded:
            raise
        except Exception as exc:         # noqa: BLE001 — сбой SlickTrip на одном плече не роняет остальные
            return f"{head}: SlickTrip не ответил — {str(exc)[:120]}; ссылка из строки", None
    with ThreadPoolExecutor(max_workers=workers) as pool:      # плечо — 2–3 вызова по ~3 с (живьём 04.10.2026)
        done = list(pool.map(one, legs))
    return [line for line, _ in done], [new for _, new in done if new]


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "book":
        ap = argparse.ArgumentParser(prog="slicktrip.py book", description="ссылка на оплату у авиакомпании по строкам")
        ap.add_argument("rows", nargs="+", help="номера строк прогона, как «строки 12+40» у report.py")
        journal.add_common_args(ap)
        args = ap.parse_args(sys.argv[2:])
        if not args.run:
            ap.error("--run обязателен: строки берутся из прогона")
        try:
            access_token()                # токен обновляется раз — до параллельных вызовов
            lines, new = book(args.run, args.rows, args.journal)
        except LoginNeeded as exc:
            print(f"{exc} — ссылки из строк", file=sys.stderr)
            return 3
        except (RuntimeError, urllib.error.URLError) as exc:   # сбой обновления токена: 5xx, 429, сеть
            print(f"SlickTrip не ответил: {str(exc)[:120]} — ссылки из строк", file=sys.stderr)
            return 1
        print("\n".join(lines) or "перелётов среди этих строк нет")
        if new:
            journal.finish(args, args.run, new, show=False)
        return 0
    if len(sys.argv) >= 2 and sys.argv[1] == "login":
        try:
            if len(sys.argv) > 2:                # кривой `--url` не должен молча начать новый вход и затереть ожидание
                if len(sys.argv) != 4 or sys.argv[2] != "--url":
                    raise ValueError("так: slicktrip.py login --url '<адрес из браузера>'")
                finish_login(sys.argv[3])
            else:
                login()
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        return 0
    if len(sys.argv) == 4 and sys.argv[1] == "call":
        arg = sys.argv[3]
        args = json.load(open(arg[1:], encoding="utf-8")) if arg.startswith("@") else json.loads(arg)
        try:
            out = call_tool(sys.argv[2], args)
        except LoginNeeded as exc:
            print(exc, file=sys.stderr)
            return 3
        print(out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
