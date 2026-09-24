"""
Garmin Forerunner 255 -> Google Sheets sync

Sheets:
- Activities
- Daily

Required environment variables:
GARMIN_EMAIL
GARMIN_PASSWORD
GOOGLE_CREDENTIALS
SHEET_ID

Optional:
SYNC_START_DATE  (optional; empty means the whole history)
GARMIN_TOKEN_DIR
SYNC_TIMEZONE
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional, Sequence, TypeVar
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import gspread
from garminconnect import Garmin
from google.oauth2.service_account import Credentials


logger = logging.getLogger("garmin_sync")

T = TypeVar("T")

SYNC_START_DATE = os.getenv("SYNC_START_DATE", "").strip()
ACTIVITY_PAGE_SIZE = 100
SYNC_TIMEZONE = os.getenv("SYNC_TIMEZONE", "Asia/Novosibirsk")
TOKEN_DIR = os.path.expanduser(os.getenv("GARMIN_TOKEN_DIR", "~/.garth"))

DATE_PARSE_FORMATS = (
    "%Y-%m-%d",
    "%d.%m.%Y",
    "%d.%m.%y",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%Y/%m/%d",
    "%d-%m-%Y",
)
DATETIME_PARSE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
)

RATE_LIMIT_BASE_SECONDS = 15
RATE_LIMIT_MAX_SECONDS = 180
RATE_LIMIT_RETRIES = 5

ACTIVITY_HEADERS = [
    "Тип активности",
    "Дата",
    "Избранное",
    "Название",
    "Дистанция (км)",
    "Калории",
    "Время (мин)",
    "Ср. ЧСС",
    "Макс. ЧСС",
    "Аэробный TE",
    "Ср. каденс",
    "Макс. каденс",
    "Ср. темп (мин/км)",
    "Лучший темп (мин/км)",
    "Набор высоты (м)",
    "Снижение (м)",
    "Ср. длина шага (см)",
    "Ср. верт. соотношение (%)",
    "Ср. верт. осцилляция (см)",
    "Training Load",
    "ID активности",
    "Локация",
    "Время в движении (мин)",
    "Затраченное время (мин)",
    "Анаэробный TE",
    "Метка TE",
    "Время контакта с землей (мс)",
    "Шаги",
    "Ср. мощность (Вт)",
    "Макс. мощность (Вт)",
    "Норм. мощность (Вт)",
    "Body Battery Δ",
    "Умеренные мин",
    "Интенсивные мин",
    "Кругов",
    "Мин. высота (м)",
    "Макс. высота (м)",
    "Устройство",
    "ЧСС зона 1 (мин)",
    "ЧСС зона 2 (мин)",
    "ЧСС зона 3 (мин)",
    "ЧСС зона 4 (мин)",
    "ЧСС зона 5 (мин)",
]

DAILY_HEADERS = [
    "Дата",
    "Шаги",
    "Этажи",
    "Стресс",
    "Body Battery Макс",
    "Body Battery Мин",
    "HRV Ср.",
    "HRV Статус",
    "Дыхание",
    "SpO2",
    "Сон Всего (мин)",
    "Оценка сна",
    "ЧСС покоя",
    "VO2 Max Бег",
    "VO2 Max Вело",
]


class SyncConfigError(RuntimeError):
    """Обязательные настройки синхронизации не заданы или не читаются."""


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def local_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(SYNC_TIMEZONE)
    except Exception as exc:
        logger.warning("Unknown SYNC_TIMEZONE=%s, using Asia/Novosibirsk (%s)", SYNC_TIMEZONE, exc)
        return ZoneInfo("Asia/Novosibirsk")


def _status_code(exc: BaseException) -> Optional[int]:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _is_rate_limited(exc: BaseException) -> bool:
    if _status_code(exc) == 429:
        return True
    text = str(exc).lower()
    return "429" in text or "too many requests" in text


def _retry_delay_seconds(exc: BaseException, attempt: int) -> float:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    retry_after = headers.get("Retry-After") if isinstance(headers, Mapping) else None
    if retry_after:
        try:
            return min(float(retry_after), RATE_LIMIT_MAX_SECONDS)
        except (TypeError, ValueError):
            pass
    delay = min(RATE_LIMIT_BASE_SECONDS * (2 ** attempt), RATE_LIMIT_MAX_SECONDS)
    return delay + random.uniform(0, delay * 0.1)


def safe_call(func: Callable[..., T], *args: Any, default: Optional[T] = None, retries: int = RATE_LIMIT_RETRIES) -> Optional[T]:
    """Вызов API. На HTTP 429 ждёт с экспоненциальной паузой, остальные ошибки заменяет на default."""
    name = getattr(func, "__name__", "unknown")
    for attempt in range(retries):
        try:
            result = func(*args)
            return default if result is None else result
        except Exception as exc:
            if _is_rate_limited(exc) and attempt < retries - 1:
                wait_seconds = _retry_delay_seconds(exc, attempt)
                logger.warning(
                    "HTTP 429 from %s, retry %s/%s in %.1fs",
                    name,
                    attempt + 1,
                    retries - 1,
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                continue
            logger.warning("API error %s: %s", name, exc)
            return default
    return default


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def dig(data: Any, *path: str, default: Any = "") -> Any:
    """Безопасно достаёт вложенное значение. Пустое или отсутствующее заменяется на default."""
    current = data
    for key in path:
        if not isinstance(current, Mapping):
            return default
        current = current.get(key)
    if current is None or current == "":
        return default
    return current


def get_value(data: Any, key: str, default: Any = "") -> Any:
    if not isinstance(data, dict):
        return default
    value = data.get(key)
    if value is None:
        return default
    return value


def pick_value(*values: Any, default: Any = "") -> Any:
    for value in values:
        if value is None or value == "":
            continue
        return value
    return default


def to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def strip_date_artifacts(value: Any) -> str:
    """Убирает кавычки, апостроф Sheets, обратные слэши и пробелы вокруг даты."""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").replace("\ufeff", "").strip()
    text = text.replace("\\'", "'").replace('\\"', '"').replace("\\", "")
    changed = True
    while changed and text:
        changed = False
        stripped = text.strip().strip("'\"`").strip()
        if stripped != text:
            text = stripped
            changed = True
    return text


def _split_date_and_time(text: str) -> tuple[str, str]:
    cleaned = text.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1].strip()
    for separator in ("T", " "):
        if separator in cleaned:
            left, right = cleaned.split(separator, 1)
            right = right.split("+", 1)[0].split("-", 1)[0] if "T" in cleaned or separator == " " else right
            # Offset like +07:00 is already cut. A date must not lose its own dashes.
            if separator == "T":
                offset_at = right.find("+")
                if offset_at >= 0:
                    right = right[:offset_at]
                zulu = right.find("Z")
                if zulu >= 0:
                    right = right[:zulu]
            return left.strip(), right.strip()
    return cleaned, ""


def parse_local_datetime(value: Any, assume_utc: bool = False) -> Optional[datetime]:
    """Разбирает метку и возвращает её в локальной таймзоне без микросекунд."""
    text = strip_date_artifacts(value)
    if not text:
        return None
    date_part, time_part = _split_date_and_time(text)
    if time_part:
        time_part = time_part.split(".", 1)[0]
        candidate = f"{date_part}T{time_part}"
        for fmt in DATETIME_PARSE_FORMATS:
            try:
                parsed = datetime.strptime(candidate.replace(" ", "T") if "T" in fmt else f"{date_part} {time_part}", fmt)
                break
            except ValueError:
                parsed = None
        else:
            parsed = None
        if parsed is not None:
            if assume_utc:
                parsed = parsed.replace(tzinfo=timezone.utc).astimezone(local_timezone())
            else:
                parsed = parsed.replace(tzinfo=local_timezone())
            return parsed.replace(microsecond=0, tzinfo=None)
    iso_date = normalize_sheet_date(date_part)
    if not iso_date:
        return None
    parsed_date = datetime.strptime(iso_date, "%Y-%m-%d")
    return parsed_date.replace(tzinfo=None)


def normalize_sheet_date(value: Any) -> str:
    """Приводит дату к YYYY-MM-DD. Неразобранное значение не возвращается как есть."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = strip_date_artifacts(value)
    if not text:
        return ""
    date_part, _time_part = _split_date_and_time(text)
    text = date_part or text
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date().isoformat()
        except ValueError:
            return ""
    for fmt in DATE_PARSE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def to_iso_timestamp(value: Any, assume_utc: bool = False) -> str:
    parsed = parse_local_datetime(value, assume_utc=assume_utc)
    if parsed is None:
        return ""
    return parsed.strftime("%Y-%m-%dT%H:%M:%S")


