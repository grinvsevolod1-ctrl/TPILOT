# liquid_ru_locations.py
# Справочник населённых пунктов РФ + новые регионы (ДНР/ЛНР/Запорожская/Херсонская).
# Источник: NickyX3/russia_settlements (settlements.csv), обновление 2025 года.
# Ключи: нижний регистр; е->е; все пробелы схлопнуты; тире нормализовано к '-'.
# Значение: (Название, (Субъект/регион, 'России'))
# ВАЖНО: ключи без префиксов 'город/г./село/посёлок' — только как пишет человек.
#
# 2026-08-20 data-move refactor: сами данные (159765 записей) вынесены из
# этого модуля в data/ru_locations.json. Модуль стал тонким загрузчиком,
# который читает JSON при импорте и реконструирует RU_LOCATIONS в ТОЧНО ТАКОМ
# ЖЕ виде, каким раньше был dict-литерал: dict[str, tuple[str, tuple[str, str]]].
# Публичный контракт (`from liquid_ru_locations import RU_LOCATIONS`) не изменён,
# типы значений остаются кортежами — потребители (geo_lexicon.py, router.py)
# работают без изменений.

from __future__ import annotations

import json
import os
from typing import Dict, Tuple

__all__ = ["RU_LOCATIONS"]

_DATA_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "ru_locations.json"
)


def _load() -> Dict[str, Tuple[str, Tuple[str, str]]]:
    """Read data/ru_locations.json and rebuild RU_LOCATIONS with the original
    tuple value shape (name, (region, country)). Insertion order is preserved
    (json + dict both keep order). Raises loudly on a missing/corrupt file,
    matching the old behavior where a broken data module failed at import."""
    with open(_DATA_PATH, encoding="utf-8") as fh:
        payload = json.load(fh)
    raw = payload["data"] if isinstance(payload, dict) and "data" in payload else payload
    out: Dict[str, Tuple[str, Tuple[str, str]]] = {}
    for key, val in raw.items():
        # val is [name, [region, country]] -> (name, (region, country))
        name, region_country = val
        region, country = region_country
        out[key] = (name, (region, country))
    return out


RU_LOCATIONS: Dict[str, Tuple[str, Tuple[str, str]]] = _load()
