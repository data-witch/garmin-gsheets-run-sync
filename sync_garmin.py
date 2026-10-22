"""
Garmin Connect -> Google Sheets.

Пишет только листы Activities и Daily. Дашборд, Свод2026 и остальные
листы с формулами не трогает.

Секреты: GARMIN_EMAIL, GARMIN_PASSWORD, GOOGLE_CREDENTIALS, SHEET_ID.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import gspread
from garminconnect import Garmin
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


SYNC_TIMEZONE = os.getenv("SYNC_TIMEZONE", "Asia/Novosibirsk")
TOKEN_DIR = os.path.expanduser(os.getenv("GARMIN_TOKEN_DIR", "~/.garth"))
DAILY_REFRESH_DAYS = int(os.getenv("DAILY_REFRESH_DAYS", "3"))
ACTIVITY_REFRESH_DAYS = int(os.getenv("ACTIVITY_REFRESH_DAYS", "7"))
SHEETS_EPOCH = date(1899, 12, 30)

DATE_FORMATS = (
    "%Y-%m-%d",
    "%d.%m.%Y",
    "%d.%m.%y",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%Y/%m/%d",
    "%m/%d/%Y",
)

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

TEXT_HEADERS = {
    "Тип активности",
    "Дата",
    "Избранное",
    "Название",
    "ID активности",
    "Локация",
    "Метка TE",
    "Устройство",
    "HRV Статус",
}


def is_blank(value):
    return value is None or (isinstance(value, str) and value.strip() == "")


def first_filled(*values):
    for value in values:
        if not is_blank(value):
            return value
    return ""


def as_dict(value):
    return value if isinstance(value, dict) else {}


def num(value, digits=0):
    if is_blank(value):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if digits <= 0:
        return int(round(number))
    return round(number, digits)


def parse_decimal_text(value):
    """'5,080' and '44,37' are Russian decimals, not thousands."""
    if not isinstance(value, str):
        return None
    text = value.strip().replace("\xa0", "").replace(" ", "")
    if not text or text in {"—", "-", "–"}:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        return None
    if number.is_integer():
        return int(number)
    return round(number, 3)


def normalize_id(value):
    if is_blank(value):
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def normalize_sheet_date(value):
    """Return YYYY-MM-DD for a Sheets date, serial, or display string."""
    if is_blank(value) or isinstance(value, bool):
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        serial = float(value)
        if 20000 <= serial <= 80000:
            return (SHEETS_EPOCH + timedelta(days=int(round(serial)))).isoformat()
        return ""

    text = str(value).strip()
    if "T" in text:
        text = text.split("T", 1)[0]
    elif " " in text:
        text = text.split(" ", 1)[0]
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def date_to_serial(day):
    return (day - SHEETS_EPOCH).days


def local_today():
    return datetime.now(ZoneInfo(SYNC_TIMEZONE)).date()


def resolve_start_date(today):
    explicit = os.getenv("SYNC_START_DATE", "").strip()
    if explicit:
        iso = normalize_sheet_date(explicit)
        if iso:
            return date.fromisoformat(iso)
        logger.warning("Не понял SYNC_START_DATE=%s, беру 2026-01-01", explicit)
        return date(2026, 1, 1)

    days_raw = os.getenv("SYNC_DAYS", "").strip()
    if days_raw:
        try:
            days = int(days_raw)
        except ValueError:
            logger.warning("Не понял SYNC_DAYS=%s, беру 2026-01-01", days_raw)
            return date(2026, 1, 1)
        return today - timedelta(days=max(days, 1))
    return date(2026, 1, 1)


def normalize_sheet_id(value):
    text = str(value or "").strip()
    marker = "/spreadsheets/d/"
    if marker in text:
        text = text.split(marker, 1)[1].split("/", 1)[0]
    return text.split("?", 1)[0]


def daily_dates_to_sync(existing_dates, today, start_date, refresh_days):
    """Missing closed days, plus the last `refresh_days` days. Never today."""
    yesterday = today - timedelta(days=1)
    if start_date > yesterday:
        return []

    wanted = set()
    for offset in range(1, refresh_days + 1):
        day = today - timedelta(days=offset)
        if day >= start_date:
            wanted.add(day.isoformat())

    current = start_date
    while current <= yesterday:
        iso = current.isoformat()
        if iso not in existing_dates:
            wanted.add(iso)
        current += timedelta(days=1)
    return sorted(wanted)


def activity_in_refresh_window(activity_day, today, refresh_days):
    if not activity_day:
        return False
    day = date.fromisoformat(activity_day)
    return today - timedelta(days=refresh_days) <= day <= today


def column_letter(col_index):
    """1-based column index to a letter."""
    return "".join(ch for ch in rowcol_to_a1(1, col_index) if ch.isalpha())


def clean_headers(headers):
    trimmed = list(headers or [])
    while trimmed and is_blank(trimmed[-1]):
        trimmed.pop()
    return [str(header).strip() for header in trimmed]


def build_output_row(headers, values_by_name, old_row):
    """Blank incoming cells keep the value already stored in the sheet."""
    output = []
    for index, header in enumerate(headers):
        old = old_row[index] if index < len(old_row) else ""
        if header not in values_by_name:
            output.append("" if old is None else old)
            continue
        new = values_by_name[header]
        if is_blank(new) and not is_blank(old):
            output.append(old)
        else:
            output.append("" if new is None else new)
    return output


def pace_min_per_km(speed_mps):
    try:
        speed = float(speed_mps)
    except (TypeError, ValueError):
        return ""
    if speed <= 0:
        return ""
    return round(1000 / (speed * 60), 2)


def optional_num(data, key, digits):
    if not isinstance(data, dict) or is_blank(data.get(key)):
        return ""
    return num(data.get(key), digits)


def positive_num(data, key, digits):
    value = optional_num(data, key, digits)
    if value in ("", 0):
        return ""
    return value


def first_positive(data, keys, digits):
    for key in keys:
        value = positive_num(data, key, digits)
        if value != "":
            return value
    return ""


def activity_values(activity, device_map):
    activity_type = as_dict(activity.get("activityType")).get("typeKey") or ""
    start_local = str(activity.get("startTimeLocal") or "")
    activity_day = normalize_sheet_date(start_local)
    if not activity_day:
        return None

    distance_m = activity.get("distance")
    distance_km = "" if is_blank(distance_m) else num(float(distance_m) / 1000, 2)
    duration_s = activity.get("duration")
    duration_min = "" if is_blank(duration_s) else num(float(duration_s) / 60, 2)

    pace = pace_min_per_km(activity.get("averageSpeed"))
    if pace == "" and distance_km not in ("", 0) and duration_min not in ("", 0):
        pace = round(float(duration_min) / float(distance_km), 2)

    device_id = str(activity.get("deviceId") or "")
    activity_id = normalize_id(activity.get("activityId"))

    return {
        "Тип активности": activity_type,
        "Дата": date_to_serial(date.fromisoformat(activity_day)),
        "Избранное": "Да" if activity.get("favorite") else "Нет",
        "Название": activity.get("activityName") or "",
        "Дистанция (км)": distance_km,
        "Калории": positive_num(activity, "calories", 0),
        "Время (мин)": duration_min,
        "Ср. ЧСС": positive_num(activity, "averageHR", 0),
        "Макс. ЧСС": positive_num(activity, "maxHR", 0),
        "Аэробный TE": optional_num(activity, "aerobicTrainingEffect", 1),
        "Ср. каденс": first_positive(
            activity,
            (
                "averageRunningCadenceInStepsPerMinute",
                "averageBikingCadenceInRevPerMinute",
                "averageSwimCadenceInStrokesPerMinute",
                "averageCadence",
            ),
            0,
        ),
        "Макс. каденс": first_positive(
            activity,
            (
                "maxRunningCadenceInStepsPerMinute",
                "maxBikingCadenceInRevPerMinute",
                "maxSwimCadenceInStrokesPerMinute",
                "maxCadence",
            ),
            0,
        ),
        "Ср. темп (мин/км)": pace,
        "Лучший темп (мин/км)": pace_min_per_km(activity.get("maxSpeed")),
        "Набор высоты (м)": optional_num(activity, "elevationGain", 0),
        "Снижение (м)": optional_num(activity, "elevationLoss", 0),
        "Ср. длина шага (см)": positive_num(activity, "avgStrideLength", 1),
        "Ср. верт. соотношение (%)": positive_num(activity, "avgVerticalRatio", 1),
        "Ср. верт. осцилляция (см)": positive_num(activity, "avgVerticalOscillation", 1),
        "Training Load": positive_num(activity, "activityTrainingLoad", 1),
        "ID активности": int(activity_id) if activity_id.isdigit() else activity_id,
        "Локация": activity.get("locationName") or "",
        "Время в движении (мин)": (
            ""
            if is_blank(activity.get("movingDuration"))
            else num(float(activity.get("movingDuration")) / 60, 1)
        ),
        "Затраченное время (мин)": (
            ""
            if is_blank(activity.get("elapsedDuration"))
            else num(float(activity.get("elapsedDuration")) / 60, 1)
        ),
        "Анаэробный TE": optional_num(activity, "anaerobicTrainingEffect", 1),
        "Метка TE": activity.get("trainingEffectLabel") or "",
        "Время контакта с землей (мс)": positive_num(activity, "avgGroundContactTime", 0),
        "Шаги": positive_num(activity, "steps", 0),
        "Ср. мощность (Вт)": positive_num(activity, "avgPower", 0),
        "Макс. мощность (Вт)": positive_num(activity, "maxPower", 0),
        "Норм. мощность (Вт)": positive_num(activity, "normPower", 0),
        "Body Battery Δ": optional_num(activity, "differenceBodyBattery", 0),
        "Умеренные мин": optional_num(activity, "moderateIntensityMinutes", 0),
        "Интенсивные мин": optional_num(activity, "vigorousIntensityMinutes", 0),
        "Кругов": optional_num(activity, "lapCount", 0),
        "Мин. высота (м)": optional_num(activity, "minElevation", 0),
        "Макс. высота (м)": optional_num(activity, "maxElevation", 0),
        "Устройство": device_map.get(device_id, ""),
        "_day": activity_day,
        "_id": activity_id,
    }


def summary_has_metrics(summary):
    if not isinstance(summary, dict) or not summary:
        return False
    return any(
        summary.get(key) is not None
        for key in (
            "totalSteps",
            "restingHeartRate",
            "averageStressLevel",
            "bodyBatteryHighestValue",
        )
    )


def parse_body_battery(entries, date_str):
    battery_max = ""
    battery_min = ""
    levels = []
    if not isinstance(entries, list):
        return "", ""

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_day = normalize_sheet_date(entry.get("calendarDate") or entry.get("date"))
        if entry_day and entry_day != date_str:
            continue
        for key in ("highestBodyBatteryValue", "bodyBatteryHighestValue"):
            if not is_blank(entry.get(key)):
                battery_max = entry.get(key)
                break
        for key in ("lowestBodyBatteryValue", "bodyBatteryLowestValue"):
            if not is_blank(entry.get(key)):
                battery_min = entry.get(key)
                break
        for point in entry.get("bodyBatteryValuesArray") or []:
            level = None
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                level = point[1]
            elif isinstance(point, dict):
                level = point.get("bodyBatteryLevel", point.get("value"))
            if isinstance(level, (int, float)):
                levels.append(level)

    if levels:
        if is_blank(battery_max):
            battery_max = max(levels)
        if is_blank(battery_min):
            battery_min = min(levels)
    return num(battery_max, 0), num(battery_min, 0)


def sleep_fields(sleep):
    dto = as_dict(as_dict(sleep).get("dailySleepDTO"))
    seconds = dto.get("sleepTimeSeconds")
    minutes = ""
    if isinstance(seconds, (int, float)) and seconds > 0:
        minutes = round(float(seconds) / 60, 1)

    scores = dto.get("sleepScores")
    overall = scores.get("overall") if isinstance(scores, dict) else None
    if isinstance(overall, dict):
        score = overall.get("value")
    elif isinstance(overall, (int, float)):
        score = overall
    else:
        score = ""
    return minutes, num(score, 0)


def hrv_fields(hrv):
    summary = as_dict(as_dict(hrv).get("hrvSummary"))
    average = summary.get("lastNightAvg")
    status = summary.get("status") or ""
    return num(average, 0), status


def vo2_pair(training_status, max_metrics, date_str):
    recent = as_dict(as_dict(training_status).get("mostRecentVO2Max"))
    generic = as_dict(recent.get("generic"))
    cycle = as_dict(recent.get("cycling"))
    running = first_filled(generic.get("vo2MaxPreciseValue"), generic.get("vo2MaxValue"))
    cycling = first_filled(cycle.get("vo2MaxPreciseValue"), cycle.get("vo2MaxValue"))

    entries = []
    if isinstance(max_metrics, list):
        entries = max_metrics
    elif isinstance(max_metrics, dict):
        for key in ("maxMetricValues", "metrics", "metricDTOs"):
            if isinstance(max_metrics.get(key), list):
                entries.extend(max_metrics[key])
        running = first_filled(
            running,
            max_metrics.get("vo2MaxPreciseValue"),
            max_metrics.get("vo2MaxValue"),
        )
        cycling = first_filled(cycling, max_metrics.get("vo2MaxCyclingValue"))

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_day = normalize_sheet_date(entry.get("calendarDate") or entry.get("date"))
        if entry_day and entry_day != date_str:
            continue
        running = first_filled(
            running,
            entry.get("vo2MaxPreciseValue"),
            entry.get("vo2MaxValue"),
        )
        cycling = first_filled(cycling, entry.get("vo2MaxCyclingValue"))
    return num(running, 1), num(cycling, 1)


def index_body_battery(payload):
    by_date = {}
    if not isinstance(payload, list):
        return by_date
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        day = normalize_sheet_date(entry.get("calendarDate") or entry.get("date"))
        if day:
            by_date.setdefault(day, []).append(entry)
    return by_date


def safe_call(func, *args, default=None, **kwargs):
    name = getattr(func, "__name__", "call")
    retry_markers = ("429", "500", "502", "503", "504", "timed out", "Timeout", "Connection")
    for attempt in range(4):
        try:
            result = func(*args, **kwargs)
            return default if result is None else result
        except Exception as exc:
            message = str(exc).splitlines()[0][:300]
            if any(marker in message for marker in retry_markers) and attempt < 3:
                wait = 15 * (attempt + 1)
                logger.warning("%s: %s. Повтор через %s с", name, message, wait)
                time.sleep(wait)
                continue
            logger.warning("%s: %s", name, message)
            return default
    return default


def connect_garmin(email, password):
    os.makedirs(TOKEN_DIR, exist_ok=True)
    token_file = os.path.join(TOKEN_DIR, "oauth1_token.json")
    if os.path.isfile(token_file):
        garmin = Garmin(email, password)
        try:
            garmin.login(TOKEN_DIR)
            logger.info("Сессия Garmin восстановлена из %s", TOKEN_DIR)
            save_tokens(garmin)
            return garmin
        except Exception as exc:
            logger.warning("Сохранённая сессия не подошла (%s), вход по паролю", exc)

    garmin = Garmin(email, password)
    garmin.login()
    save_tokens(garmin)
    logger.info("Вход в Garmin по паролю, токены сохранены")
    return garmin


def save_tokens(garmin):
    try:
        os.makedirs(TOKEN_DIR, exist_ok=True)
        garmin.garth.dump(TOKEN_DIR)
    except Exception as exc:
        logger.warning("Не удалось сохранить токены Garmin: %s", exc)


def sheets_call(func, *args, **kwargs):
    for attempt in range(4):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            message = str(exc)
            if ("429" in message or "Quota" in message) and attempt < 3:
                time.sleep(20 * (attempt + 1))
                continue
            raise
    return func(*args, **kwargs)


def ensure_headers(sheet, expected):
    current = clean_headers(sheets_call(sheet.row_values, 1))
    if not current:
        sheets_call(sheet.update, range_name="A1", values=[expected], value_input_option="RAW")
        logger.info("Созданы заголовки на листе %s", sheet.title)
        return list(expected)

    missing = [header for header in expected if header not in current]
    if missing:
        current = current + missing
        sheets_call(sheet.update, range_name="A1", values=[current], value_input_option="RAW")
        logger.info("На лист %s добавлены столбцы: %s", sheet.title, ", ".join(missing))
    return current


def read_rows(sheet):
    rows = sheets_call(sheet.get_all_values, value_render_option="UNFORMATTED_VALUE")
    return rows or []


def write_cells(sheet, cells):
    for start in range(0, len(cells), 100):
        chunk = cells[start:start + 100]
        data = [
            {"range": rowcol_to_a1(row, col), "values": [[value]]}
            for row, col, value in chunk
        ]
        sheets_call(sheet.batch_update, data, value_input_option="USER_ENTERED")


def write_rows(sheet, rows):
    """rows: list of (row_number, values)."""
    for start in range(0, len(rows), 50):
        chunk = rows[start:start + 50]
        data = []
        for row_number, values in chunk:
            data.append({
                "range": f"{rowcol_to_a1(row_number, 1)}:{rowcol_to_a1(row_number, len(values))}",
                "values": [values],
            })
        sheets_call(sheet.batch_update, data, value_input_option="USER_ENTERED")


def format_date_column(sheet, col_index):
    letter = column_letter(col_index)
    try:
        sheets_call(
            sheet.format,
            f"{letter}:{letter}",
            {"numberFormat": {"type": "DATE", "pattern": "dd.mm.yyyy"}},
        )
    except Exception as exc:
        logger.warning("Не удалось задать формат даты на %s: %s", sheet.title, exc)


def repair_stored_values(rows, headers):
    """Turn text dates and '5,080' into real numbers. Returns cells to write."""
    cells = []
    date_index = headers.index("Дата") if "Дата" in headers else None
    for row_offset, row in enumerate(rows[1:], start=2):
        for col_offset, header in enumerate(headers):
            if col_offset >= len(row):
                continue
            value = row[col_offset]
            column = col_offset + 1
            if header == "Дата" or col_offset == date_index:
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    continue
                iso = normalize_sheet_date(value)
                if not iso:
                    continue
                serial = date_to_serial(date.fromisoformat(iso))
                row[col_offset] = serial
                cells.append((row_offset, column, serial))
                continue
            if header in TEXT_HEADERS or not isinstance(value, str):
                continue
            number = parse_decimal_text(value)
            if number is None:
                continue
            row[col_offset] = number
            cells.append((row_offset, column, number))
    return cells


def last_data_row(rows, key_indexes):
    last = 1
    for row_number, row in enumerate(rows[1:], start=2):
        if any(index < len(row) and not is_blank(row[index]) for index in key_indexes):
            last = row_number
    return last


def sheet_width(rows, headers):
    return max([len(headers)] + [len(row) for row in rows])


def sort_by_date(sheet, date_col, last_row, width):
    if last_row < 3 or width < 1:
        return
    end = rowcol_to_a1(last_row, width)
    sheets_call(sheet.sort, (date_col, "asc"), range=f"A2:{end}")
    logger.info("Лист %s отсортирован по дате", sheet.title)


def index_activities(rows, headers):
    id_col = headers.index("ID активности")
    date_col = headers.index("Дата")
    name_col = headers.index("Название")
    by_id = {}
    by_name = {}
    for row_number, row in enumerate(rows[1:], start=2):
        activity_id = normalize_id(row[id_col] if id_col < len(row) else "")
        day = normalize_sheet_date(row[date_col] if date_col < len(row) else "")
        name = str(row[name_col]).strip() if name_col < len(row) and not is_blank(row[name_col]) else ""
        if activity_id and activity_id not in by_id:
            by_id[activity_id] = row_number
        if day and name and (day, name) not in by_name:
            by_name[(day, name)] = row_number
    return by_id, by_name


def index_daily(rows, headers):
    date_col = headers.index("Дата")
    by_date = {}
    duplicates = 0
    for row_number, row in enumerate(rows[1:], start=2):
        day = normalize_sheet_date(row[date_col] if date_col < len(row) else "")
        if not day:
            continue
        if day in by_date:
            duplicates += 1
            continue
        by_date[day] = row_number
    if duplicates:
        logger.warning("На Daily %s повторных строк с той же датой — обновляю первую", duplicates)
    return by_date


def find_activity_row(values, rows, headers, by_id, by_name):
    activity_id = values["_id"]
    if activity_id and activity_id in by_id:
        return by_id[activity_id]

    name = str(values.get("Название") or "").strip()
    key = (values["_day"], name)
    if not name or key not in by_name:
        return None
    row_number = by_name[key]
    id_col = headers.index("ID активности")
    row = rows[row_number - 1]
    if is_blank(normalize_id(row[id_col] if id_col < len(row) else "")):
        return row_number
    return None


def device_map_from(garmin):
    devices = safe_call(garmin.get_devices, default=[]) or []
    mapping = {}
    for device in devices:
        if not isinstance(device, dict):
            continue
        device_id = str(device.get("deviceId") or "")
        name = device.get("displayName") or device.get("productDisplayName") or ""
        if device_id:
            mapping[device_id] = name
    return mapping


def build_daily_values(garmin, date_str, battery_by_date, fetch_vo2):
    summary = as_dict(safe_call(garmin.get_user_summary, date_str, default={}))
    if not summary_has_metrics(summary):
        return None

    stress = summary.get("averageStressLevel")
    if is_blank(stress):
        stress_data = as_dict(safe_call(garmin.get_all_day_stress, date_str, default={}))
        if not stress_data:
            stress_data = as_dict(safe_call(garmin.get_stress_data, date_str, default={}))
        stress = (
            stress_data.get("avgStressLevel")
            or stress_data.get("averageStressLevel")
            or stress_data.get("stressLevel")
        )

    battery_max = num(summary.get("bodyBatteryHighestValue"), 0)
    battery_min = num(summary.get("bodyBatteryLowestValue"), 0)
    if battery_max == "" or battery_min == "":
        entries = battery_by_date.get(date_str)
        if entries is None:
            entries = safe_call(garmin.get_body_battery, date_str, date_str, default=[]) or []
        parsed_max, parsed_min = parse_body_battery(entries, date_str)
        if battery_max == "":
            battery_max = parsed_max
        if battery_min == "":
            battery_min = parsed_min

    respiration = summary.get("averageWakingRespirationValue")
    if is_blank(respiration):
        respiration = summary.get("averageSleepRespirationValue")
    if is_blank(respiration):
        breath = as_dict(safe_call(garmin.get_respiration_data, date_str, default={}))
        respiration = (
            breath.get("avgWakingRespirationValue")
            or breath.get("avgSleepRespirationValue")
            or breath.get("avgWakingRespiration")
        )

    spo2 = summary.get("averageSpo2")
    if is_blank(spo2):
        spo2_data = as_dict(safe_call(garmin.get_spo2_data, date_str, default={}))
        spo2 = spo2_data.get("averageSpO2") or spo2_data.get("avgSleepSpO2")

    hrv_avg, hrv_status = hrv_fields(safe_call(garmin.get_hrv_data, date_str, default={}))
    sleep_min, sleep_score = sleep_fields(safe_call(garmin.get_sleep_data, date_str, default={}))

    vo2_running, vo2_cycling = "", ""
    if fetch_vo2:
        training = safe_call(garmin.get_training_status, date_str, default={})
        metrics = safe_call(garmin.get_max_metrics, date_str, default={})
        vo2_running, vo2_cycling = vo2_pair(training, metrics, date_str)

    return {
        "Дата": date_to_serial(date.fromisoformat(date_str)),
        "Шаги": num(summary.get("totalSteps"), 0),
        "Этажи": num(summary.get("floorsAscended"), 1),
        "Стресс": num(stress, 0),
        "Body Battery Макс": battery_max,
        "Body Battery Мин": battery_min,
        "HRV Ср.": hrv_avg,
        "HRV Статус": hrv_status,
        "Дыхание": num(respiration, 1),
        "SpO2": num(spo2, 0),
        "Сон Всего (мин)": sleep_min,
        "Оценка сна": sleep_score,
        "ЧСС покоя": num(summary.get("restingHeartRate"), 0),
        "VO2 Max Бег": vo2_running,
        "VO2 Max Вело": vo2_cycling,
    }


def sync_activities(sheet, garmin, today):
    headers = ensure_headers(sheet, ACTIVITY_HEADERS)
    date_col = headers.index("Дата") + 1
    format_date_column(sheet, date_col)

    rows = read_rows(sheet)
    if not rows:
        rows = [headers]
    width = sheet_width(rows, headers)
    repaired = repair_stored_values(rows, headers)
    if repaired:
        write_cells(sheet, repaired)
        logger.info("На Activities поправлены форматы ячеек: %s", len(repaired))

    by_id, by_name = index_activities(rows, headers)
    start = resolve_start_date(today)
    activities = safe_call(
        garmin.get_activities_by_date,
        start.isoformat(),
        today.isoformat(),
        default=[],
    ) or []
    activities = sorted(activities, key=lambda item: str(item.get("startTimeLocal") or ""))
    logger.info("Garmin отдал тренировок: %s", len(activities))

    devices = device_map_from(garmin)
    id_col = headers.index("ID активности")
    name_col = headers.index("Название")
    next_row = last_data_row(rows, [headers.index("Дата"), id_col]) + 1
    pending = []
    inserted = 0
    updated = 0

    for activity in activities:
        if not isinstance(activity, dict):
            continue
        values = activity_values(activity, devices)
        if not values:
            continue
        row_number = find_activity_row(values, rows, headers, by_id, by_name)
        in_window = activity_in_refresh_window(values["_day"], today, ACTIVITY_REFRESH_DAYS)
        if row_number is not None and not in_window:
            continue

        payload = {key: val for key, val in values.items() if not key.startswith("_")}
        if row_number is None:
            old_row = []
            row_number = next_row
            next_row += 1
            inserted += 1
            while len(rows) < row_number:
                rows.append([""] * len(headers))
        else:
            old_row = rows[row_number - 1]
            updated += 1

        output = build_output_row(headers, payload, old_row)
        rows[row_number - 1] = output
        pending.append((row_number, output))
        activity_id = values["_id"]
        if activity_id:
            by_id[activity_id] = row_number
        name = str(values.get("Название") or "").strip()
        if name:
            by_name[(values["_day"], name)] = row_number

    if pending:
        write_rows(sheet, pending)
    last_row = last_data_row(rows, [headers.index("Дата"), id_col, name_col])
    if inserted or repaired:
        sort_by_date(sheet, date_col, last_row, width)
    return inserted, updated


def sync_daily(sheet, garmin, today):
    headers = ensure_headers(sheet, DAILY_HEADERS)
    date_col = headers.index("Дата") + 1
    format_date_column(sheet, date_col)

    rows = read_rows(sheet)
    if not rows:
        rows = [headers]
    width = sheet_width(rows, headers)
    repaired = repair_stored_values(rows, headers)
    if repaired:
        write_cells(sheet, repaired)
        logger.info("На Daily поправлены форматы ячеек: %s", len(repaired))

    by_date = index_daily(rows, headers)
    start = resolve_start_date(today)
    dates = daily_dates_to_sync(set(by_date), today, start, DAILY_REFRESH_DAYS)
    if not dates:
        if repaired:
            last_row = last_data_row(rows, [headers.index("Дата")])
            sort_by_date(sheet, date_col, last_row, width)
        return 0, 0

    logger.info("Daily к записи: %s (%s .. %s)", len(dates), dates[0], dates[-1])
    battery_payload = safe_call(garmin.get_body_battery, dates[0], dates[-1], default=[]) or []
    battery_by_date = index_body_battery(battery_payload)
    refresh_from = (today - timedelta(days=DAILY_REFRESH_DAYS)).isoformat()

    next_row = last_data_row(rows, [headers.index("Дата")]) + 1
    pending = []
    written = 0
    skipped = 0
    for date_str in dates:
        values = build_daily_values(
            garmin,
            date_str,
            battery_by_date,
            fetch_vo2=date_str >= refresh_from,
        )
        if values is None:
            skipped += 1
            logger.warning("Daily %s пропущен: Garmin не отдал сводку", date_str)
            continue

        row_number = by_date.get(date_str)
        if row_number is None:
            old_row = []
            row_number = next_row
            next_row += 1
            while len(rows) < row_number:
                rows.append([""] * len(headers))
            by_date[date_str] = row_number
        else:
            old_row = rows[row_number - 1]

        output = build_output_row(headers, values, old_row)
        rows[row_number - 1] = output
        pending.append((row_number, output))
        written += 1
        time.sleep(0.4)

    if pending:
        write_rows(sheet, pending)
    last_row = last_data_row(rows, [headers.index("Дата")])
    if written or repaired:
        sort_by_date(sheet, date_col, last_row, width)
    return written, skipped


def main():
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    sheet_id = normalize_sheet_id(os.getenv("SHEET_ID"))
    credentials = os.getenv("GOOGLE_CREDENTIALS")
    if not all([email, password, sheet_id, credentials]):
        raise SystemExit("Нужны GARMIN_EMAIL, GARMIN_PASSWORD, GOOGLE_CREDENTIALS и SHEET_ID")

    today = local_today()
    logger.info(
        "Старт синхронизации, сегодня %s (%s), Daily обновляет %s дн., тренировки %s дн.",
        today.isoformat(),
        SYNC_TIMEZONE,
        DAILY_REFRESH_DAYS,
        ACTIVITY_REFRESH_DAYS,
    )

    garmin = connect_garmin(email, password)
    try:
        creds = Credentials.from_service_account_info(
            json.loads(credentials),
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        spreadsheet = gspread.authorize(creds).open_by_key(sheet_id)
        inserted, updated = sync_activities(spreadsheet.worksheet("Activities"), garmin, today)
        written, skipped = sync_daily(spreadsheet.worksheet("Daily"), garmin, today)
    finally:
        save_tokens(garmin)

    logger.info("Добавлено тренировок: %s", inserted)
    logger.info("Обновлено тренировок: %s", updated)
    logger.info("Записано дней Daily: %s", written)
    logger.info("Пропущено дней Daily: %s", skipped)


if __name__ == "__main__":
    main()
