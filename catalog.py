"""Offline catalogs for AutoMoto: KATOTTG locations, makes and models."""
import json
from pathlib import Path
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

BASE = Path(__file__).parent / "data"
GEO = json.loads((BASE / "katottg_places.json").read_text(encoding="utf-8"))
CARS = json.loads((BASE / "car_models.json").read_text(encoding="utf-8"))
REGIONS = {name: code for code, name in GEO["regions"]}
PLACES = GEO["places"]

AGRO = {
    "John Deere": ["5055E", "5075E", "5090M", "6110M", "6155M", "6195M", "6215R", "6930", "7830", "S670", "S770", "S780"],
    "CLAAS": ["ARION 430", "ARION 650", "AXION 850", "AXION 950", "LEXION 560", "LEXION 770", "LEXION 780", "TUCANO 450", "DOMINATOR 108"],
    "Case IH": ["Puma 165", "Puma 185", "Magnum 340", "Maxxum 125", "Axial-Flow 2388", "Axial-Flow 7240"],
    "New Holland": ["T5.110", "T6.180", "T7.270", "T8.410", "CR8.90", "CX6.90", "TC5.90"],
    "Massey Ferguson": ["5711", "6713", "7718", "7726", "S 8.265", "7278"],
    "Fendt": ["Vario 516", "Vario 724", "Vario 828", "Vario 936"],
    "Deutz-Fahr": ["Agrotron 150", "Agrotron 7250", "Series 6", "Series 7"],
    "МТЗ (Belarus)": ["80", "82", "82.1", "892", "920", "952", "1025", "1221", "1523"],
    "ЮМЗ": ["6", "6АКЛ", "6АЛ", "8240", "8271"],
    "ХТЗ": ["Т-150", "Т-150К", "17021", "17221", "243К", "16131"],
    "Кіровець": ["К-700", "К-701", "К-744"],
    "JCB": ["3CX", "4CX", "531-70", "535-95", "Fastrac 4220"],
    "Manitou": ["MLT 735", "MLT 741", "MLT 840", "MT 625"],
    "Завод Ростсільмаш": ["Дон-1500", "Acros 530", "Vector 410"],
    "Палессе (Гомсільмаш)": ["GS12", "GS3219", "КЗС-1218"],
    "Богуслав": ["ОП-2000", "ОП-2500", "ОП-3000"],
}
OTHER = "➕ Інша марка / модель"


def keyboard(items, n=2):
    labels = list(items)
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=v) for v in labels[i:i+n]] for i in range(0, len(labels), n)]
        + [[KeyboardButton(text="❌ Скасувати")]],
        resize_keyboard=True,
    )


def makes(kind):
    return sorted(CARS if kind == "🚗 Авторинок" else AGRO, key=str.casefold)


def models(kind, make):
    return CARS.get(make, []) if kind == "🚗 Авторинок" else AGRO.get(make, [])


def matches(values, query, limit=12):
    query = query.strip().casefold()
    if not query:
        return []
    starts = [v for v in values if v.casefold().startswith(query)]
    contains = [v for v in values if query in v.casefold() and v not in starts]
    return (starts + contains)[:limit]


def settlements(region_code, query, limit=12):
    query = query.strip().casefold()
    if len(query) < 2:
        return []
    result = [v for v in PLACES if v[0] == region_code and v[1].casefold().startswith(query)]
    if len(result) < limit:
        result += [v for v in PLACES if v[0] == region_code and query in v[1].casefold() and v not in result]
    return result[:limit]