def entry_date(entry: Any) -> str:
    return normalize_sheet_date(
        pick_value(
            get_value(entry, "calendarDate"),
            get_value(entry, "date"),
            default="",
        )
    )


def get_local_today() -> date:
    return datetime.now(local_timezone()).date()


def get_sync_start_date() -> Optional[date]:
    """Нижняя граница, если она задана. Пустое значение значит всю историю."""
    if not SYNC_START_DATE:
        return None
    normalized = normalize_sheet_date(SYNC_START_DATE)
    if not normalized:
        logger.warning("Invalid SYNC_START_DATE=%s, loading the whole history", SYNC_START_DATE)
        return None
    return datetime.strptime(normalized, "%Y-%m-%d").date()


def token_files_ready(token_dir: str) -> bool:
    return all(
        os.path.isfile(os.path.join(token_dir, name))
        for name in ("oauth1_token.json", "oauth2_token.json")
    )


def connect_garmin(email: str, password: str) -> Garmin:
    """Сначала поднимает сессию из токенов Garth. Полный логин — только если токенов нет или они отклонены."""
    os.makedirs(TOKEN_DIR, exist_ok=True)
    garmin = Garmin(email, password)
    if token_files_ready(TOKEN_DIR):
        try:
            garmin.login(TOKEN_DIR)
            logger.info("Garmin session restored from tokenstore %s", TOKEN_DIR)
            return garmin
        except Exception as exc:
            logger.warning(
                "Stored Garmin tokens were rejected (%s). Password login will refresh the tokenstore.",
                type(exc).__name__,
            )
    garmin.garth.login(email, password)
    garmin.display_name = garmin.garth.profile["displayName"]
    garmin.full_name = garmin.garth.profile["fullName"]
    garmin.garth.dump(TOKEN_DIR)
    logger.info("Garmin password login succeeded, tokens saved to %s", TOKEN_DIR)
    return garmin


