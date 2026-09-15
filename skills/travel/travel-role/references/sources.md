# Источники цен: скрипты роли и что они дают

Проверено живыми запросами с VPS 11–13.09.2026. Все скрипты лежат в
`/opt/data/skills/travel/travel-role/scripts/`, запускаются venv источников:

    /opt/data/travel/lib/venv/bin/python /opt/data/skills/travel/travel-role/scripts/<скрипт> …

Общие ключи: `--run <id>` (прогон; без него скрипт заводит новый и печатает
его — дальше передавать), `--dry-run` (напечатать, в журнал не писать),
`--journal <файл>` (только для проверок; боевой журнал — по умолчанию).
Каждый скрипт сам дописывает строки в журнал и печатает их вместе с `id`;
`report.py --run R --list` нумерует строки прогона — номера идут в варианты отчёта.

## Иерархия доверия (v2, 15.09.2026)

1. **Fare-finder перевозчика** — `ryanair_fares.py`: официальный JSON без ключа, цена того
   рынка, который просим (`currency=PLN`; без параметра BUD→WMI отдал HUF — проверено
   15.09.2026 с VPS в Куала-Лумпуре, IP на цену не влияет). Ссылка на покупку — `link` строки.
2. **Google Flights** — `google_flights.py` (fast-flights, без браузера): все перевозчики,
   перекрёстная проверка, дальние маршруты. Хрупкий (валюта не наблюдается), необязательный.
3. **Kiwi MCP** — `kiwi_search.py`: обнаружение связок, багаж, окно дат, ссылка `bookingUrl`.
   Холодный вызов отдаёт неполную выдачу (15.09.2026: WAW→CPT 4 930 → 3 400 zł через 2 с) —
   скрипт повторяет его сам по `searchTimeMs`; время в пути плеча — в `raw` строки (`31h40`).
   Первый для «что вообще есть», последний для цены Ryanair/Wizz: Kiwi нашёл → перевозчик
   подтвердил → ссылка перевозчика.
4. **Своими глазами** — `web_search`, `web_extract`, браузер → строка `web_site` через
   `journal_add.py` с url и фрагментом; статус не выше QUOTED.
Карта минимумов Wizz и кэш Aviasales — только для выбора дат (ORIENTIR), ценой не являются.
AZair (`azair_search.py`) — связки лоукостеров по окну дат, в том числе из двух-трёх раздельных
билетов через третий город; кэш с возрастом проверки, ORIENTIR (15.09.2026: WMI→BCN октябрь от
272 zł, WAW+WMI→BUD от 129 zł — 144 связки). Словарь — 304 аэропорта, Азии нет. В вариант
строка ORIENTIR не идёт (`report.py` откажет): AZair показывает, что бывает, цену даёт перевозчик.
Ленты охотников (`deal_feeds.py --match`) — на каждый запрос, не только по интересу: распродажа
или ошибочный тариф, которых перебор дат не находит (15.09.2026: LOT WAW→BKK+PQC за €558 на
fly4free.com). Наводка — ORIENTIR в валюте ленты; цену подтверждает источник выше по списку.

## Откаты: источник упал → чем заменить

| Упало | Признак | Замена | Пометка в ответе |
|---|---|---|---|
| `ryanair_fares.py` | HTTP 4xx/5xx, пусто | `google_flights.py --airline Ryanair`, затем Kiwi `--airlines FR`, затем страница ryanair.com своими глазами | `--failed ryanair_api=<код>` |
| `google_flights.py` | код 1, «парсер fast-flights упал» (IndexError на варианте без цены — сбой источника, не «рейсов нет»: 15.09.2026 WAW→SIN у Kiwi был) | Kiwi без фильтра перевозчика; страница Google из stderr браузером; для Wizz — `wizz_farechart.py` (дата) + сайт своими глазами | `--failed google_flights=<причина>` |
| `kiwi_search.py` | MCP не отвечает, пустая выдача | fare-finder + Google по плечам; связки — `--self-transfer` не подменять руками | `--failed kiwi_mcp=<причина>` |
| `flixbus_fares.py` | HTTP 400 (чужой id), пусто | профиль: ориентир Модлина; сайт flixbus своими глазами | строка профиля печатается сама |
| курс НБП | `❌ nbp_api` в дайджесте | ничего: цена в валюте источника с пометкой «по курсу, на сайте может отличаться» | ставит `report.py` |
| Диск | «Таблица на Диск не загрузилась» | черновик `/opt/data/travel/reports/<run>.md`, ссылка позже | ставит `report.py` |
| `report.py` | исключение | черновик руками из строк `--list` с пометкой «цифры не пересчитаны» | руками |
Последний откат везде — `web_search` и браузер с пометкой «с глаз». Бот-гейт (Wizz, Kiwi,
Ryanair 403) не обходим: фиксируем отказ.

