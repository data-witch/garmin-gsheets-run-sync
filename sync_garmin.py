# import os
# import json
# from garminconnect import Garmin
# from google.oauth2.service_account import Credentials
# import gspread
# from datetime import datetime, timedelta

# # Load environment variables from .env file if it exists (for local testing)
# if os.path.exists('.env'):
#     try:
#         from dotenv import load_dotenv
#         load_dotenv()
#     except ImportError:
#         print("Warning: python-dotenv not installed. Install with: pip install python-dotenv")
#         pass

# def format_duration(seconds):
#     """Convert seconds to minutes (rounded to 2 decimals)"""
#     return round(seconds / 60, 2) if seconds else 0

# def format_pace(distance_meters, duration_seconds):
#     """Calculate pace in min/km"""
#     if not distance_meters or not duration_seconds:
#         return 0
#     distance_km = distance_meters / 1000
#     pace_seconds = duration_seconds / distance_km
#     return round(pace_seconds / 60, 2)  # Convert to min/km

# def main():
#     print("Starting Garmin running activities sync...")
    
#     # Get credentials from environment variables
#     garmin_email = os.environ.get('GARMIN_EMAIL')
#     garmin_password = os.environ.get('GARMIN_PASSWORD')
#     google_creds_json = os.environ.get('GOOGLE_CREDENTIALS')
#     sheet_id = os.environ.get('SHEET_ID')  # Add sheet ID from environment
    
#     # For local testing: try to load from credentials.json file
#     if not google_creds_json and os.path.exists('credentials.json'):
#         print("Loading Google credentials from credentials.json...")
#         with open('credentials.json', 'r') as f:
#             google_creds_json = f.read()
    
#     if not all([garmin_email, garmin_password, google_creds_json, sheet_id]):
#         print("❌ Missing required environment variables")
#         print(f"   GARMIN_EMAIL: {'✓' if garmin_email else '✗'}")
#         print(f"   GARMIN_PASSWORD: {'✓' if garmin_password else '✗'}")
#         print(f"   GOOGLE_CREDENTIALS: {'✓' if google_creds_json else '✗'}")
#         print(f"   SHEET_ID: {'✓' if sheet_id else '✗'}")
#         return
    
#     # Connect to Garmin
#     print("Connecting to Garmin...")
#     try:
#         garmin = Garmin(garmin_email, garmin_password)
#         garmin.login()
#         print("✅ Connected to Garmin")
#     except Exception as e:
#         print(f"❌ Failed to connect to Garmin: {e}")
#         return
    
#     # Get recent activities (last 7 days)
#     print("Fetching recent activities...")
#     try:
#         activities = garmin.get_activities(0, 20)  # Get last 20 activities
#         print(f"Found {len(activities)} total activities")
#     except Exception as e:
#         print(f"❌ Failed to fetch activities: {e}")
#         return
    
#     # Filter for running activities only
#     running_activities = [
#         activity for activity in activities 
#         if activity.get('activityType', {}).get('typeKey', '').lower() in ['running', 'treadmill_running', 'trail_running']
#     ]
    
#     print(f"Found {len(running_activities)} running activities")
    
#     if not running_activities:
#         print("No running activities found in recent data")
#         return
    
#     # Connect to Google Sheets
#     print("Connecting to Google Sheets...")
#     try:
#         creds_dict = json.loads(google_creds_json)
#         creds = Credentials.from_service_account_info(
#             creds_dict,
#             scopes=[
#                 'https://www.googleapis.com/auth/spreadsheets',
#                 'https://www.googleapis.com/auth/drive'
#             ]
#         )
#         client = gspread.authorize(creds)
#         sheet = client.open("Garmin Data").sheet1
#         print("✅ Connected to Google Sheets")
#     except Exception as e:
#         print(f"❌ Failed to connect to Google Sheets: {e}")
#         return
    
#     # Get existing dates to avoid duplicates
#     try:
#         existing_data = sheet.get_all_values()
#         existing_dates = set()
#         if len(existing_data) > 1:  # If there's data beyond headers
#             for row in existing_data[1:]:  # Skip header row
#                 if row and row[0]:  # If date column exists
#                     existing_dates.add(row[0])
#         print(f"Found {len(existing_dates)} existing entries")
#     except Exception as e:
#         print(f"Warning: Could not check existing data: {e}")
#         existing_dates = set()
    