def seconds_to_minutes(value: Any) -> Any:
    number = to_float(value)
    if number is None:
        return ""
    return round(number / 60, 1)


def speed_to_pace_min_per_km(speed_mps: Any) -> Any:
    speed = to_float(speed_mps)
    if speed is None or speed <= 0:
        return ""
    return round(1000 / (speed * 60), 2)


def round_value(value: Any, digits: int = 2) -> Any:
    number = to_float(value)
    if number is None:
        return "" if value in (None, "") else value
    return round(number, digits)


def get_daily_row_index(sheet: gspread.Worksheet) -> dict[str, int]:
    rows = sheet.get_all_values()
    date_to_row: dict[str, int] = {}
    for row_number, row in enumerate(rows[1:], start=2):
        if not row or not row[0]:
            continue
        normalized = normalize_sheet_date(row[0])
        if normalized:
            date_to_row[normalized] = row_number
    return date_to_row


def upsert_daily_rows(sheet: gspread.Worksheet, rows_by_date: Mapping[str, Sequence[Any]]) -> None:
    date_to_row = get_daily_row_index(sheet)
    rows_to_append = []
    for date_str in sorted(rows_by_date):
        row = list(rows_by_date[date_str])
        if date_str in date_to_row:
            row_number = date_to_row[date_str]
            sheet.update(range_name=f"A{row_number}", values=[row], value_input_option="RAW")
            logger.info("Updated Daily row for %s (row %s)", date_str, row_number)
        else:
            rows_to_append.append(row)
    if rows_to_append:
        sheet.append_rows(rows_to_append, value_input_option="USER_ENTERED")
        logger.info("Added %s new Daily rows", len(rows_to_append))
    last_row = len(sheet.col_values(1))
    if last_row > 2:
        sheet.sort((1, "asc"), range=f"A2:AZ{last_row}")
        logger.info("Daily rows sorted by date, %s data rows", last_row - 1)


def fetch_all_activities(garmin: Garmin) -> list[dict[str, Any]]:
    """Все тренировки аккаунта, без окна дат. Страницы по 100, пока Garmin не отдаст короткую страницу."""
    activities: list[dict[str, Any]] = []
    start = 0
    while True:
        page = safe_call(garmin.get_activities, start, ACTIVITY_PAGE_SIZE, default=[]) or []
        if not isinstance(page, list) or not page:
            break
        activities.extend(item for item in page if isinstance(item, dict))
        logger.info("Fetched activities %s..%s", start, start + len(page) - 1)
        if len(page) < ACTIVITY_PAGE_SIZE:
            break
        start += ACTIVITY_PAGE_SIZE
    return activities


def _dates_from_steps(payload: Any) -> set[str]:
    dates: set[str] = set()
    if not isinstance(payload, list):
        return dates
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        day = normalize_sheet_date(
            pick_value(entry.get("calendarDate"), entry.get("date"), default="")
        )
        if day:
            dates.add(day)
    return dates


def discover_daily_dates(garmin: Garmin, activity_dates: set[str]) -> list[str]:
    """Каждый календарный день от первой даты с данными до сегодня, без пропусков."""
    today = get_local_today()
    configured = get_sync_start_date()
    if configured:
        start = configured
    else:
        seeds = []
        for day in activity_dates:
            parsed = normalize_sheet_date(day)
            if parsed:
                seeds.append(datetime.strptime(parsed, "%Y-%m-%d").date())
        start = min(seeds) if seeds else today
        probe_end = start
        empty_windows = 0
        while empty_windows < 3 and probe_end > date(2012, 1, 1):
            probe_start = probe_end - timedelta(days=7)
            payload = safe_call(
                garmin.get_daily_steps,
                probe_start.isoformat(),
                probe_end.isoformat(),
                default=[],
            )
            found_days = []
            for day in _dates_from_steps(payload):
                parsed = datetime.strptime(day, "%Y-%m-%d").date()
                if parsed < probe_end:
                    found_days.append(parsed)
            if found_days:
                start = min(start, min(found_days))
                empty_windows = 0
            else:
                empty_windows += 1
            probe_end = probe_start
    if start > today:
        return []
    days: list[str] = []
    current = start
    while current <= today:
        days.append(current.isoformat())
        current += timedelta(days=1)
    logger.info("Daily calendar: %s .. %s (%s days)", days[0], days[-1], len(days))
    return days