| Скрипт | `source_id` | Что даёт | Статус | Чего не даёт |
|---|---|---|---|---|
| `kiwi_search.py ORIGIN DEST DATE [BACK] …` или `@args.json` | `kiwi_mcp` | round-trip и one-way с `bookingUrl`, багажом, сегментами; окно дат (`--out-to`, `--nights 3-4`), фильтр перевозчиков (`--airlines AY,KL,LO`), багаж в цене (`--hold-bags 1`), explore по стране (`@args.json` с `"flyTo": "Spain", "one_for_city": true`) | QUOTED | CONFIRMED — только страница тарифа продавца по `bookingUrl`; лимиты MCP не опубликованы |
| `ryanair_fares.py ORIGIN DEST DATE [BACK] [--flex N]` · `ryanair_fares.py ORIGIN DEST --month YYYY-MM` | `ryanair_api` | минимум по дню (Basic, 1 взрослый) для окна ±N; с обратной датой — сумма двух one-way (CALC); `--month` — минимум на каждый день месяца в обе стороны одним запросом на плечо (`cheapestPerDay`, проверен 13.09.2026), все `other_date`; у каждой строки `link` — страница выбора рейса на плечо и день (ссылка на покупку без браузера) | QUOTED | тарифы Regular/Plus (availability отдаёт 409); из WAW в Барселону не летает — брать WMI |
| `flixbus_fares.py ORIGIN DEST DATE --route WAW-WMI [--pax N] [--after HH:MM --before HH:MM] [--top 3]` | `flixbus_api` | автобус до аэропорта и обратно: города словами (id из автокомплита при каждом вызове), на каждый из `--top` самых дешёвых рейсов окна — цена (`fare`) и сбор платформы (`fee`, `of` → рейс); проверен 13.09.2026 | QUOTED | цену на месте и в кассе; маршруты без Flixbus (Варшава ↔ Шопен) |
| `google_flights.py ORIGIN DEST DATE [BACK] [--max-stops N] [--airline NAME]` | `google_flights` | блок «лучшие» Google Flights (~6 вариантов, все перевозчики, стыковки) без браузера | QUOTED, `currency_observed: false` | эталон «Statystyki cen» — только браузером; полную выдачу |
| `google_flights.py ORIGIN DEST DATE --airline Wizz` — плечо Wizz на дату | `google_flights` | живая цена плеча Wizz (`--airline` = только этот перевозчик, прямые рейсы: без него блок «лучшие» прячет прямой Wizz за стыковками, 14.09.2026): Wizz сам отдаёт тарифы в Google Flights, цена равна сайту продавца (14.09.2026: сайт 39,99 EUR, Google 40 EUR, скрипт 174 PLN) | QUOTED | карта минимумов Wizz этой ценой не является; ссылка на покупку — `link` строки `wizz_farechart.py` |
| `wizz_farechart.py ORIGIN DEST DATE [BACK] [--flex 3..10]` | `wizzair_api` | карта минимумов Wizz по дням (Basic) — матрица дат, не живой тариф (14.09.2026: карта 31,99 EUR, сайт 39,99); `link` — форма покупки Wizz с маршрутом, датой и пассажиром | QUOTED | обратное плечо из-за границы — в EUR как есть, рядом строка курса НБП (`nbp_api`, kind `rate`, одна на валюту) и CALC `convert` в PLN — в отчёт идёт convert; тарифы и полная выдача — за бот-гейтом Kasada (429); `--flex` больше 10 API отвергает (`DayIntervalMustBeLessOrEqualTo10`, Лиза 14.09.2026) — длинное окно двумя вызовами |
| `azair_search.py ORIGIN DEST --out-from ISO --out-to ISO --nights 2-4 [--extra-from WMI] [--extra-to GRO] [--max-changes 1] [--adults N] [--top 5] [--max-age-h 336] [--one-way]` | `azair_site` | связки 62 лоукостеров (Ryanair, Wizz, easyJet, Volotea, Pegasus, flydubai, Norse…) по окну дат: вся связка одной ценой, плечи с перевозчиком, рейсом, ценой и возрастом проверки в `raw`; `link` — постоянная ссылка на связку; серверный HTML, без ключа, PLN; коды IATA, имя аэропорта серверу не нужно (проверено 15.09.2026) | ORIENTIR всегда: кэш AZair; связка из нескольких билетов — `self_transfer_allowed: true` | живую цену — подтверждать плечо у перевозчика (`ryanair_fares.py`, Google `--airline`); Азию и Доху — их нет в словаре AZair (304 аэропорта); плечо с ценой давнее `--max-age-h` (14 суток) или без отметки проверки не пишется — живьём встретилось 5355 h; в вариант `report.py` строку ORIENTIR не складывает — подтверди плечо у перевозчика и складывай его строку |
| `deal_feeds.py --run R --match "слова" [--from WAW] [--days 14] [--from-poland] [--seen файл]` · `--interest <каталог>` | `<лента>_feed` | наводки из лент охотников: RSS fly4free.pl/.com, wakacyjnipiraci, holidaypirates, pepper, vandrouki.by и превью Telegram `t.me/s/vandroukiby`, `t.me/s/wakacyjnipiracipl`; совпадения по городам, странам, авиакомпаниям — по началу слова: `tajland` найдёт Tajlandia/Tajlandii, `azja` не найдёт `okazja` (подстрока дала 23 чужих наводки из 24, 15.09.2026); `--from-poland` — только вылет из Польши словами; цена в валюте ленты как есть (EUR/GBP у fly4free.com), злотые считает `report.py`; `--interest` — режим cron по файлам интереса (ежедневно, `--pending` копит, свод по `--digest-weekday`), stdout только новые наводки | ORIENTIR, `kind: lead` | цену на даты — это реклама «od»; проверять источником (Kiwi с `--airlines`); наводки без цены в заголовке в журнал не идут; российские источники не подключать (владелец, 13.09.2026) |
| `journal_add.py --run R …` | любой из словаря, для «с глаз» — `web_site` | строка руками из аргументов: наблюдение браузером или страницей (`--source-id --url --value --currency --status --raw`), доплата (`--kind fee --of ID`), сумма или разность (`--operator sum|diff --inputs ID,ID`, value считается сам) | по аргументу | — |
| `watch_route.py <задание>.json` | `kiwi_mcp` (свой журнал дозора) | один тик дозора: стабильный stdout для `--monitor-script`, рядом `<имя>.jsonl` и `<имя>.latest.md` с id и ссылками | QUOTED | см. `references/watch.md` |
| `report.py --run R --title … --auto --nights 2-3 [--bags 1] [--home WAW,WMI] [--variant "B=5+7"] [--failed id=причина] [--no-drive]` (питон Hermes, `HERMES_HOME=/opt/data`) | `nbp_api`, `CALC` | `--auto` собирает поездки из строк прогона сам (одной строкой и парами плеч, автобус на даты плеч) и печатает самые дешёвые до двери: `auto-1` — ★; курс раз на прогон, злотые, дорога по `profile.yaml`, блок источников, таблица всех строк в Google Doc папки «Поездки»; `--list` — строки с номерами | — | ничего не ищет: только считает и показывает строки журнала |