#     # Process each running activity
#     new_entries = 0
#     for activity in running_activities:
#         try:
#             # Parse activity date
#             activity_date = activity.get('startTimeLocal', '')[:10]  # Get YYYY-MM-DD
            
#             # Skip if already in sheet
#             if activity_date in existing_dates:
#                 print(f"Skipping {activity_date} - already exists")
#                 continue
            
#             # Extract metrics
#             activity_name = activity.get('activityName', 'Run')
#             distance_meters = activity.get('distance', 0)
#             distance_km = round(distance_meters / 1000, 2) if distance_meters else 0
#             duration_seconds = activity.get('duration', 0)
#             duration_min = format_duration(duration_seconds)
#             avg_pace = format_pace(distance_meters, duration_seconds)
#             avg_hr = activity.get('averageHR', 0) or 0
#             max_hr = activity.get('maxHR', 0) or 0
#             calories = activity.get('calories', 0) or 0
#             avg_cadence = activity.get('averageRunningCadenceInStepsPerMinute', 0) or 0
#             elevation_gain = round(activity.get('elevationGain', 0), 1) if activity.get('elevationGain') else 0
#             activity_type = activity.get('activityType', {}).get('typeKey', 'running')
            
#             # Prepare row
#             row = [
#                 activity_date,
#                 activity_name,
#                 distance_km,
#                 duration_min,
#                 avg_pace,
#                 avg_hr,
#                 max_hr,
#                 calories,
#                 avg_cadence,
#                 elevation_gain,
#                 activity_type
#             ]
            
#             # Append to sheet
#             sheet.append_row(row)
#             print(f"✅ Added: {activity_date} - {activity_name} ({distance_km} km)")
#             new_entries += 1
            
#         except Exception as e:
#             print(f"❌ Error processing activity: {e}")
#             continue
    
#     if new_entries > 0:
#         print(f"\n🎉 Successfully added {new_entries} new running activities!")
#     else:
#         print("\n✓ No new activities to add")

# if __name__ == "__main__":
#     main()

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
        pass

def format_duration(seconds):
    """Convert seconds to minutes (rounded to 2 decimals)"""
    return round(seconds / 60, 2) if seconds else 0

def format_pace(distance_meters, duration_seconds):
    """Calculate pace in min/km"""
    if not distance_meters or not duration_seconds:
        return 0
    distance_km = distance_meters / 1000
    pace_seconds = duration_seconds / distance_km
    return round(pace_seconds / 60, 2)  # Convert to min/km

def safe_get(data, key, default=0):
    """Safely get value from dict, return default if None"""
    val = data.get(key)
    return val if val is not None else default

def get_daily_metrics(garmin, date):
    """Get all daily health metrics for a given date"""
    metrics = {
        'steps': 0,
        'floors': 0,
        'intensity_minutes': 0,
        'stress': 0,
        'body_battery': 0,
        'hrv_avg': 0,
        'hrv_status': '',
        'respiration': 0,
        'spo2': 0
    }
    
    try:
        wellness = garmin.get_wellness_data(date)
        if wellness:
            metrics['steps'] = safe_get(wellness, 'totalSteps')
            metrics['floors'] = safe_get(wellness, 'totalFloors')
            metrics['intensity_minutes'] = safe_get(wellness, 'intensityMinutes')
            metrics['respiration'] = safe_get(wellness, 'respirationRate')
    except Exception as e:
        print(f"   ⚠️  Could not get wellness data: {e}")
    
    try:
        stress_data = garmin.get_stress_data(date)
        if stress_data:
            metrics['stress'] = safe_get(stress_data, 'stressLevel')
    except Exception as e:
        print(f"   ⚠️  Could not get stress data: {e}")
    
    try:
        battery_data = garmin.get_body_battery_data(date, date)
        if battery_data and len(battery_data) > 0:
            metrics['body_battery'] = safe_get(battery_data[0], 'value')
    except Exception as e:
        print(f"   ⚠️  Could not get body battery: {e}")
    
    try:
        hrv_data = garmin.get_hrv_data(date)
        if hrv_data:
            metrics['hrv_avg'] = safe_get(hrv_data, 'avgHRV')
            metrics['hrv_status'] = safe_get(hrv_data, 'status', '')
    except Exception as e:
        print(f"   ⚠️  Could not get HRV data: {e}")
    
    try:
        spo2_data = garmin.get_spo2_data(date)
        if spo2_data and len(spo2_data) > 0:
            readings = [reading.get('value', 0) for reading in spo2_data if reading.get('value')]
            if readings:
                metrics['spo2'] = round(sum(readings) / len(readings))
    except Exception as e:
        print(f"   ⚠️  Could not get SpO2 data: {e}")
    
    return metrics

