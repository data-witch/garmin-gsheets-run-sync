import os
import json
import logging
from garminconnect import Garmin
from google.oauth2.service_account import Credentials
import gspread
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

if os.path.exists('.env'):
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        print("Warning: python-dotenv not installed. Install with: pip install python-dotenv")

SYNC_DAYS = int(os.environ.get('SYNC_DAYS', 30))
TOKEN_DIR = os.path.expanduser(os.environ.get('GARMIN_TOKEN_DIR', '~/.garth'))

EXPECTED_HEADERS = [
    "Date", "Activity ID", "Activity Name", "Distance (km)", "Duration (min)", "Avg Pace (min/km)",
    "Avg HR", "Max HR", "Calories", "Avg Cadence", "Elevation Gain (m)", "Activity Type",
    "Steps", "Floors", "Intensity Minutes", "Stress", "Body Battery Max", "Body Battery Min",
    "HRV Avg", "HRV Status", "Respiration", "SpO2",
    "Total Sleep (min)", "Deep Sleep (min)", "Light Sleep (min)", "REM Sleep (min)", "Awake (min)",
    "Sleep Score", "Weight (kg)", "Body Fat (%)",
    "Blood Pressure Systolic", "Blood Pressure Diastolic",
    "Active Calories", "Resting Calories", "Resting HR",
    "VO2 Max Running", "VO2 Max Cycling", "Training Status",
    "Acute Training Load", "Chronic Training Load",
    "Fitness Age", "Menstrual Phase", "Menstrual Flow",
]


def format_duration(seconds):
    return round(seconds / 60, 2) if seconds else 0


def format_pace(distance_meters, duration_seconds):
    if not distance_meters or not duration_seconds:
        return 0
    distance_km = distance_meters / 1000
    return round((duration_seconds / distance_km) / 60, 2)


def seconds_to_minutes(seconds):
    return round(seconds / 60, 1) if seconds else 0


def safe_call(func, *args, default=None):
    try:
        result = func(*args)
        return result if result is not None else default
    except Exception as exc:
        logger.debug("API call %s failed: %s", getattr(func, '__name__', func), exc)
        return default


def connect_garmin(email, password):
    """Connect to Garmin, reusing saved tokens from ~/.garth when possible."""
    garmin = Garmin(email, password)
    if os.path.exists(TOKEN_DIR):
        try:
            garmin.login(tokenstore=TOKEN_DIR)
            logger.info("Connected to Garmin using saved tokens")
            return garmin
        except Exception as exc:
            logger.info("Saved tokens expired (%s), performing full login", exc)

    garmin.login()
    try:
        os.makedirs(TOKEN_DIR, exist_ok=True)
        if hasattr(garmin, 'garth') and hasattr(garmin.garth, 'dump'):
            garmin.garth.dump(TOKEN_DIR)
            logger.info("Saved Garmin tokens to %s", TOKEN_DIR)
    except Exception as exc:
        logger.warning("Could not save Garmin tokens: %s", exc)

    return garmin


def ensure_headers(sheet):
    try:
        current_headers = sheet.row_values(1)
        if not current_headers:
            sheet.update('A1', [EXPECTED_HEADERS], value_input_option='RAW')
            print("✅ Created sheet headers")
            return

        missing = [header for header in EXPECTED_HEADERS if header not in current_headers]
        if missing:
            updated_headers = current_headers + missing
            sheet.update('A1', [updated_headers], value_input_option='RAW')
            print(f"✅ Added missing headers: {', '.join(missing)}")
        elif current_headers[:len(EXPECTED_HEADERS)] != EXPECTED_HEADERS:
            sheet.update('A1', [EXPECTED_HEADERS], value_input_option='RAW')
            print("✅ Updated sheet headers")
    except Exception as exc:
        print(f"Warning: Could not update headers: {exc}")


def get_fitness_age(garmin):
    today = datetime.today().strftime('%Y-%m-%d')
    data = safe_call(garmin.get_fitnessage_data, today, default={}) or {}
    fitness_age = data.get('fitnessAge') or data.get('currentFitnessAge')
    if fitness_age is not None:
        return fitness_age

    max_metrics = safe_call(garmin.get_max_metrics, today, default={}) or {}
    return max_metrics.get('fitnessAge', '') or ''


