"""Быстрый JSON (application-слой, рекомендация №2 из docs/PERFORMANCE.md).

Зачем: сервер парсит/сериализует JSON на каждом чихе — /events poll ходит
каждые 1.5-2с от КАЖДОГО клиента, JSONL-журнал дописывается на каждое
сообщение/дельту, ответы транспорта собираются из словарей. orjson (MIT)
делает то же самое в 5-10 раз быстрее stdlib, и это самая дешёвая
ускоряющая конфета сервера: менять форматы не нужно, точки вызова уже
собраны в один модуль.

Пакет ОПЦИОНАЛЬНЫЙ (как urllib3 в lib/client.py): без orjson всё работает
на stdlib json, просто медленнее. Установка: `pip install orjson`.

Совместимость форматов:
  - dumps отдаёт компактный UTF-8 без ASCII-эскейпов — то же, что
    json.dumps(..., ensure_ascii=False, separators=(",", ":")), т.е. байты
    на проводе и в history.jsonl совпадают байт-в-байт с прежними;
  - loads принимает и bytes, и str (JSONL-строки читаются без .decode).

Ограничения orjson (учтены вызывающими): сериализует только dict/list/
str/int/float/bool/None — все наши payload'ы ровно такие; NaN/Inf даёт
null вместо 'NaN' — в событиях чата чисел с плавающей точкой вне ts нет,
а ts — int(time.time()).
"""
from __future__ import annotations

import json

# Имя бэкенда наружу для тестов и диагностики (lib/util.py style: одна
# строчка правды о том, что реально работает на этой машине).
BACKEND = "stdlib"

try:  # pragma: no cover - зависит от окружения (есть/нет orjson)
    import orjson as _orjson

    BACKEND = "orjson"
except ImportError:  # без orjson — stdlib, поведение идентичное
    _orjson = None


def loads(data: bytes | str):
    """Разбирает JSON (bytes или str). Бросает ValueError при мусоре —
    json.JSONDecodeError и orjson.JSONDecodeError оба его наследуют,
    поэтому вызывающие ловят один и тот же класс исключений."""
    if _orjson is not None:
        return _orjson.loads(data)
    return json.loads(data)


def dumps(obj) -> bytes:
    """Сериализует в компактные UTF-8 байты (ensure_ascii=False-семантика).
    Основной путь для HTTP-ответов (Content-Length по len байтов)."""
    if _orjson is not None:
        return _orjson.dumps(obj)
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def dumps_str(obj) -> str:
    """То же, что dumps(), но строкой — для JSONL-строк журнала
    (f.write() хочет str, а не bytes)."""
    if _orjson is not None:
        return _orjson.dumps(obj).decode("utf-8")
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
