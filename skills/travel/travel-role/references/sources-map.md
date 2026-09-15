# Поведение travel-сайтов с headless (VPS, 09.2026) — разбор сессии Варшава→Будапешт

Контекст: поиск билетов WAW→BUD 11–14.09.2026 и отеля. Браузер: Chrome for Testing headless (CDP 9222), IP VPS — гео Малайзия (Google показывает MYR).

## Работает с headless
- **Google Flights** (`google.com/travel/flights?q=...&curr=PLN`): выдаёт цены за даты из URL. Читать `document.body.innerText`, искать «from PLN». Делает «Travel Sep 9–12 for PLN 368» подсказки-бонусы.
- **Ryanair** (`ryanair.com/pl/pl/trip/flights/select?...&originIata=WMI&destinationIata=BUD`): полная выдача, календарь дней, тарифы Basic/Regular/Plus с доплатами («+ zł X w ramach każdego lotu» = за каждый полёт). Даты в URL принимает. Редиректит на тот же URL с tp* параметрами — норм.
- **Booking hotel page** (`booking.com/hotel/hu/<slug>.html`): открывается; адрес, рейтинг (X/10), число отзывов, локация, тип номеров читаются. Неверный slug → «Nie znaleziono strony» (404) — искать правильный slug через web_search.
- **Agoda city page**: открывается, календарь кликается (div с aria-label вида «Fri Sep 11 2026»), но поиск потом падает («Wystąpił problem... Wyniki 0») — не тратить время.

## Блокирует / режет
- **Kiwi.com** → HTTP 403 (анти-бот) на любой URL, даже с /pl/.
- **Wizz Air** → DataDome-капча: сначала «Let's confirm you are human / Begin», затем картинки («Choose all the clocks»). Капчу не обходить; цена плеча Wizz на дату — `google_flights.py` (равна сайту, проверено 14.09.2026). Форма покупки по шаблонной ссылке `wizzair.com/en-gb/booking/select-flight/<откуда>/<куда>/<дата>/null/1/0/0/null` у человека открывается с подставленными рейсом и датой (владелец, 14.09.2026); из браузера агента та же форма открывается, но список рейсов не грузится (429) — ссылку отдавать, не открывать.
- **Booking.com searchresults** — любой URL (ss=, dest_id=-850553, checkin/checkout, lang=en-us) редиректит на главную или city page, срезая параметры. Только UI.
- Снято Лизой 11.09.2026 (прогоны Барселона/Вена, перенесено из её блокнота 14.09): **lot.com** —
  «Access to the website has been blocked» по IP; **wtp.waw.pl, ztm.waw.pl** — CloudFront 403
  (билет ZTM — только словами, без цифры); **esky.pl** — «Access Denied»; **pl.trip.com** — whaleguard;
  `be.wizzair.com/<версия>/Api/...` — 404 на угаданные версии, не перебирать. Заблокированный
  продавец не отменяет цифру: цена с `travel/flights/booking` Google идёт как QUOTED с источником
  `google_flights`, сам сайт — в «не проверено:».
- **Google Flights в браузере, запрос словами `?q=…for 1 adult`** (снято Лизой 11.09.2026,
  Варшава→Вена): выдача «dla 2 osób» — сдвиг на +1; без фразы о пассажирах выдача за одного,
  число читать в строке «Ceny obejmują … dla N osób». К `google_flights.py` не относится: он
  задаёт пассажиров параметром (fast-flights `Passengers`), не фразой. Группу считает
  перевозчик (`adults=N` в select-странице Ryanair). Клик по строке рейса не срабатывает —
  кликать по элементу с ценой. Deep-link `travel/flights/booking?tfs=…` при перезагрузке уводит
  на главную — читать в той вкладке, где открылся.
- **Ryanair select-страница one-way** (снято Лизой 14.09.2026, WMI→BCN): один клик «Wybierz»
  открывает модалку «Wybierz taryfę» сразу (лестница Basic/Regular/Plus/Flexi); цена — в валюте
  страны вылета (из BCN — €). Пустой `fares[]` у fare-finder = в этот день рейса нет, не
  «продано» (13.09.2026). **Flixbus** (11.09.2026): плеч «аэропорт ↔ город» нет у Шопена и
  Эль-Прата — такой трансфер в «не проверено:».

