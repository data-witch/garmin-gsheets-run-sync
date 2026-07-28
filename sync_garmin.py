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
SYNC_TIMEZONE
"""

import os
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
from garminconnect import Garmin
from google.oauth2.service_account import Credentials


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


SYNC_DAYS = int(os.getenv("SYNC_DAYS", "210"))
SYNC_TIMEZONE = os.getenv("SYNC_TIMEZONE", "Asia/Novosibirsk")

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


def pick_value(*values, default=""):
    for value in values:
        if value is None:
            continue
        if value == "":
            continue
        return value
    return default


def get_local_today():
    return datetime.now(ZoneInfo(SYNC_TIMEZONE)).date()


def get_daily_row_index(sheet):
    rows = sheet.get_all_values()
    date_to_row = {}
    for row_number, row in enumerate(rows[1:], start=2):
        if row and row[0]:
            date_to_row[row[0]] = row_number
    return date_to_row


def upsert_daily_rows(sheet, rows_by_date):
    date_to_row = get_daily_row_index(sheet)
    rows_to_append = []

    for date_str in sorted(rows_by_date):
        row = rows_by_date[date_str]
        if date_str in date_to_row:
            row_number = date_to_row[date_str]
            sheet.update(
                range_name=f"A{row_number}",
                values=[row],
                value_input_option="USER_ENTERED",
            )
            logger.info("Updated Daily row for %s (row %s)", date_str, row_number)
        else:
            rows_to_append.append(row)

    if rows_to_append:
        sheet.append_rows(rows_to_append, value_input_option="USER_ENTERED")
        logger.info("Added %s new Daily rows", len(rows_to_append))


def get_daily_dates_to_sync(existing_dates):
    """
    First run: backfill all missing days in the SYNC_DAYS window.
    Regular run: add only missing days and always refresh yesterday.
    Today is never written — the day is still in progress.
    """
    today = get_local_today()
    yesterday = today - timedelta(days=1)
    start_date = yesterday - timedelta(days=SYNC_DAYS - 1)

    dates_to_sync = set()
    current = start_date
    while current <= yesterday:
        date_str = current.isoformat()
        if date_str not in existing_dates:
            dates_to_sync.add(date_str)
        current += timedelta(days=1)

    dates_to_sync.add(yesterday.isoformat())
    return sorted(dates_to_sync), yesterday.isoformat()


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


def parse_body_battery(summary, battery_payload, date_str):
    battery_max = pick_value(
        get_value(summary, "bodyBatteryHighestValue"),
        default="",
    )
    battery_min = pick_value(
        get_value(summary, "bodyBatteryLowestValue"),
        default="",
    )
    if battery_max != "" and battery_min != "":
        return battery_max, battery_min

    values = []
    if isinstance(battery_payload, list):
        for entry in battery_payload:
            if entry.get("calendarDate") not in (date_str, None, ""):
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
                level = point.get("bodyBatteryLevel")
                if level is not None:
                    values.append(level)

            for key in ("value", "charged", "drained"):
                level = entry.get(key)
                if isinstance(level, (int, float)):
                    values.append(level)

    if values:
        return max(values), min(values)
    return battery_max, battery_min


def parse_vo2_max(garmin, date_str, training_status=None):
    training_status = training_status if training_status is not None else safe_call(
        garmin.get_training_status,
        date_str,
        default={},
    ) or {}

    most_recent_vo2max = training_status.get("mostRecentVO2Max") or {}
    vo2_running = pick_value(
        (most_recent_vo2max.get("generic") or {}).get("vo2MaxValue"),
        default="",
    )
    vo2_cycling = pick_value(
        (most_recent_vo2max.get("cycling") or {}).get("vo2MaxValue"),
        default="",
    )
    if vo2_running != "" or vo2_cycling != "":
        return vo2_running, vo2_cycling

    max_metrics = safe_call(garmin.get_max_metrics, date_str, default={}) or {}
    if isinstance(max_metrics, list):
        for entry in max_metrics:
            if entry.get("calendarDate") not in (date_str, None, ""):
                continue
            vo2_running = pick_value(entry.get("vo2MaxValue"), vo2_running, default="")
            vo2_cycling = pick_value(entry.get("vo2MaxCyclingValue"), vo2_cycling, default="")
    elif isinstance(max_metrics, dict):
        metrics_list = max_metrics.get("metricsMap") or max_metrics.get("metricDTOs") or []
        if isinstance(metrics_list, list):
            for entry in metrics_list:
                metric_type = entry.get("metricsType") or entry.get("metricType")
                if metric_type in ("VO2_MAX", "vo2max", "generic"):
                    vo2_running = pick_value(entry.get("vo2MaxValue"), vo2_running, default="")
                if metric_type in ("CYCLING_VO2_MAX", "cycling_vo2max", "cycling"):
                    vo2_cycling = pick_value(entry.get("vo2MaxValue"), vo2_cycling, default="")
        vo2_running = pick_value(
            max_metrics.get("vo2MaxValue"),
            (max_metrics.get("generic") or {}).get("vo2MaxValue"),
            vo2_running,
            default="",
        )
        vo2_cycling = pick_value(
            max_metrics.get("vo2MaxCyclingValue"),
            (max_metrics.get("cycling") or {}).get("vo2MaxValue"),
            vo2_cycling,
            default="",
        )

    return vo2_running, vo2_cycling


def build_daily_row(garmin, date_str):
    summary = safe_call(garmin.get_user_summary, date_str, default={}) or {}
    stress_data = safe_call(garmin.get_all_day_stress, date_str, default={}) or {}
    if not stress_data:
        stress_data = safe_call(garmin.get_stress_data, date_str, default={}) or {}
    battery_payload = safe_call(garmin.get_body_battery, date_str, date_str, default=[]) or []
    hrv = safe_call(garmin.get_hrv_data, date_str, default={}) or {}
    spo2 = safe_call(garmin.get_spo2_data, date_str, default={}) or {}
    respiration = safe_call(garmin.get_respiration_data, date_str, default={}) or {}
    sleep = safe_call(garmin.get_sleep_data, date_str, default={}) or {}
    training_status = safe_call(garmin.get_training_status, date_str, default={}) or {}

    steps = pick_value(get_value(summary, "totalSteps"), default="")
    floors = pick_value(get_value(summary, "floorsAscended"), default="")
    resting_hr = pick_value(get_value(summary, "restingHeartRate"), default="")

    stress_level = pick_value(
        get_value(summary, "averageStressLevel"),
        stress_data.get("avgStressLevel"),
        stress_data.get("averageStressLevel"),
        stress_data.get("stressLevel"),
        default="",
    )

    battery_max, battery_min = parse_body_battery(summary, battery_payload, date_str)

    hrv_summary = hrv.get("hrvSummary") or {}
    hrv_avg = pick_value(hrv_summary.get("lastNightAvg"), default="")
    hrv_status = pick_value(hrv_summary.get("status"), default="")

    respiration_value = pick_value(
        get_value(summary, "averageWakingRespirationValue"),
        get_value(summary, "averageSleepRespirationValue"),
        respiration.get("avgWakingRespirationValue"),
        respiration.get("avgSleepRespirationValue"),
        respiration.get("avgWakingRespiration"),
        default="",
    )

    spo2_value = pick_value(
        get_value(summary, "averageSpo2"),
        spo2.get("averageSpO2"),
        spo2.get("avgSleepSpO2"),
        spo2.get("lowestSpO2"),
        default="",
    )

    sleep_dto = sleep.get("dailySleepDTO") or {}
    sleep_time = pick_value(get_value(sleep_dto, "sleepTimeSeconds"), default="")
    sleep_score = pick_value(
        (sleep_dto.get("sleepScores") or {}).get("overall", {}).get("value"),
        default="",
    )

    vo2_running, vo2_cycling = parse_vo2_max(garmin, date_str, training_status)

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
    existing_dates = set(get_daily_row_index(daily_sheet))

    today_local = get_local_today()
    yesterday_local = (today_local - timedelta(days=1)).isoformat()
    start_date = today_local - timedelta(days=SYNC_DAYS)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = today_local.strftime("%Y-%m-%d")

    logger.info(
        "Sync window: %s .. %s (local timezone %s, yesterday=%s)",
        start_str,
        end_str,
        SYNC_TIMEZONE,
        yesterday_local,
    )

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

    daily_dates, refresh_date = get_daily_dates_to_sync(existing_dates)
    daily_rows_by_date = {}
    for date_str in daily_dates:
        logger.info("Fetching Daily metrics for %s%s", date_str, " (refresh)" if date_str == refresh_date else "")
        daily_rows_by_date[date_str] = build_daily_row(garmin, date_str)

    if daily_rows_by_date:
        upsert_daily_rows(daily_sheet, daily_rows_by_date)

    logger.info("Добавлено тренировок: %s", len(activity_rows))
    logger.info("Обработано дней Daily: %s", len(daily_rows_by_date))


if __name__ == "__main__":
    main()