def parse_blood_pressure(bp_data):
    if not bp_data:
        return '', ''

    all_readings = []
    for day_summary in bp_data.get('measurementSummaries', []):
        all_readings.extend(day_summary.get('measurements', []))

    if not all_readings:
        return '', ''

    sys_values = [reading['systolic'] for reading in all_readings if reading.get('systolic') is not None]
    dia_values = [reading['diastolic'] for reading in all_readings if reading.get('diastolic') is not None]

    systolic = round(sum(sys_values) / len(sys_values)) if sys_values else ''
    diastolic = round(sum(dia_values) / len(dia_values)) if dia_values else ''
    return systolic, diastolic


def parse_training_status(training_status):
    vo2max_running = ''
    vo2max_cycling = ''
    training_status_phrase = ''
    acute_training_load = ''
    chronic_training_load = ''

    if not training_status:
        return vo2max_running, vo2max_cycling, training_status_phrase, acute_training_load, chronic_training_load

    most_recent_vo2max = training_status.get('mostRecentVO2Max') or {}
    generic_vo2max = most_recent_vo2max.get('generic') or {}
    cycling_vo2max = most_recent_vo2max.get('cycling') or {}
    vo2max_running = generic_vo2max.get('vo2MaxValue', '') or ''
    vo2max_cycling = cycling_vo2max.get('vo2MaxValue', '') or ''

    most_recent_training_status = training_status.get('mostRecentTrainingStatus') or {}
    latest_training_status_data = most_recent_training_status.get('latestTrainingStatusData') or {}
    for device_entry in latest_training_status_data.values():
        training_status_phrase = device_entry.get('trainingStatusFeedbackPhrase', '') or ''
        acute_dto = device_entry.get('acuteTrainingLoadDTO') or {}
        acute_training_load = acute_dto.get('dailyTrainingLoadAcute', '') or ''
        chronic_training_load = acute_dto.get('dailyTrainingLoadChronic', '') or ''
        break

    return vo2max_running, vo2max_cycling, training_status_phrase, acute_training_load, chronic_training_load


def get_daily_metrics(garmin, date_str, fitness_age):
    metrics = {
        'steps': 0,
        'floors': 0,
        'intensity_minutes': 0,
        'stress': 0,
        'body_battery_max': 0,
        'body_battery_min': 0,
        'hrv_avg': 0,
        'hrv_status': '',
        'respiration': 0,
        'spo2': 0,
        'total_sleep_min': 0,
        'deep_sleep_min': 0,
        'light_sleep_min': 0,
        'rem_sleep_min': 0,
        'awake_min': 0,
        'sleep_score': '',
        'weight': '',
        'body_fat': '',
        'bp_systolic': '',
        'bp_diastolic': '',
        'active_calories': 0,
        'resting_calories': 0,
        'resting_hr': 0,
        'vo2max_running': '',
        'vo2max_cycling': '',
        'training_status': '',
        'acute_training_load': '',
        'chronic_training_load': '',
        'menstrual_phase': '',
        'menstrual_flow': '',
        'fitness_age': fitness_age,
    }

    summary = safe_call(garmin.get_user_summary, date_str, default={}) or {}
    stats_body = safe_call(garmin.get_stats_and_body, date_str, default={}) or {}
    sleep_data = safe_call(garmin.get_sleep_data, date_str, default={}) or {}
    training_status = safe_call(garmin.get_training_status, date_str, default={}) or {}
    hrv_payload = safe_call(garmin.get_hrv_data, date_str, default={}) or {}
    bp_data = safe_call(garmin.get_blood_pressure, date_str, date_str, default={}) or {}

    metrics['steps'] = summary.get('totalSteps') or 0
    metrics['floors'] = summary.get('floorsAscended') or 0
    metrics['intensity_minutes'] = (
        (summary.get('moderateIntensityMinutes') or 0)
        + 2 * (summary.get('vigorousIntensityMinutes') or 0)
    )
    metrics['stress'] = summary.get('averageStressLevel') or 0
    metrics['body_battery_max'] = summary.get('bodyBatteryHighestValue') or 0
    metrics['body_battery_min'] = summary.get('bodyBatteryLowestValue') or 0
    metrics['active_calories'] = summary.get('activeKilocalories') or 0
    metrics['resting_calories'] = summary.get('bmrKilocalories') or 0
    metrics['resting_hr'] = summary.get('restingHeartRate') or 0

    if stats_body.get('weight'):
        metrics['weight'] = round(stats_body['weight'] / 1000, 2)
    metrics['body_fat'] = stats_body.get('bodyFat') or ''

    metrics['bp_systolic'], metrics['bp_diastolic'] = parse_blood_pressure(bp_data)

    sleep_dto = sleep_data.get('dailySleepDTO', {}) if isinstance(sleep_data, dict) else {}
    sleep_scores = sleep_dto.get('sleepScores') or {}
    metrics['sleep_score'] = sleep_scores.get('overall', {}).get('value', '') or ''
    metrics['total_sleep_min'] = seconds_to_minutes(sleep_dto.get('sleepTimeSeconds', 0))
    metrics['deep_sleep_min'] = seconds_to_minutes(sleep_dto.get('deepSleepSeconds', 0))
    metrics['light_sleep_min'] = seconds_to_minutes(sleep_dto.get('lightSleepSeconds', 0))
    metrics['rem_sleep_min'] = seconds_to_minutes(sleep_dto.get('remSleepSeconds', 0))
    metrics['awake_min'] = seconds_to_minutes(sleep_dto.get('awakeSleepSeconds', 0))

    hrv_summary = (hrv_payload or {}).get('hrvSummary') or {}
    metrics['hrv_avg'] = hrv_summary.get('lastNightAvg') or 0
    metrics['hrv_status'] = hrv_summary.get('status') or ''

    (
        metrics['vo2max_running'],
        metrics['vo2max_cycling'],
        metrics['training_status'],
        metrics['acute_training_load'],
        metrics['chronic_training_load'],
    ) = parse_training_status(training_status)

    respiration = safe_call(garmin.get_respiration_data, date_str, default={}) or {}
    metrics['respiration'] = (
        respiration.get('avgWakingRespirationValue')
        or respiration.get('avgSleepRespirationValue')
        or respiration.get('avgWakingRespiration')
        or 0
    ) or 0

    spo2 = safe_call(garmin.get_spo2_data, date_str, default={}) or {}
    metrics['spo2'] = (
        spo2.get('averageSpO2')
        or spo2.get('avgSleepSpO2')
        or spo2.get('lowestSpO2')
        or 0
    ) or 0

    menstrual = safe_call(garmin.get_menstrual_data_for_date, date_str, default={}) or {}
    metrics['menstrual_phase'] = (
        menstrual.get('phase')
        or menstrual.get('menstrualPhase')
        or menstrual.get('phaseType')
        or ''
    )
    metrics['menstrual_flow'] = (
        menstrual.get('flowLevel')
        or menstrual.get('flow')
        or menstrual.get('flowType')
        or ''
    )

    return metrics