def get_daily_dates_to_sync(existing_dates: set[str], available_dates: Sequence[str]) -> tuple[list[str], str]:
    """Все ещё не записанные дни плюс сегодня и вчера: их Garmin ещё может дополнить."""
    today = get_local_today()
    refresh_date = today.isoformat()
    dates_to_sync = {day for day in available_dates if day not in existing_dates}
    dates_to_sync.add(refresh_date)
    dates_to_sync.add((today - timedelta(days=1)).isoformat())
    return sorted(day for day in dates_to_sync if day <= refresh_date), refresh_date


def get_device_map(garmin: Garmin) -> dict[str, str]:
    devices = safe_call(garmin.get_devices, default=[]) or []
    device_map: dict[str, str] = {}
    if not isinstance(devices, list):
        return device_map
    for device in devices:
        device_id = str(get_value(device, "deviceId"))
        device_map[device_id] = str(
            pick_value(
                get_value(device, "displayName"),
                get_value(device, "productDisplayName"),
                default="",
            )
        )
    return device_map


def ensure_sheet_headers(sheet: gspread.Worksheet, expected_headers: Sequence[str]) -> None:
    """Дописывает недостающие заголовки в конец. Существующий порядок колонок не перезаписывает."""
    current_headers = sheet.row_values(1)
    if not current_headers:
        sheet.update(range_name="A1", values=[list(expected_headers)], value_input_option="RAW")
        logger.info("Created headers on %s", sheet.title)
        return
    missing = [header for header in expected_headers if header not in current_headers]
    if missing:
        sheet.update(
            range_name="A1",
            values=[current_headers + missing],
            value_input_option="RAW",
        )
        logger.info("Added missing headers on %s: %s", sheet.title, ", ".join(missing))
    elif list(current_headers[: len(expected_headers)]) != list(expected_headers):
        logger.warning(
            "Header order on %s differs from the script. Rows are still written in the script order.",
            sheet.title,
        )


def get_existing_activity_keys(sheet: gspread.Worksheet, activity_id_column: int) -> tuple[set[str], set[tuple[str, str]]]:
    rows = sheet.get_all_values()
    existing_ids: set[str] = set()
    existing_name_keys: set[tuple[str, str]] = set()
    name_column = ACTIVITY_HEADERS.index("Название")
    date_column = ACTIVITY_HEADERS.index("Дата")
    for row in rows[1:]:
        if len(row) <= date_column or not row[date_column]:
            continue
        activity_date = normalize_sheet_date(row[date_column])
        if not activity_date:
            continue
        if len(row) > activity_id_column and row[activity_id_column]:
            existing_ids.add(str(row[activity_id_column]).split(".")[0].strip().strip("'\""))
        elif len(row) > name_column and row[name_column]:
            existing_name_keys.add((activity_date, row[name_column]))
    return existing_ids, existing_name_keys


def activity_type_key(activity: Mapping[str, Any]) -> str:
    nested = activity.get("activityType")
    if isinstance(nested, Mapping):
        return str(pick_value(nested.get("typeKey"), nested.get("typeId"), default=""))
    return str(pick_value(activity.get("activityType"), default=""))


def activity_local_date(activity: Mapping[str, Any]) -> str:
    local_raw = get_value(activity, "startTimeLocal")
    if local_raw:
        return normalize_sheet_date(local_raw) or (
            parse_local_datetime(local_raw).date().isoformat() if parse_local_datetime(local_raw) else ""
        )
    gmt_raw = get_value(activity, "startTimeGMT")
    parsed = parse_local_datetime(gmt_raw, assume_utc=True) if gmt_raw else None
    return parsed.date().isoformat() if parsed else ""


def is_activity_existing(
    activity: Mapping[str, Any],
    existing_ids: set[str],
    existing_name_keys: set[tuple[str, str]],
) -> bool:
    activity_id = str(pick_value(activity.get("activityId"), default="")).split(".")[0]
    activity_date = activity_local_date(activity)
    activity_name = str(get_value(activity, "activityName"))
    if activity_id and activity_id in existing_ids:
        return True
    return bool(activity_date) and (activity_date, activity_name) in existing_name_keys


