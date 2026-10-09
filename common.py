"""Общие мелочи для шагов конвейера: чтение и запись JSON, время, выход с ошибкой."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CRITERIA = ["complexity", "quality", "risk", "clarity"]
LEVELS = ["junior", "middle", "senior"]
CI_VALUES = {"success", "failure", "mixed", "unknown"}


class DataError(Exception):
    """Входной файл отсутствует или не того вида. Текст ошибки показывается человеку."""


def die(message: str, code: int = 2) -> None:
    print("ОШИБКА: " + message, file=sys.stderr)
    sys.exit(code)


def read_json(path, what: str = "файл"):
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise DataError(f"не найден {what}: {path}")
    except (OSError, ValueError) as e:
        raise DataError(f"не удалось прочитать {what} {path}: {e}")


def read_list(path, what: str) -> list:
    data = read_json(path, what)
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise DataError(f"{what} {path} должен быть JSON-списком записей")
    return data


def write_text(path, text: str) -> None:
    """Через временный файл и переименование: прерванный запуск не оставит полфайла."""
    path = Path(path)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_json(path, data) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def parse_time(stamp: str) -> datetime:
    """'2026-05-14T10:22:00Z' -> datetime в UTC."""
    moment = datetime.fromisoformat(str(stamp).strip().replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def days_between(earlier: str, later: str) -> float:
    return (parse_time(later) - parse_time(earlier)).total_seconds() / 86400


def data_dir() -> Path:
    """Папка с данными по умолчанию: текущая, а если prs.json в ней нет, то подпапка data/.

    В репозитории проекта данные лежат в data/, а скрипты в корне: команда без параметров,
    запущенная из корня, должна сама найти свои файлы. В папке, где prs.json лежит рядом
    (образец sample/, результат репетиции), ничего не меняется."""
    if not Path("prs.json").is_file() and Path("data").is_dir():
        return Path("data")
    return Path(".")


def use_data_dir(parser, *names) -> None:
    """Пути, оставленные по умолчанию, ведут в папку данных. Путь, заданный явно, не трогается."""
    folder = data_dir()
    if folder != Path("."):
        parser.set_defaults(**{name: str(folder / parser.get_default(name)) for name in names})


def setup_output() -> None:
    """Консоль Windows не всегда умеет кириллицу: заменяем непечатаемое вместо падения."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
