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
SYNC_DAYS
GARMIN_TOKEN_DIR
"""

import os
import json
import logging
from datetime import datetime, timedelta, timezone

import gspread
from garminconnect import Garmin
from google.oauth2.service_account import Credentials


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


SYNC_DAYS = int(os.getenv("SYNC_DAYS", "30"))

TOKEN_DIR = os.path.expanduser(
    os.getenv(
        "GARMIN_TOKEN_DIR",
        "~/.garth"
    )
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
    "VO2 Max Вело"
]


def safe_call(func, *args, default=None):
    try:
        result = func(*args)
        if result is None:
            return default
        return result
    except Exception as e:
        logger.warning(
            "API error %s: %s",
            getattr(func, "__name__", "unknown"),
            e
        )
        return default


def get_value(data, key, default=""):
    if not isinstance(data, dict):
        return default
    value = data.get(key)
    if value is None:
        return default
    return value


def connect_garmin(email, password):
    garmin = Garmin(email, password)

    if os.path.exists(TOKEN_DIR):
        try:
            garmin.login(tokenstore=TOKEN_DIR)
            logger.info("Garmin login by token")
            return garmin
        except Exception:
            logger.info("Token expired")

    garmin.login()

    try:
        os.makedirs(TOKEN_DIR, exist_ok=True)
        if hasattr(garmin, 'garth') and hasattr(garmin.garth, 'dump'):
            garmin.garth.dump(TOKEN_DIR)
            logger.info("Tokens saved to %s", TOKEN_DIR)
        elif hasattr(garmin, 'dump_tokens'):
            garmin.dump_tokens(TOKEN_DIR)
            logger.info("Tokens saved to %s", TOKEN_DIR)
    except Exception as e:
        logger.warning("Token save error: %s", e)

    return garmin


def seconds_to_minutes(value):
    if not value:
        return 0
    return round(value / 60, 1)


def speed_to_pace_min_per_km(speed_mps):
    """Convert speed (m/s) to pace (min/km)."""
    if not speed_mps or speed_mps <= 0:
        return ""
    return round(1000 / (speed_mps * 60), 2)


def round_value(value, digits=2):
    if value is None or value == "":
        return ""
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return value


def get_device_map(garmin):
    devices = safe_call(garmin.get_devices, default=[]) or []
    device_map = {}
    for device in devices:
        device_id = str(get_value(device, "deviceId"))
        device_map[device_id] = get_value(device, "displayName") or get_value(device, "productDisplayName")
    return device_map


def ensure_sheet_headers(sheet, expected_headers):
    current_headers = sheet.row_values(1)
    if not current_headers:
        sheet.update(range_name="A1", values=[expected_headers])
        logger.info("Created headers")
        return

    missing = [header for header in expected_headers if header not in current_headers]
    if missing:
        sheet.update(range_name="A1", values=[current_headers + missing])
        logger.info("Added missing headers: %s", ", ".join(missing))
    elif current_headers[:len(expected_headers)] != expected_headers:
        sheet.update(range_name="A1", values=[expected_headers])
        logger.info("Updated headers")


def get_existing_activity_keys(sheet, activity_id_column):
    rows = sheet.get_all_values()
    existing_ids = set()
    existing_name_keys = set()

    name_column = ACTIVITY_HEADERS.index("Название")
    date_column = ACTIVITY_HEADERS.index("Дата")

    for row in rows[1:]:
        if len(row) <= date_column or not row[date_column]:
            continue

        if len(row) > activity_id_column and row[activity_id_column]:
            existing_ids.add(str(row[activity_id_column]))
        elif len(row) > name_column and row[name_column]:
            existing_name_keys.add((row[date_column], row[name_column]))

    return existing_ids, existing_name_keys


def is_activity_existing(activity, existing_ids, existing_name_keys):
    activity_id = str(activity.get("activityId", ""))
    activity_date = get_value(activity, "startTimeLocal")[:10]
    activity_name = get_value(activity, "activityName")

    if activity_id and activity_id in existing_ids:
        return True
    return (activity_date, activity_name) in existing_name_keys


def build_activity_row(activity, device_map):
    """Build one Activities row from Garmin activity list payload."""
    activity_type = get_value(activity.get("activityType", {}), "typeKey")
    start_time_local = get_value(activity, "startTimeLocal")
    activity_date = start_time_local[:10] if start_time_local else ""
    distance = float(get_value(activity, "distance", 0) or 0)
    duration = float(get_value(activity, "duration", 0) or 0)
    moving_duration = float(get_value(activity, "movingDuration", 0) or 0)
    elapsed_duration = float(get_value(activity, "elapsedDuration", 0) or 0)

    avg_pace = speed_to_pace_min_per_km(get_value(activity, "averageSpeed", 0))
    if not avg_pace and distance:
        avg_pace = round_value((duration / (distance / 1000)) / 60)

    best_pace = speed_to_pace_min_per_km(get_value(activity, "maxSpeed", 0))

    avg_cadence = (
        get_value(activity, "averageRunningCadenceInStepsPerMinute")
        or get_value(activity, "averageBikingCadenceInRevPerMinute")
        or get_value(activity, "averageCadence")
        or ""
    )
    max_cadence = (
        get_value(activity, "maxRunningCadenceInStepsPerMinute")
        or get_value(activity, "maxBikingCadenceInRevPerMinute")
        or get_value(activity, "maxCadence")
        or ""
    )

    device_id = str(get_value(activity, "deviceId"))
    device_name = device_map.get(device_id, "")

    return [
        activity_type,
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
        device_name,
    ]


def build_daily_row(garmin, date):
    # Все данные из user_summary
    summary = safe_call(garmin.get_user_summary, date, default={})
    
    # Стресс
    stress = safe_call(garmin.get_stress_data, date, default={})
    
    # Body Battery - ПРАВИЛЬНОЕ НАЗВАНИЕ МЕТОДА!
    battery = safe_call(garmin.get_body_battery, date, date, default=[])
    
    # HRV
    hrv = safe_call(garmin.get_hrv_data, date, default={})
    
    # SpO2
    spo2 = safe_call(garmin.get_spo2_data, date, default={})
    
    # Сон
    sleep = safe_call(garmin.get_sleep_data, date, default={})
    
    # VO2 Max
    vo2 = safe_call(garmin.get_max_metrics, date, default={})

    # Извлекаем значения
    steps = get_value(summary, "totalSteps", 0) or 0
    floors = get_value(summary, "floorsAscended", 0) or 0
    resting_hr = get_value(summary, "restingHeartRate", 0) or 0
    
    respiration_value = get_value(summary, "averageWakingRespirationValue", 0) or 0
    if not respiration_value:
        respiration_value = get_value(summary, "averageSleepRespirationValue", 0) or 0
    
    stress_level = get_value(stress, "stressLevel", 0) or 0
    
    # Body Battery - правильная обработка
    battery_max = 0
    battery_min = 0
    if battery and isinstance(battery, list) and len(battery) > 0:
        values = []
        for b in battery:
            val = get_value(b, "value", 0)
            if val:
                values.append(val)
        if values:
            battery_max = max(values)
            battery_min = min(values)
    
    hrv_summary = hrv.get("hrvSummary", {})
    hrv_avg = get_value(hrv_summary, "lastNightAvg", 0) or 0
    hrv_status = get_value(hrv_summary, "status", "") or ""
    
    spo2_value = (
        get_value(spo2, "averageSpO2", 0) or
        get_value(spo2, "avgSleepSpO2", 0) or
        0
    )
    
    sleep_dto = sleep.get("dailySleepDTO", {})
    sleep_time = get_value(sleep_dto, "sleepTimeSeconds", 0) or 0
    sleep_score = get_value(
        sleep_dto.get("sleepScores", {}).get("overall", {}),
        "value",
        0
    ) or 0
    
    vo2_running = get_value(vo2, "vo2MaxValue", 0) or 0
    vo2_cycling = get_value(vo2, "vo2MaxCyclingValue", 0) or 0

    return [
        date,
        steps,
        floors,
        stress_level,
        battery_max,
        battery_min,
        hrv_avg,
        hrv_status,
        respiration_value,
        spo2_value,
        seconds_to_minutes(sleep_time),
        sleep_score,
        resting_hr,
        vo2_running,
        vo2_cycling
    ]


def main():
    logger.info("Garmin sync started (Activities + Daily, %s days)", SYNC_DAYS)
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    sheet_id = os.getenv("SHEET_ID")
    credentials = os.getenv("GOOGLE_CREDENTIALS")

    if not all([email, password, sheet_id, credentials]):
        raise Exception("Missing environment variables")

    garmin = connect_garmin(email, password)

    creds = Credentials.from_service_account_info(
        json.loads(credentials),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
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
    existing_dates = {
        row[0]
        for row in daily_sheet.get_all_values()[1:]
        if row and row[0]
    }

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=SYNC_DAYS)
    
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    device_map = get_device_map(garmin)

    activities = safe_call(
        garmin.get_activities_by_date,
        start_str,
        end_str,
        default=[]
    ) or []

    activity_rows = []
    for activity in activities:
        if is_activity_existing(activity, existing_activity_ids, existing_activity_name_keys):
            continue
        activity_rows.append(build_activity_row(activity, device_map))

    if activity_rows:
        activities_sheet.append_rows(activity_rows, value_input_option="USER_ENTERED")

    daily_rows = []
    current = start
    while current <= end:
        date = current.strftime("%Y-%m-%d")
        if date not in existing_dates:
            daily_rows.append(build_daily_row(garmin, date))
        current += timedelta(days=1)

    if daily_rows:
        daily_sheet.append_rows(daily_rows, value_input_option="USER_ENTERED")

    logger.info("Добавлено тренировок: %s", len(activity_rows))
    logger.info("Добавлено дней: %s", len(daily_rows))


if __name__ == "__main__":
    main()