def hr_zone_minutes(activity: Mapping[str, Any]) -> list[Any]:
    """Минуты в зонах 1–5 из вложенного списка, если он есть в ответе активности."""
    zones: list[Any] = [""] * 5
    raw_zones: Any = None
    for key in ("hrTimeInZone", "heartRateZones", "heartRateZoneDTOs"):
        candidate = activity.get(key)
        if isinstance(candidate, list):
            raw_zones = candidate
            break
    if raw_zones is None:
        raw_zones = dig(activity, "hrTimeInZoneDTO", "hrTimeInZone", default=None)
        if not isinstance(raw_zones, list):
            return zones
    for item in raw_zones:
        number: Any
        seconds: Any
        if isinstance(item, Mapping):
            number = pick_value(item.get("zoneNumber"), item.get("zone"), default=None)
            seconds = pick_value(
                item.get("secsInZone"),
                item.get("seconds"),
                item.get("timeInZone"),
                default=None,
            )
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            number, seconds = item[0], item[1]
        else:
            continue
        try:
            index = int(number) - 1
        except (TypeError, ValueError):
            continue
        seconds_value = to_float(seconds)
        if 0 <= index < 5 and seconds_value is not None:
            zones[index] = round(seconds_value / 60, 1)
    return zones


def build_activity_row(activity: Mapping[str, Any], device_map: Mapping[str, str]) -> list[Any]:
    """Одна строка Activities. Дата — локальный календарный день YYYY-MM-DD."""
    activity_date = activity_local_date(activity)
    distance = to_float(get_value(activity, "distance", 0), 0.0) or 0.0
    duration = to_float(get_value(activity, "duration", 0), 0.0) or 0.0
    moving_duration = to_float(get_value(activity, "movingDuration", 0), 0.0) or 0.0
    elapsed_duration = to_float(get_value(activity, "elapsedDuration", 0), 0.0) or 0.0

    avg_pace = speed_to_pace_min_per_km(get_value(activity, "averageSpeed", 0))
    if not avg_pace and distance:
        avg_pace = round_value((duration / (distance / 1000)) / 60)

    best_pace = speed_to_pace_min_per_km(get_value(activity, "maxSpeed", 0))
    avg_cadence = pick_value(
        get_value(activity, "averageRunningCadenceInStepsPerMinute"),
        get_value(activity, "averageBikingCadenceInRevPerMinute"),
        get_value(activity, "averageCadence"),
        default="",
    )
    max_cadence = pick_value(
        get_value(activity, "maxRunningCadenceInStepsPerMinute"),
        get_value(activity, "maxBikingCadenceInRevPerMinute"),
        get_value(activity, "maxCadence"),
        default="",
    )
    device_id = str(get_value(activity, "deviceId"))
    return [
        activity_type_key(activity),
        activity_date,
        "Да" if activity.get("favorite") else "Нет",
        get_value(activity, "activityName"),
        round_value(distance / 1000) if distance else 0,
        round_value(get_value(activity, "calories", 0), 0),
        round_value(duration / 60),
        round_value(get_value(activity, "averageHR", 0), 0),
        round_value(get_value(activity, "maxHR", 0), 0),
        round_value(get_value(activity, "aerobicTrainingEffect")),
        round_value(avg_cadence, 1),
        round_value(max_cadence, 0),
        avg_pace,
        best_pace,
        round_value(get_value(activity, "elevationGain", 0), 1),
        round_value(get_value(activity, "elevationLoss", 0), 1),
        round_value(get_value(activity, "avgStrideLength", 0), 1),
        round_value(get_value(activity, "avgVerticalRatio", 0), 1),
        round_value(get_value(activity, "avgVerticalOscillation", 0), 1),
        round_value(get_value(activity, "activityTrainingLoad", 0), 1),
        str(get_value(activity, "activityId")),
        get_value(activity, "locationName"),
        round_value(moving_duration / 60) if moving_duration else "",
        round_value(elapsed_duration / 60) if elapsed_duration else "",
        round_value(get_value(activity, "anaerobicTrainingEffect")),
        get_value(activity, "trainingEffectLabel"),
        round_value(get_value(activity, "avgGroundContactTime", 0), 1),
        round_value(get_value(activity, "steps", 0), 0),
        round_value(get_value(activity, "avgPower", 0), 0),
        round_value(get_value(activity, "maxPower", 0), 0),
        round_value(get_value(activity, "normPower", 0), 0),
        round_value(get_value(activity, "differenceBodyBattery", 0), 0),
        round_value(get_value(activity, "moderateIntensityMinutes", 0), 0),
        round_value(get_value(activity, "vigorousIntensityMinutes", 0), 0),
        round_value(get_value(activity, "lapCount", 0), 0),
        round_value(get_value(activity, "minElevation", 0), 0),
        round_value(get_value(activity, "maxElevation", 0), 0),
        device_map.get(device_id, ""),
        *hr_zone_minutes(activity),
    ]