## Что установлено про каналы

* **Kiwi MCP** `https://mcp.kiwi.com/` — единственный источник round-trip с багажом и
  ссылкой на покупку. Даты у инструмента `dd/mm/yyyy` (скрипт переводит из ISO), валюта по
  умолчанию EUR (скрипт ставит PLN), `allow_self_transfer` по умолчанию `true` (скрипт ставит
  `false`; `--self-transfer` включает и помечает строки `self_transfer_allowed: true`). Kiwi сам
  подставляет соседний аэропорт: в `route` строки — фактические коды (`WMI-BCN`), не запрошенные.
  Окно дат бьёт точную дату (11.09.2026: 275 PLN против 1093 на WAW→BCN), explore по стране —
  ещё дешевле (258 PLN Мадрид). На дальних маршрутах Kiwi и Google показывают разные связки с
  разницей в тысячи злотых (WAW→PQC февраль 2027: 4435 у Kiwi против 6813 у Google).
* **Ryanair fare-finder** (`services-api.ryanair.com/farfnd/v4`) — официальный, без ключа;
  round-trip одной ценой не отдаёт.
* **Google Flights через fast-flights** — те же данные, что видит браузер, но только топ «лучших»
  и без блока статистики цен. Валюта в ответе отсутствует — контроль 11.09.2026: EUR 220 ↔ PLN 880.
* **Wizz `asset/farechart`** — POST, в строке журнала url = эндпоинт, тело — в `request`.
  `checkPrice` с нулём — цены в карте нет, скрипт такую строку не пишет.
* **Ленты** — RSS 2.0, HTTP 200 без защиты (11.09.2026). Secret Flying ленты не имеет.
* **Закрыто**: Skyscanner (партнёрский API; клиенты с PXSolver — обход защиты, не используем),
  Amadeus Self-Service (закрыт 17.07.2026), Expedia MCP (403 без ключа), сайт Kiwi (403),
  сайт Wizz (DataDome), Kayak/Momondo без JS.
* **AZair** (`azfin.php`, GET) — разобран 15.09.2026 сабмитом формы в браузере: серверу нужен
  только код в скобках (`X [WAW] (+WMI)`), `indexSubmit=Search`, дни включительно
  (`minDaysStay`/`maxDaysStay`), результаты — `div.result` с `tp`-JSON (итог и цены плеч),
  `data-age` — часы с проверки цены, `div.bookmark` — постоянная ссылка. С VPS: 200, 938 КБ, 2,6 с,
  бот-гейта нет. Свой API AZair отдаёт «on contractual basis» — не берём.

Поведение сайтов в headless-браузере (Google Flights, Ryanair, Booking, Flixbus) —
`references/sources-map.md`.