def get_sleep_data(garmin, date):
    """Get sleep metrics for a given date"""
    sleep_info = {
        'total_sleep_min': 0,
        'deep_sleep_min': 0,
        'light_sleep_min': 0,
        'rem_sleep_min': 0,
        'awake_min': 0
    }
    
    try:
        sleep_data = garmin.get_sleep_data(date)
        if sleep_data and 'dailySleepDTO' in sleep_data:
            dto = sleep_data['dailySleepDTO']
            sleep_info['total_sleep_min'] = safe_get(dto, 'sleepTimeSeconds', 0) // 60
            sleep_info['deep_sleep_min'] = safe_get(dto, 'deepSleepSeconds', 0) // 60
            sleep_info['light_sleep_min'] = safe_get(dto, 'lightSleepSeconds', 0) // 60
            sleep_info['rem_sleep_min'] = safe_get(dto, 'remSleepSeconds', 0) // 60
            sleep_info['awake_min'] = safe_get(dto, 'awakeTimeSeconds', 0) // 60
    except Exception as e:
        print(f"   ⚠️  Could not get sleep data: {e}")
    
    return sleep_info

def get_fitness_age(garmin):
    """Get fitness age (fetched once)"""
    try:
        user_settings = garmin.get_user_settings()
        if user_settings:
            return safe_get(user_settings, 'fitnessAge', '')
    except Exception as e:
        print(f"   ⚠️  Could not get fitness age: {e}")
    return ''

def get_menstrual_cycle(garmin, date):
    """Get menstrual cycle data for a given date"""
    try:
        cycle_data = garmin.get_menstrual_cycle()
        if cycle_data and 'cycleData' in cycle_data:
            for cycle in cycle_data['cycleData']:
                if cycle.get('date') == date:
                    return {
                        'phase': cycle.get('phase', ''),
                        'flow': cycle.get('flow', '')
                    }
    except Exception as e:
        print(f"   ⚠️  Could not get menstrual cycle data: {e}")
    return {'phase': '', 'flow': ''}

def update_sheet_headers(sheet):
    """Force update headers in Google Sheet"""
    headers = [
        'Date', 'Activity Name', 'Distance (km)', 'Duration (min)',
        'Avg Pace (min/km)', 'Avg HR', 'Max HR', 'Calories',
        'Avg Cadence', 'Elevation Gain (m)', 'Activity Type',
        'Steps', 'Floors', 'Intensity Minutes',
        'Stress', 'Body Battery', 'HRV Avg', 'HRV Status',
        'Respiration', 'SpO2',
        'Total Sleep (min)', 'Deep Sleep (min)', 'Light Sleep (min)',
        'REM Sleep (min)', 'Awake (min)',
        'Fitness Age',
        'Menstrual Phase', 'Menstrual Flow'
    ]
    
    try:
        # Try to update existing headers
        current_headers = sheet.row_values(1)
        print(f"Current headers: {len(current_headers)} columns")
        print(f"Expected headers: {len(headers)} columns")
        
        if len(current_headers) != len(headers):
            # Clear first row and rewrite all headers
            print("Updating headers...")
            # Write all headers in one go
            for i, header in enumerate(headers, start=1):
                sheet.update_cell(1, i, header)
            print(f"✅ Headers updated to {len(headers)} columns")
        else:
            print("✅ Headers already correct")
            
    except Exception as e:
        print(f"⚠️  Error updating headers: {e}")
        # Fallback: try to recreate header row
        try:
            sheet.insert_row(headers, 1)
            print("✅ Headers inserted successfully")
        except Exception as e2:
            print(f"❌ Failed to insert headers: {e2}")