def body_battery_point_level(point: Any) -> Any:
    if isinstance(point, (list, tuple)) and len(point) >= 2:
        return point[1]
    if isinstance(point, dict):
        return pick_value(point.get("bodyBatteryLevel"), point.get("value"), default=None)
    return None


def parse_body_battery(summary: Mapping[str, Any], battery_payload: Any, date_str: str) -> tuple[Any, Any]:
    battery_max = pick_value(get_value(summary, "bodyBatteryHighestValue"), default="")
    battery_min = pick_value(get_value(summary, "bodyBatteryLowestValue"), default="")
    if battery_max != "" and battery_min != "":
        return battery_max, battery_min

    values: list[float] = []
    entries = battery_payload if isinstance(battery_payload, list) else []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry_date(entry) not in ("", date_str):
            continue
        highest = pick_value(
            entry.get("highestBodyBatteryValue"),
            entry.get("bodyBatteryHighestValue"),
            default="",
        )
        lowest = pick_value(
            entry.get("lowestBodyBatteryValue"),
            entry.get("bodyBatteryLowestValue"),
            default="",
        )
        if highest != "":
            battery_max = highest
        if lowest != "":
            battery_min = lowest
        for point in entry.get("bodyBatteryValuesArray") or []:
            level = to_float(body_battery_point_level(point))
            if level is not None:
                values.append(level)
    if values:
        parsed_max, parsed_min = max(values), min(values)
        battery_max = pick_value(battery_max, parsed_max, default="")
        battery_min = pick_value(battery_min, parsed_min, default="")
    return battery_max, battery_min


def vo2_from_training_status(training_status: Any) -> tuple[Any, Any]:
    most_recent = as_dict(as_dict(training_status).get("mostRecentVO2Max"))
    vo2_running = pick_value(as_dict(most_recent.get("generic")).get("vo2MaxValue"), default="")
    vo2_cycling = pick_value(as_dict(most_recent.get("cycling")).get("vo2MaxValue"), default="")
    return vo2_running, vo2_cycling


def _take_vo2_entry(entry: Any, date_str: str, current: tuple[Any, Any]) -> tuple[Any, Any]:
    vo2_running, vo2_cycling = current
    if not isinstance(entry, dict):
        return vo2_running, vo2_cycling
    entry_day = entry_date(entry)
    if entry_day not in ("", date_str):
        return vo2_running, vo2_cycling
    vo2_running = pick_value(entry.get("vo2MaxValue"), entry.get("vo2MaxPreciseValue"), vo2_running, default="")
    vo2_cycling = pick_value(entry.get("vo2MaxCyclingValue"), vo2_cycling, default="")
    return vo2_running, vo2_cycling


def vo2_from_max_metrics(max_metrics: Any, date_str: str) -> tuple[Any, Any]:
    vo2_running, vo2_cycling = "", ""
    if isinstance(max_metrics, list):
        for entry in max_metrics:
            vo2_running, vo2_cycling = _take_vo2_entry(entry, date_str, (vo2_running, vo2_cycling))
        return vo2_running, vo2_cycling

    metrics = as_dict(max_metrics)
    for key in ("maxMetricValues", "metrics", "metricDTOs"):
        items = metrics.get(key)
        if isinstance(items, list):
            for entry in items:
                vo2_running, vo2_cycling = _take_vo2_entry(entry, date_str, (vo2_running, vo2_cycling))

    metrics_map = metrics.get("metricsMap")
    if isinstance(metrics_map, list):
        for entry in metrics_map:
            metric_type = str(pick_value(get_value(entry, "metricsType"), get_value(entry, "metricType"), default=""))
            if metric_type in ("VO2_MAX", "vo2max", "generic"):
                vo2_running = pick_value(entry.get("vo2MaxValue"), vo2_running, default="")
            if metric_type in ("CYCLING_VO2_MAX", "cycling_vo2max", "cycling"):
                vo2_cycling = pick_value(entry.get("vo2MaxValue"), vo2_cycling, default="")
    elif isinstance(metrics_map, dict):
        for metric_type, entries in metrics_map.items():
            items = entries if isinstance(entries, list) else [entries]
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                if metric_type in ("VO2_MAX", "vo2max", "generic"):
                    vo2_running = pick_value(entry.get("vo2MaxValue"), vo2_running, default="")
                if metric_type in ("CYCLING_VO2_MAX", "cycling_vo2max", "cycling"):
                    vo2_cycling = pick_value(entry.get("vo2MaxValue"), vo2_cycling, default="")

    vo2_running = pick_value(
        metrics.get("vo2MaxValue"),
        as_dict(metrics.get("generic")).get("vo2MaxValue"),
        vo2_running,
        default="",
    )
    vo2_cycling = pick_value(
        metrics.get("vo2MaxCyclingValue"),
        as_dict(metrics.get("cycling")).get("vo2MaxValue"),
        vo2_cycling,
        default="",
    )
    return vo2_running, vo2_cycling


