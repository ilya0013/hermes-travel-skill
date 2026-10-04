"""Города с несколькими аэропортами: код города IATA → аэропорты, главный — первым.

Эвал 02.10.2026: туда Wizz в Фьюмичино, обратно Ryanair из Чампино — `report.py --auto` не сложил пару (457 zł на двоих
против ★ 477). Список — направления из
Варшавы, где аэропорты одного города продаются раздельно; аэропорт в 100 км от города (Жирона, Хан) — не тот город.
"""

CITIES = {
    "LON": ("LHR", "LGW", "STN", "LTN", "SEN", "LCY"), "PAR": ("CDG", "ORY", "BVA"), "ROM": ("FCO", "CIA"),
    "MIL": ("MXP", "LIN", "BGY"), "STO": ("ARN", "NYO", "BMA"), "OSL": ("OSL", "TRF", "RYG"), "BRU": ("BRU", "CRL"),
    "VCE": ("VCE", "TSF"), "IST": ("IST", "SAW"), "TCI": ("TFS", "TFN"), "DXB": ("DXB", "DWC"), "TYO": ("NRT", "HND"),
    "OSA": ("KIX", "ITM"), "SEL": ("ICN", "GMP"), "BKK": ("BKK", "DMK"), "BJS": ("PEK", "PKX"), "SHA": ("PVG", "SHA"),
    "TPE": ("TPE", "TSA"), "NYC": ("JFK", "EWR", "LGA"), "WAW": ("WAW", "WMI"),
}
CITY_OF = {ap: city for city, aps in CITIES.items() for ap in aps}


def same_city(a, b):
    """Один город: тот же код или аэропорты одного города из `CITIES`."""
    return a == b or (a in CITY_OF and CITY_OF.get(a) == CITY_OF.get(b))