def main():
    print("Starting Garmin comprehensive sync...")
    
    # Get credentials from environment variables
    garmin_email = os.environ.get('GARMIN_EMAIL')
    garmin_password = os.environ.get('GARMIN_PASSWORD')
    google_creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    sheet_id = os.environ.get('SHEET_ID')
    
    # For local testing: try to load from credentials.json file
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
    
    # Connect to Garmin
    print("Connecting to Garmin...")
    try:
        garmin = Garmin(garmin_email, garmin_password)
        garmin.login()
        print("✅ Connected to Garmin")
    except Exception as e:
        print(f"❌ Failed to connect to Garmin: {e}")
        return
    
    # Get fitness age once
    fitness_age = get_fitness_age(garmin)
    if fitness_age:
        print(f"   Fitness Age: {fitness_age}")
    
    # Connect to Google Sheets
    print("Connecting to Google Sheets...")
    try:
        creds_dict = json.loads(google_creds_json)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=[
                'https://www.googleapis.com/auth/spreadsheets',
                'https://www.googleapis.com/auth/drive'
            ]
        )
        client = gspread.authorize(creds)
        sheet = client.open("Garmin Data").sheet1
        print("✅ Connected to Google Sheets")
    except Exception as e:
        print(f"❌ Failed to connect to Google Sheets: {e}")
        return
    
    # FORCE UPDATE HEADERS - always run this
    print("\n📋 Updating sheet headers...")
    update_sheet_headers(sheet)
    
    # Get existing dates to avoid duplicates
    try:
        existing_data = sheet.get_all_values()
        existing_dates = set()
        if len(existing_data) > 1:
            for row in existing_data[1:]:
                if row and row[0]:
                    existing_dates.add(row[0])
        print(f"Found {len(existing_dates)} existing entries")
    except Exception as e:
        print(f"Warning: Could not check existing data: {e}")
        existing_dates = set()
    
    # Get recent activities (last 30 days)
    print("\n📊 Fetching activities from last 30 days...")
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=30)
        activities = garmin.get_activities_by_date(
            start_date.strftime("%Y-%m-%d"),
            end_date.strftime("%Y-%m-%d")
        )
        print(f"Found {len(activities)} total activities")
    except Exception as e:
        print(f"❌ Failed to fetch activities: {e}")
        return
    
    if not activities:
        print("No activities found")
        return
    
    # Process each activity
    new_entries = 0
    for activity in activities:
        try:
            activity_date = activity.get('startTimeLocal', '')[:10]
            if not activity_date:
                continue
            
            date_obj = datetime.strptime(activity_date, "%Y-%m-%d")
            date_str = date_obj.strftime("%Y-%m-%d")
            
            # Skip if already in sheet
            if date_str in existing_dates:
                print(f"Skipping {date_str} - already exists")
                continue
            
            print(f"\n📊 Processing {date_str}...")
            
            # Extract activity metrics
            activity_name = activity.get('activityName', 'Unknown')
            distance_meters = safe_get(activity, 'distance', 0)
            distance_km = round(distance_meters / 1000, 2) if distance_meters else 0
            duration_seconds = safe_get(activity, 'duration', 0)
            duration_min = format_duration(duration_seconds)
            avg_pace = format_pace(distance_meters, duration_seconds)
            avg_hr = safe_get(activity, 'averageHR', 0)
            max_hr = safe_get(activity, 'maxHR', 0)
            calories = safe_get(activity, 'calories', 0)
            avg_cadence = safe_get(activity, 'averageRunningCadenceInStepsPerMinute', 0)
            elevation_gain = round(safe_get(activity, 'elevationGain', 0), 1)
            activity_type = activity.get('activityType', {}).get('typeKey', 'unknown')
            
            # Get daily health metrics
            daily_metrics = get_daily_metrics(garmin, date_str)
            sleep_metrics = get_sleep_data(garmin, date_str)
            cycle_data = get_menstrual_cycle(garmin, date_str)
            
            # Prepare row with all data
            row = [
                date_str,
                activity_name,
                distance_km,
                duration_min,
                avg_pace,
                avg_hr,
                max_hr,
                calories,
                avg_cadence,
                elevation_gain,
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
                sleep_metrics['total_sleep_min'],
                sleep_metrics['deep_sleep_min'],
                sleep_metrics['light_sleep_min'],
                sleep_metrics['rem_sleep_min'],
                sleep_metrics['awake_min'],
                fitness_age,
                cycle_data['phase'],
                cycle_data['flow']
            ]
            
            # Append to sheet
            sheet.append_row(row)
            print(f"✅ Added: {date_str} - {activity_name} ({distance_km} km)")
            new_entries += 1
            
            existing_dates.add(date_str)
            
        except Exception as e:
            print(f"❌ Error processing activity: {e}")
            import traceback
            traceback.print_exc()
            continue
    
    if new_entries > 0:
        print(f"\n🎉 Successfully added {new_entries} new days of data!")
    else:
        print("\n✓ No new data to add")

if __name__ == "__main__":
    main()