def fetch_body_battery_by_date(garmin: Garmin, start_date: str, end_date: str) -> dict[str, list[Any]]:
    by_date: dict[str, list[Any]] = {}
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    current = start
    while current <= end:
        chunk_end = min(end, current + timedelta(days=6))
        payload = safe_call(
            garmin.get_body_battery,
            current.isoformat(),
            chunk_end.isoformat(),
            default=[],
        ) or []
        if isinstance(payload, list):
            for entry in payload:
                date_key = entry_date(entry)
                if date_key:
                    by_date.setdefault(date_key, []).append(entry)
        current = chunk_end + timedelta(days=1)
    return by_date


def fetch_max_metrics(garmin: Garmin, start_date: str, end_date: str) -> Any:
    if hasattr(garmin, "get_max_metrics_range"):
        return safe_call(garmin.get_max_metrics_range, start_date, end_date, default={}) or {}
    return safe_call(garmin.get_max_metrics, end_date, default={}) or {}


def build_daily_row(
    garmin: Garmin,
    date_str: str,
    battery_payload: Optional[list[Any]] = None,
    max_metrics: Any = None,
    fetch_training_status: bool = False,
) -> list[Any]:
    summary = as_dict(safe_call(garmin.get_user_summary, date_str, default={}))
    steps = pick_value(get_value(summary, "totalSteps"), default="")
    floors = pick_value(get_value(summary, "floorsAscended"), default="")
    resting_hr = pick_value(get_value(summary, "restingHeartRate"), default="")

    stress_level = pick_value(get_value(summary, "averageStressLevel"), default="")
    if stress_level == "":
        stress_data = as_dict(safe_call(garmin.get_all_day_stress, date_str, default={}))
        if not stress_data:
            stress_data = as_dict(safe_call(garmin.get_stress_data, date_str, default={}))
        stress_level = pick_value(
            stress_data.get("avgStressLevel"),
            stress_data.get("averageStressLevel"),
            stress_data.get("stressLevel"),
            default="",
        )

    if battery_payload is None:
        battery_max = pick_value(get_value(summary, "bodyBatteryHighestValue"), default="")
        battery_min = pick_value(get_value(summary, "bodyBatteryLowestValue"), default="")
        if battery_max == "" or battery_min == "":
            battery_payload = safe_call(garmin.get_body_battery, date_str, date_str, default=[]) or []
        else:
            battery_payload = []
    battery_max, battery_min = parse_body_battery(summary, battery_payload, date_str)

    hrv = as_dict(safe_call(garmin.get_hrv_data, date_str, default={}))
    hrv_summary = as_dict(hrv.get("hrvSummary"))
    hrv_avg = pick_value(hrv_summary.get("lastNightAvg"), default="")
    hrv_status = pick_value(hrv_summary.get("status"), default="")

    respiration_value = pick_value(
        get_value(summary, "averageWakingRespirationValue"),
        get_value(summary, "averageSleepRespirationValue"),
        default="",
    )
    if respiration_value == "":
        respiration = as_dict(safe_call(garmin.get_respiration_data, date_str, default={}))
        respiration_value = pick_value(
            respiration.get("avgWakingRespirationValue"),
            respiration.get("avgSleepRespirationValue"),
            respiration.get("avgWakingRespiration"),
            default="",
        )

    spo2_value = pick_value(get_value(summary, "averageSpo2"), default="")
    if spo2_value == "":
        spo2 = as_dict(safe_call(garmin.get_spo2_data, date_str, default={}))
        spo2_value = pick_value(spo2.get("averageSpO2"), spo2.get("avgSleepSpO2"), spo2.get("lowestSpO2"), default="")

    sleep = as_dict(safe_call(garmin.get_sleep_data, date_str, default={}))
    sleep_dto = as_dict(sleep.get("dailySleepDTO"))
    sleep_time = pick_value(get_value(sleep_dto, "sleepTimeSeconds"), default="")
    overall_score = as_dict(sleep_dto.get("sleepScores")).get("overall")
    sleep_score = pick_value(overall_score.get("value") if isinstance(overall_score, dict) else None, default="")

    vo2_running, vo2_cycling = vo2_from_max_metrics(max_metrics, date_str)
    if fetch_training_status:
        training_status = as_dict(safe_call(garmin.get_training_status, date_str, default={}))
        ts_running, ts_cycling = vo2_from_training_status(training_status)
        vo2_running = pick_value(ts_running, vo2_running, default="")
        vo2_cycling = pick_value(ts_cycling, vo2_cycling, default="")

    return [
        date_str,
        steps,
        floors,
        stress_level,
        battery_max,
        battery_min,
        hrv_avg,
        hrv_status,
        respiration_value,
        spo2_value,
        seconds_to_minutes(sleep_time) if sleep_time != "" else "",
        sleep_score,
        resting_hr,
        vo2_running,
        vo2_cycling,
    ]


