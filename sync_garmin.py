
import os
import json
import logging
from datetime import datetime, timedelta

import gspread
from garminconnect import Garmin
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

SYNC_DAYS = int(os.environ.get("SYNC_DAYS", 100))
TOKEN_DIR = os.path.expanduser(os.environ.get("GARMIN_TOKEN_DIR", "~/.garth"))

ACTIVITY_HEADERS = [
    "Дата", "ID активности", "Название активности", "Дистанция (км)",
    "Длительность (мин)", "Средний темп (мин/км)", "Ср. ЧСС",
    "Макс. ЧСС", "Калории", "Ср. Каденс", "Набор высоты (м)",
    "Тип активности"
]

DAILY_HEADERS = [
    "Дата", "Шаги", "Этажи", "Стресс", "Body Battery Макс",
    "Body Battery Мин", "HRV Ср.", "HRV Статус", "Дыхание",
    "SpO2", "Сон Всего (мин)", "Оценка сна", "ЧСС покоя",
    "VO2 Max Бег", "VO2 Max Вело", "Статус тренировки",
    "Острая нагрузка", "Хроническая нагрузка",
    "Фитнес-возраст", "Фаза цикла"
]


def safe_call(func, *args, default=None):
    try:
        result = func(*args)
        return default if result is None else result
    except Exception as e:
        logger.warning("Garmin API error %s: %s", func.__name__, e)
        return default


def connect_garmin(email, password):
    garmin = Garmin(email, password)

    if os.path.exists(TOKEN_DIR):
        try:
            garmin.login(tokenstore=TOKEN_DIR)
            return garmin
        except Exception:
            pass

    garmin.login()

    try:
        os.makedirs(TOKEN_DIR, exist_ok=True)
        garmin.garth.dump(TOKEN_DIR)
    except Exception:
        pass

    return garmin


def seconds_to_minutes(seconds):
    return round(seconds / 60, 1) if seconds else 0


def get_existing_activity_ids(sheet):
    values = sheet.get_all_values()
    return {str(row[1]) for row in values[1:] if len(row) > 1 and row[1]}


def get_existing_dates(sheet):
    values = sheet.get_all_values()
    return {row[0] for row in values[1:] if row and row[0]}


def build_activity_row(activity):
    distance = activity.get("distance", 0) or 0
    duration = activity.get("duration", 0) or 0

    pace = 0
    if distance:
        pace = round((duration / (distance / 1000)) / 60, 2)

    return [
        activity.get("startTimeLocal", "")[:10],
        str(activity.get("activityId", "")),
        activity.get("activityName", ""),
        round(distance / 1000, 2),
        round(duration / 60, 2),
        pace,
        activity.get("averageHR", 0),
        activity.get("maxHR", 0),
        activity.get("calories", 0),
        activity.get("averageCadence", 0),
        activity.get("elevationGain", 0),
        activity.get("activityType", {}).get("typeKey", "")
    ]


def get_daily_metrics(garmin, date_str):
    summary = safe_call(garmin.get_user_summary, date_str, default={}) or {}
    sleep = safe_call(garmin.get_sleep_data, date_str, default={}) or {}
    hrv = safe_call(garmin.get_hrv_data, date_str, default={}) or {}

    sleep_dto = sleep.get("dailySleepDTO", {})

    return [
        date_str,
        summary.get("totalSteps", 0),
        summary.get("floorsAscended", 0),
        summary.get("averageStressLevel", 0),
        summary.get("bodyBatteryHighestValue", 0),
        summary.get("bodyBatteryLowestValue", 0),
        hrv.get("hrvSummary", {}).get("lastNightAvg", 0),
        hrv.get("hrvSummary", {}).get("status", ""),
        0,
        0,
        seconds_to_minutes(sleep_dto.get("sleepTimeSeconds", 0)),
        sleep_dto.get("sleepScores", {}).get("overall", {}).get("value", ""),
        summary.get("restingHeartRate", 0),
        "",
        "",
        "",
        "",
        "",
        "",
        ""
    ]


def main():
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    google_creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    sheet_id = os.environ.get("SHEET_ID")

    if not all([email, password, google_creds_json, sheet_id]):
        raise Exception("Missing environment variables")

    garmin = connect_garmin(email, password)

    creds = Credentials.from_service_account_info(
        json.loads(google_creds_json),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
    )

    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(sheet_id)

    activity_sheet = spreadsheet.worksheet("Лист1")
    daily_sheet = spreadsheet.worksheet("Лист2")

    existing_activity_ids = get_existing_activity_ids(activity_sheet)
    existing_dates = get_existing_dates(daily_sheet)

    end = datetime.today()
    start = end - timedelta(days=SYNC_DAYS)

    activities = garmin.get_activities_by_date(
        start.strftime("%Y-%m-%d"),
        end.strftime("%Y-%m-%d")
    )

    activity_rows = []

    for activity in sorted(
        activities,
        key=lambda x: x.get("startTimeLocal", "")
    ):
        activity_id = str(activity.get("activityId", ""))

        if activity_id not in existing_activity_ids:
            activity_rows.append(build_activity_row(activity))

    if activity_rows:
        activity_sheet.append_rows(
            activity_rows,
            value_input_option="USER_ENTERED"
        )

    daily_rows = []
    current = start

    while current <= end:
        date_str = current.strftime("%Y-%m-%d")

        if date_str not in existing_dates:
            daily_rows.append(get_daily_metrics(garmin, date_str))

        current += timedelta(days=1)

    if daily_rows:
        daily_sheet.append_rows(
            daily_rows,
            value_input_option="USER_ENTERED"
        )

    logger.info("Синхронизация завершена")


if __name__ == "__main__":
    main()