## Селекторы Booking (city/hotel page)
- Кнопка дат: `button[data-testid="searchbox-dates-container"]`.
- Дни календаря: `span[data-date="2026-09-11"]` — кликать по data-date, НЕ по [aria-label] (попадает в td/span, может не сработать).
- Поля отображения: `[data-testid="date-display-field-start"]` / `-end`.
- Футер календаря `[data-testid="datepicker-footer"]` — кнопки без текста; флоу капризный. Escape сбрасывает выбранные даты.
- Кнопка «Szukaj» под поповером может не срабатывать, пока поповер открыт.
- Итог: максимум 2 попытки UI-календаря, дальше — отдать пользователю ссылку с checkin/checkout.

## Google Hotels
- `google.com/travel/hotels?q=Budapest&curr=PLN` редиректит на `/travel/search?q=...`; **даты из URL игнорируются** (checkin_date/checkout_date/checkin) — всегда дефолтные 1-2 ночи («Ceny dla przedziału 7–8 wrz»).
- UI-календарь (клик по чипу дат / «Zmień daty») с headless не открывается надёжно.
- Валюта по гео IP: MYR (×0.92 ≈ PLN по xe/keycurrency). `curr=PLN` в URL не перебивает.
- Ценность: названия реальных отелей, рейтинг (Google 4.x/5), «Doskonała lokalizacja», цена за ночь за стандартный номер — как ОРИЕНТИР + кандидаты для ссылок.

## Валюты/цифры сессии (для сверки методики)
- Google Flights WAW→BUD (Wizz, Basic): 10–13.09 = 788 zł; 11–13.09 = 478 zł; 11–14.09 = 458 zł; бонус 9–12.09 = 368 zł.
- Ryanair WMI↔BUD 11–14.09: Basic 217,33+111,33 = 328,66 zł; Regular = +102,60/полёт (место + ручная кладь 10 кг).
- Ryanair WMI–BUD летает пн/ср/пт (обратно пт/пн) — чт/сб/вс рейсов нет.
- Модлин: 40 км от Варшавы, ~1 ч трансфер. Обратный FR1923 в 05:45.

## Flixbus — работает, официальный API
GET https://global.api.flixbus.com/search/service/v4/search
  ?from_city_id=...&to_city_id=...&departure_date=DD.MM.YYYY
  &products=%7B%22adult%22%3A1%7D&currency=PLN&locale=pl
  &search_by=cities&include_after_midnight_rides=1
ID городов: /search/autocomplete/cities?q=Warszawa&lang=pl&country=PL
Ключ не нужен. Round-trip одной строкой не отдаёт — сумма двух
one-way это CALC, не цена продавца.
Скрипты писать только в /opt/data/tmp/ — /tmp запрещён.

## Ryanair fare-finder API — работает
Официальный endpoint отдаёт цены Basic по датам в PLN, ключ не нужен.
Проверено 10.09.2026 на WMI–BUD. Endpoint availability отдаёт 409 на
всех вариантах — тарифы Regular/Plus через него не берутся.

URL, которым пользовалась 10.09.2026 (скрипт /opt/data/tmp/ryanair_farfnd.py):
GET https://www.ryanair.com/api/farfnd/v4/oneWayFares
  ?departureAirportIataCode=WMI&arrivalAirportIataCode=BUD
  &outboundDepartureDateFrom=2026-09-01&outboundDepartureDateTo=2026-10-05
  &currency=PLN&market=pl-pl&adultPaxCount=1&searchMode=ALL
Заголовки: User-Agent (браузерный), Accept-Language: pl-PL, Referer
https://www.ryanair.com/pl/pl. Ответ: fares[].outbound.price.value в PLN
+ departureDate/arrivalDate — это минимум по дню (Basic), не срез всех
тарифов и не round-trip: связка двух one-way это CALC.
Второй endpoint (availability, /api/booking/v4/pl-pl/availability) отдал
409 Conflict и на one-way, и на round-trip, и с market/currency — лестницу
Basic/Regular/Plus через него не получить.
