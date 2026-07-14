import os
import json
from garminconnect import Garmin
from google.oauth2.service_account import Credentials
import gspread
from datetime import datetime, timedelta

# Load environment variables from .env file if it exists (for local testing)
if os.path.exists('.env'):
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        print("Warning: python-dotenv not installed. Install with: pip install python-dotenv")

SYNC_DAYS = int(os.environ.get('SYNC_DAYS', 30))

EXPECTED_HEADERS = [
    "Date", "Activity Name", "Distance (km)", "Duration (min)", "Avg Pace (min/km)",
    "Avg HR", "Max HR", "Calories", "Avg Cadence", "Elevation Gain (m)", "Activity Type",
    "Steps", "Floors", "Intensity Minutes", "Stress", "Body Battery",
    "HRV Avg", "HRV Status", "Respiration", "SpO2",
    "Total Sleep (min)", "Deep Sleep (min)", "Light Sleep (min)", "REM Sleep (min)", "Awake (min)",
    "Fitness Age", "Menstrual Phase", "Menstrual Flow",
]


def format_duration(seconds):
    """Convert seconds to minutes (rounded to 2 decimals)."""
    return round(seconds / 60, 2) if seconds else 0


def format_pace(distance_meters, duration_seconds):
    """Calculate pace in min/km."""
    if not distance_meters or not duration_seconds:
        return 0
    distance_km = distance_meters / 1000
    pace_seconds = duration_seconds / distance_km
    return round(pace_seconds / 60, 2)


def seconds_to_minutes(seconds):
    """Convert seconds to minutes (rounded to 1 decimal)."""
    return round(seconds / 60, 1) if seconds else 0


def safe_call(func, *args, default=None):
    """Call Garmin API method and return default on any error."""
    try:
        result = func(*args)
        return result if result is not None else default
    except Exception:
        return default


def ensure_headers(sheet):
    """Ensure sheet has all required column headers."""
    try:
        current_headers = sheet.row_values(1)
        if current_headers != EXPECTED_HEADERS:
            sheet.update('A1', [EXPECTED_HEADERS], value_input_option='RAW')
            print("✅ Updated sheet headers")
    except Exception as e:
        print(f"Warning: Could not update headers: {e}")


def get_fitness_age(garmin):
    """Fetch fitness age once on login."""
    today = datetime.today().strftime('%Y-%m-%d')
    data = safe_call(garmin.get_fitnessage_data, today, default={}) or {}
    fitness_age = data.get('fitnessAge') or data.get('currentFitnessAge')
    if fitness_age is not None:
        return fitness_age

    max_metrics = safe_call(garmin.get_max_metrics, today, default={}) or {}
    return max_metrics.get('fitnessAge', '') or ''


def get_daily_metrics(garmin, date_str, fitness_age):
    """Fetch daily health metrics for a given date."""
    metrics = {
        'steps': 0,
        'floors': 0,
        'intensity_minutes': 0,
        'stress': 0,
        'body_battery': 0,
        'hrv_avg': 0,
        'hrv_status': '',
        'respiration': 0,
        'spo2': 0,
        'total_sleep_min': 0,
        'deep_sleep_min': 0,
        'light_sleep_min': 0,
        'rem_sleep_min': 0,
        'awake_min': 0,
        'menstrual_phase': '',
        'menstrual_flow': '',
        'fitness_age': fitness_age,
    }

    summary = safe_call(garmin.get_stats, date_str, default={}) or {}
    metrics['steps'] = summary.get('totalSteps', 0) or 0
    metrics['floors'] = summary.get('floorsAscended', 0) or 0

    intensity = safe_call(garmin.get_intensity_minutes_data, date_str, default={}) or {}
    moderate = intensity.get('moderateMinutes', 0) or 0
    vigorous = intensity.get('vigorousMinutes', 0) or 0
    metrics['intensity_minutes'] = moderate + vigorous
    if not metrics['intensity_minutes']:
        metrics['intensity_minutes'] = (
            (summary.get('moderateIntensityMinutes', 0) or 0)
            + (summary.get('vigorousIntensityMinutes', 0) or 0)
        )

    stress = safe_call(garmin.get_all_day_stress, date_str, default={}) or {}
    if not stress:
        stress = safe_call(garmin.get_stress_data, date_str, default={}) or {}
    metrics['stress'] = stress.get('avgStressLevel', 0) or 0

    body_battery_data = safe_call(garmin.get_body_battery, date_str, date_str, default=[]) or []
    for entry in body_battery_data:
        if entry.get('calendarDate') == date_str:
            metrics['body_battery'] = (
                entry.get('highestBodyBatteryValue')
                or entry.get('bodyBatteryHighestValue')
                or entry.get('charged')
                or 0
            )
            break
    if not metrics['body_battery']:
        metrics['body_battery'] = summary.get('bodyBatteryHighestValue', 0) or 0

    hrv = safe_call(garmin.get_hrv_data, date_str, default={}) or {}
    hrv_summary = hrv.get('hrvSummary', hrv) if isinstance(hrv, dict) else {}
    metrics['hrv_avg'] = hrv_summary.get('lastNightAvg', 0) or hrv_summary.get('weeklyAvg', 0) or 0
    metrics['hrv_status'] = hrv_summary.get('status', '') or hrv_summary.get('hrvStatus', '') or ''

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

    sleep = safe_call(garmin.get_sleep_data, date_str, default={}) or {}
    sleep_dto = sleep.get('dailySleepDTO', {}) if isinstance(sleep, dict) else {}
    metrics['total_sleep_min'] = seconds_to_minutes(sleep_dto.get('sleepTimeSeconds', 0))
    metrics['deep_sleep_min'] = seconds_to_minutes(sleep_dto.get('deepSleepSeconds', 0))
    metrics['light_sleep_min'] = seconds_to_minutes(sleep_dto.get('lightSleepSeconds', 0))
    metrics['rem_sleep_min'] = seconds_to_minutes(sleep_dto.get('remSleepSeconds', 0))
    metrics['awake_min'] = seconds_to_minutes(sleep_dto.get('awakeSleepSeconds', 0))

    menstrual = safe_call(garmin.get_menstrual_data_for_date, date_str, default={}) or {}
    metrics['menstrual_phase'] = (
        menstrual.get('phase', '')
        or menstrual.get('menstrualPhase', '')
        or menstrual.get('phaseType', '')
        or ''
    )
    metrics['menstrual_flow'] = (
        menstrual.get('flowLevel', '')
        or menstrual.get('flow', '')
        or menstrual.get('flowType', '')
        or ''
    )

    return metrics