def get_avg_cadence(activity):
    return (
        activity.get('averageRunningCadenceInStepsPerMinute')
        or activity.get('averageBikingCadenceInRevPerMinute')
        or activity.get('averageCadence')
        or 0
    ) or 0


def build_activity_row(activity, daily_metrics):
    activity_date = activity.get('startTimeLocal', '')[:10]
    activity_name = activity.get('activityName', 'Activity')
    activity_id = activity.get('activityId', '')
    distance_meters = activity.get('distance', 0) or 0
    distance_km = round(distance_meters / 1000, 2) if distance_meters else 0
    duration_seconds = activity.get('duration', 0) or 0
    activity_type = activity.get('activityType', {}).get('typeKey', '')

    return [
        activity_date,
        activity_id,
        activity_name,
        distance_km,
        format_duration(duration_seconds),
        format_pace(distance_meters, duration_seconds),
        activity.get('averageHR', 0) or 0,
        activity.get('maxHR', 0) or 0,
        activity.get('calories', 0) or 0,
        get_avg_cadence(activity),
        round(activity.get('elevationGain', 0), 1) if activity.get('elevationGain') else 0,
        activity_type,
        daily_metrics['steps'],
        daily_metrics['floors'],
        daily_metrics['intensity_minutes'],
        daily_metrics['stress'],
        daily_metrics['body_battery_max'],
        daily_metrics['body_battery_min'],
        daily_metrics['hrv_avg'],
        daily_metrics['hrv_status'],
        daily_metrics['respiration'],
        daily_metrics['spo2'],
        daily_metrics['total_sleep_min'],
        daily_metrics['deep_sleep_min'],
        daily_metrics['light_sleep_min'],
        daily_metrics['rem_sleep_min'],
        daily_metrics['awake_min'],
        daily_metrics['sleep_score'],
        daily_metrics['weight'],
        daily_metrics['body_fat'],
        daily_metrics['bp_systolic'],
        daily_metrics['bp_diastolic'],
        daily_metrics['active_calories'],
        daily_metrics['resting_calories'],
        daily_metrics['resting_hr'],
        daily_metrics['vo2max_running'],
        daily_metrics['vo2max_cycling'],
        daily_metrics['training_status'],
        daily_metrics['acute_training_load'],
        daily_metrics['chronic_training_load'],
        daily_metrics['fitness_age'],
        daily_metrics['menstrual_phase'],
        daily_metrics['menstrual_flow'],
    ]