def load_google_credentials(raw: str) -> dict[str, Any]:
    text = raw.strip().lstrip("\ufeff")
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SyncConfigError("GOOGLE_CREDENTIALS is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise SyncConfigError("GOOGLE_CREDENTIALS must be a service account JSON object")
    return payload


def require_env() -> tuple[str, str, str, str]:
    email = os.getenv("GARMIN_EMAIL", "").strip()
    password = os.getenv("GARMIN_PASSWORD", "")
    sheet_id = os.getenv("SHEET_ID", "").strip()
    credentials = os.getenv("GOOGLE_CREDENTIALS", "")
    missing = [
        name
        for name, value in (
            ("GARMIN_EMAIL", email),
            ("GARMIN_PASSWORD", password),
            ("SHEET_ID", sheet_id),
            ("GOOGLE_CREDENTIALS", credentials),
        )
        if not value
    ]
    if missing:
        raise SyncConfigError("Missing environment variables: " + ", ".join(missing))
    return email, password, sheet_id, credentials


def main() -> None:
    configure_logging()
    logger.info(
        "Garmin sync started (Activities + Daily, start=%s)",
        SYNC_START_DATE or "whole history",
    )
    email, password, sheet_id, credentials = require_env()
    garmin = connect_garmin(email, password)

    creds = Credentials.from_service_account_info(
        load_google_credentials(credentials),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(sheet_id)
    activities_sheet = spreadsheet.worksheet("Activities")
    daily_sheet = spreadsheet.worksheet("Daily")

    ensure_sheet_headers(activities_sheet, ACTIVITY_HEADERS)
    ensure_sheet_headers(daily_sheet, DAILY_HEADERS)

    activity_id_column = ACTIVITY_HEADERS.index("ID активности")
    existing_activity_ids, existing_activity_name_keys = get_existing_activity_keys(
        activities_sheet,
        activity_id_column,
    )
    existing_dates = set(get_daily_row_index(daily_sheet))

    today_local = get_local_today()
    start_date = get_sync_start_date()
    logger.info(
        "Sync window: %s .. %s (timezone %s, no open-day cutoff)",
        start_date.isoformat() if start_date else "beginning",
        today_local.isoformat(),
        SYNC_TIMEZONE,
    )

    device_map = get_device_map(garmin)
    activities = fetch_all_activities(garmin)
    activity_dates: set[str] = set()

    activity_rows = []
    for activity in activities:
        activity_date = activity_local_date(activity)
        if not activity_date:
            logger.info("Skip undated activity %s", get_value(activity, "activityId"))
            continue
        if start_date and activity_date < start_date.isoformat():
            continue
        activity_dates.add(activity_date)
        if is_activity_existing(activity, existing_activity_ids, existing_activity_name_keys):
            continue
        activity_rows.append(build_activity_row(activity, device_map))

    if activity_rows:
        activities_sheet.append_rows(activity_rows, value_input_option="RAW")

    available_daily_dates = discover_daily_dates(garmin, activity_dates)
    daily_dates, refresh_date = get_daily_dates_to_sync(existing_dates, available_daily_dates)
    daily_rows_by_date: dict[str, list[Any]] = {}
    battery_by_date: dict[str, list[Any]] = {}
    max_metrics: Any = {}
    if daily_dates:
        logger.info("Prefetching Body Battery and VO2 for %s .. %s", daily_dates[0], daily_dates[-1])
        battery_by_date = fetch_body_battery_by_date(garmin, daily_dates[0], daily_dates[-1])
        max_metrics = fetch_max_metrics(garmin, daily_dates[0], daily_dates[-1])

    for date_str in daily_dates:
        logger.info("Fetching Daily metrics for %s%s", date_str, " (refresh)" if date_str == refresh_date else "")
        daily_rows_by_date[date_str] = build_daily_row(
            garmin,
            date_str,
            battery_payload=battery_by_date.get(date_str, []),
            max_metrics=max_metrics,
            fetch_training_status=(date_str == refresh_date),
        )

    if daily_rows_by_date:
        upsert_daily_rows(daily_sheet, daily_rows_by_date)

    logger.info("Activities added: %s", len(activity_rows))
    logger.info("Daily days processed: %s", len(daily_rows_by_date))


if __name__ == "__main__":
    main()