def get_avg_cadence(activity):
    """Extract cadence from activity, supporting different activity types."""
    return (
        activity.get('averageRunningCadenceInStepsPerMinute')
        or activity.get('averageBikingCadenceInRevPerMinute')
        or activity.get('averageCadence')
        or 0
    ) or 0


def build_activity_row(activity, daily_metrics):
    """Build a sheet row from activity and daily metrics."""
    activity_date = activity.get('startTimeLocal', '')[:10]
    activity_name = activity.get('activityName', 'Activity')
    distance_meters = activity.get('distance', 0) or 0
    distance_km = round(distance_meters / 1000, 2) if distance_meters else 0
    duration_seconds = activity.get('duration', 0) or 0
    activity_type = activity.get('activityType', {}).get('typeKey', '')

    return [
        activity_date,
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
        daily_metrics['body_battery'],
        daily_metrics['hrv_avg'],
        daily_metrics['hrv_status'],
        daily_metrics['respiration'],
        daily_metrics['spo2'],
        daily_metrics['total_sleep_min'],
        daily_metrics['deep_sleep_min'],
        daily_metrics['light_sleep_min'],
        daily_metrics['rem_sleep_min'],
        daily_metrics['awake_min'],
        daily_metrics['fitness_age'],
        daily_metrics['menstrual_phase'],
        daily_metrics['menstrual_flow'],
    ]


def main():
    print(f"Starting Garmin activities sync (last {SYNC_DAYS} days)...")

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
        garmin = Garmin(garmin_email, garmin_password)
        garmin.login()
        print("✅ Connected to Garmin")
    except Exception as e:
        print(f"❌ Failed to connect to Garmin: {e}")
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
        activities = garmin.get_activities_by_date(start_str, end_str)
        print(f"Found {len(activities)} activities")
    except Exception as e:
        print(f"❌ Failed to fetch activities: {e}")
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
    except Exception as e:
        print(f"❌ Failed to connect to Google Sheets: {e}")
        return

    ensure_headers(sheet)

    existing_keys = set()
    try:
        existing_data = sheet.get_all_values()
        if len(existing_data) > 1:
            for row in existing_data[1:]:
                if row and row[0]:
                    activity_name = row[1] if len(row) > 1 else ''
                    existing_keys.add((row[0], activity_name))
        print(f"Found {len(existing_keys)} existing entries")
    except Exception as e:
        print(f"Warning: Could not check existing data: {e}")

    daily_cache = {}
    new_entries = 0

    for activity in activities:
        try:
            activity_date = activity.get('startTimeLocal', '')[:10]
            activity_name = activity.get('activityName', 'Activity')
            row_key = (activity_date, activity_name)

            if row_key in existing_keys:
                print(f"Skipping {activity_date} - {activity_name} (already exists)")
                continue

            if activity_date not in daily_cache:
                daily_cache[activity_date] = get_daily_metrics(garmin, activity_date, fitness_age)

            row = build_activity_row(activity, daily_cache[activity_date])
            sheet.append_row(row, value_input_option='USER_ENTERED')
            print(f"✅ Added: {activity_date} - {activity_name}")
            new_entries += 1
            existing_keys.add(row_key)

        except Exception as e:
            print(f"❌ Error processing activity: {e}")
            continue

    if new_entries > 0:
        print(f"\n🎉 Successfully added {new_entries} new activities!")
    else:
        print("\n✓ No new activities to add")


if __name__ == "__main__":
    main()