def main():
    print(f"Starting Garmin sync (last {SYNC_DAYS} days)...")

    garmin_email = os.environ.get('GARMIN_EMAIL')
    garmin_password = os.environ.get('GARMIN_PASSWORD')
    google_creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    sheet_id = os.environ.get('SHEET_ID')

    if not google_creds_json and os.path.exists('credentials.json'):
        print("Loading Google credentials from credentials.json...")
        with open('credentials.json', 'r') as f:
            google_creds_json = f.read()

    if not all([garmin_email, garmin_password, google_creds_json, sheet_id]):
        print("❌ Missing required environment variables")
        print(f"   GARMIN_EMAIL: {'✓' if garmin_email else '✗'}")
        print(f"   GARMIN_PASSWORD: {'✓' if garmin_password else '✗'}")
        print(f"   GOOGLE_CREDENTIALS: {'✓' if google_creds_json else '✗'}")
        print(f"   SHEET_ID: {'✓' if sheet_id else '✗'}")
        return

    print("Connecting to Garmin...")
    try:
        garmin = connect_garmin(garmin_email, garmin_password)
        print("✅ Connected to Garmin")
    except Exception as exc:
        print(f"❌ Failed to connect to Garmin: {exc}")
        print("If MFA is enabled, run login once locally — tokens will be saved to ~/.garth")
        return

    fitness_age = get_fitness_age(garmin)
    if fitness_age:
        print(f"Fitness Age: {fitness_age}")

    end_date = datetime.today()
    start_date = end_date - timedelta(days=SYNC_DAYS)
    start_str = start_date.strftime('%Y-%m-%d')
    end_str = end_date.strftime('%Y-%m-%d')

    print(f"Fetching activities from {start_str} to {end_str}...")
    try:
        activities = garmin.get_activities_by_date(start_str, end_str) or []
        print(f"Found {len(activities)} activities")
    except Exception as exc:
        print(f"❌ Failed to fetch activities: {exc}")
        return

    if not activities:
        print("No activities found in the selected period")
        return

    print("Connecting to Google Sheets...")
    try:
        creds_dict = json.loads(google_creds_json)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=[
                'https://www.googleapis.com/auth/spreadsheets',
                'https://www.googleapis.com/auth/drive',
            ],
        )
        client = gspread.authorize(creds)
        sheet = client.open_by_key(sheet_id).sheet1
        print("✅ Connected to Google Sheets")
    except Exception as exc:
        print(f"❌ Failed to connect to Google Sheets: {exc}")
        return

    ensure_headers(sheet)

    existing_keys = set()
    try:
        existing_data = sheet.get_all_values()
        if len(existing_data) > 1:
            for row in existing_data[1:]:
                if not row or not row[0]:
                    continue
                activity_id = row[1] if len(row) > 1 else ''
                activity_name = row[2] if len(row) > 2 else (row[1] if len(row) > 1 else '')
                if activity_id:
                    existing_keys.add((row[0], str(activity_id)))
                else:
                    existing_keys.add((row[0], activity_name))
        print(f"Found {len(existing_keys)} existing entries")
    except Exception as exc:
        print(f"Warning: Could not check existing data: {exc}")

    daily_cache = {}
    new_entries = 0

    for activity in activities:
        try:
            activity_date = activity.get('startTimeLocal', '')[:10]
            activity_name = activity.get('activityName', 'Activity')
            activity_id = str(activity.get('activityId', ''))
            row_key = (activity_date, activity_id) if activity_id else (activity_date, activity_name)

            if row_key in existing_keys:
                print(f"Skipping {activity_date} - {activity_name} (already exists)")
                continue

            if activity_date not in daily_cache:
                print(f"Fetching daily metrics for {activity_date}...")
                daily_cache[activity_date] = get_daily_metrics(garmin, activity_date, fitness_age)

            row = build_activity_row(activity, daily_cache[activity_date])
            sheet.append_row(row, value_input_option='USER_ENTERED')
            print(f"✅ Added: {activity_date} - {activity_name}")
            new_entries += 1
            existing_keys.add(row_key)

        except Exception as exc:
            print(f"❌ Error processing activity: {exc}")
            continue

    if new_entries > 0:
        print(f"\n🎉 Successfully added {new_entries} new activities!")
    else:
        print("\n✓ No new activities to add")


if __name__ == "__main__":
    main()
