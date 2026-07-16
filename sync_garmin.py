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
    "Дата",
    "ID активности",
    "Название активности",
    "Дистанция (км)",
    "Длительность (мин)",
    "Средний темп (мин/км)",
    "Ср. ЧСС",
    "Макс. ЧСС",
    "Калории",
    "Ср. Каденс",
    "Набор высоты (м)",
    "Тип активности",
    "Training Effect аэробный",
    "Training Effect анаэробный",
    "Время восстановления (ч)",
    "Статус тренировки",
    "Время контакта с землей (мс)",
    "Вертикальная осцилляция (см)",
    "Длина шага (см)"
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


def get_existing_values(sheet, column):
    rows = sheet.get_all_values()
    return {
        row[column]
        for row in rows[1:]
        if len(row) > column and row[column]
    }


def build_activity_row(garmin, activity, training_status):
    details = safe_call(
        garmin.get_activity_details,
        activity.get("activityId"),
        default={}
    )

    summary = {}
    if details:
        summary = details.get("summaryDTO", {})

    distance = get_value(activity, "distance", 0)
    duration = get_value(activity, "duration", 0)

    pace = 0
    if distance:
        pace = round((duration / (distance / 1000)) / 60, 2)

    cadence = (
        get_value(activity, "averageRunningCadenceInStepsPerMinute", 0) or
        get_value(activity, "averageCadence", 0) or
        0
    )

    ground_contact_time = get_value(activity, "avgGroundContactTime", 0) or 0
    vertical_oscillation = get_value(activity, "avgVerticalOscillation", 0) or 0
    stride_length = get_value(activity, "avgStrideLength", 0) or 0

    return [
        get_value(activity, "startTimeLocal")[:10],
        str(get_value(activity, "activityId")),
        get_value(activity, "activityName"),
        round(distance / 1000, 2),
        round(duration / 60, 2),
        pace,
        get_value(activity, "averageHR", 0),
        get_value(activity, "maxHR", 0),
        get_value(activity, "calories", 0),
        cadence,
        get_value(activity, "elevationGain", 0),
        get_value(activity.get("activityType", {}), "typeKey"),
        get_value(summary, "aerobicTrainingEffect"),
        get_value(summary, "anaerobicTrainingEffect"),
        round(get_value(summary, "recoveryTime", 0) / 60, 1),
        get_value(training_status, "trainingStatus"),
        ground_contact_time,
        vertical_oscillation,
        stride_length
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

    if activities_sheet.row_values(1) != ACTIVITY_HEADERS:
        activities_sheet.update(range_name="A1", values=[ACTIVITY_HEADERS])
        logger.info("Updated Activities headers")

    if daily_sheet.row_values(1) != DAILY_HEADERS:
        daily_sheet.update(range_name="A1", values=[DAILY_HEADERS])
        logger.info("Updated Daily headers")

    existing_activity_ids = get_existing_values(activities_sheet, 1)
    existing_dates = get_existing_values(daily_sheet, 0)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=SYNC_DAYS)
    
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    training_status = safe_call(garmin.get_training_status, end_str, default={})

    activities = safe_call(
        garmin.get_activities_by_date,
        start_str,
        end_str,
        default=[]
    )

    activity_rows = []
    for activity in activities:
        activity_id = str(activity.get("activityId"))
        if activity_id not in existing_activity_ids:
            activity_rows.append(
                build_activity_row(garmin, activity, training_status)
            )

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
