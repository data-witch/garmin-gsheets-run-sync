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

SYNC_DAYS = int(os.environ.get('SYNC_DAYS', 100))
TOKEN_DIR = os.path.expanduser(os.environ.get('GARMIN_TOKEN_DIR', '~/.garth'))

# Заголовки для листа с тренировками (Лист 1)
ACTIVITY_HEADERS = [
    "Дата", "ID активности", "Название активности", "Дистанция (км)", 
    "Длительность (мин)", "Средний темп (мин/км)", "Ср. ЧСС", 
    "Макс. ЧСС", "Калории", "Ср. Каденс", "Набор высоты (м)", "Тип активности"
]

# Заголовки для листа с ежедневным состоянием (Лист 2)
DAILY_HEADERS = [
    "Дата", "Шаги", "Этажи", "Стресс", "Body Battery Макс", "Body Battery Мин",
    "HRV Ср.", "HRV Статус", "Дыхание", "SpO2",
    "Сон Всего (мин)", "Оценка сна", "ЧСС покоя",
    "VO2 Max Бег", "VO2 Max Вело", "Статус тренировки",
    "Острая нагрузка", "Хроническая нагрузка", "Фитнес-возраст", "Фаза цикла"
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


def ensure_headers(sheet, headers):
    """Проверяет и создает заголовки на листе"""
    try:
        current_headers = sheet.row_values(1)
        if not current_headers:
            sheet.update('A1', [headers], value_input_option='RAW')
            print(f"✅ Созданы заголовки на листе {sheet.title}")
            return

        # Проверяем, нужно ли обновить заголовки
        if len(current_headers) < len(headers):
            # Добавляем недостающие
            updated_headers = current_headers + headers[len(current_headers):]
            sheet.update('A1', [updated_headers], value_input_option='RAW')
            print(f"✅ Обновлены заголовки на листе {sheet.title}")
        elif current_headers[:len(headers)] != headers:
            sheet.update('A1', [headers], value_input_option='RAW')
            print(f"✅ Заголовки на листе {sheet.title} приведены к нужному формату")
    except Exception as exc:
        print(f"⚠️  Не удалось обновить заголовки на листе {sheet.title}: {exc}")


def get_fitness_age(garmin):
    today = datetime.today().strftime('%Y-%m-%d')
    data = safe_call(garmin.get_fitnessage_data, today, default={}) or {}
    fitness_age = data.get('fitnessAge') or data.get('currentFitnessAge')
    if fitness_age is not None:
        return fitness_age

    max_metrics = safe_call(garmin.get_max_metrics, today, default={}) or {}
    return max_metrics.get('fitnessAge', '') or ''


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
    """Получает все ежедневные метрики для указанной даты"""
    metrics = {
        'steps': 0,
        'floors': 0,
        'stress': 0,
        'body_battery_max': 0,
        'body_battery_min': 0,
        'hrv_avg': 0,
        'hrv_status': '',
        'respiration': 0,
        'spo2': 0,
        'total_sleep_min': 0,
        'sleep_score': '',
        'resting_hr': 0,
        'vo2max_running': '',
        'vo2max_cycling': '',
        'training_status': '',
        'acute_training_load': '',
        'chronic_training_load': '',
        'menstrual_phase': '',
        'fitness_age': fitness_age,
    }

    summary = safe_call(garmin.get_user_summary, date_str, default={}) or {}
    sleep_data = safe_call(garmin.get_sleep_data, date_str, default={}) or {}
    training_status = safe_call(garmin.get_training_status, date_str, default={}) or {}
    hrv_payload = safe_call(garmin.get_hrv_data, date_str, default={}) or {}

    metrics['steps'] = summary.get('totalSteps') or 0
    metrics['floors'] = summary.get('floorsAscended') or 0
    metrics['stress'] = summary.get('averageStressLevel') or 0
    metrics['body_battery_max'] = summary.get('bodyBatteryHighestValue') or 0
    metrics['body_battery_min'] = summary.get('bodyBatteryLowestValue') or 0
    metrics['resting_hr'] = summary.get('restingHeartRate') or 0

    sleep_dto = sleep_data.get('dailySleepDTO', {}) if isinstance(sleep_data, dict) else {}
    sleep_scores = sleep_dto.get('sleepScores') or {}
    metrics['sleep_score'] = sleep_scores.get('overall', {}).get('value', '') or ''
    metrics['total_sleep_min'] = seconds_to_minutes(sleep_dto.get('sleepTimeSeconds', 0))

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

    return metrics


def get_avg_cadence(activity):
    return (
        activity.get('averageRunningCadenceInStepsPerMinute')
        or activity.get('averageBikingCadenceInRevPerMinute')
        or activity.get('averageCadence')
        or 0
    ) or 0


def build_activity_row(activity):
    """Создает строку для листа с тренировками"""
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
    ]


def build_daily_row(date_str, metrics):
    """Создает строку для листа с ежедневным состоянием"""
    return [
        date_str,
        metrics['steps'],
        metrics['floors'],
        metrics['stress'],
        metrics['body_battery_max'],
        metrics['body_battery_min'],
        metrics['hrv_avg'],
        metrics['hrv_status'],
        metrics['respiration'],
        metrics['spo2'],
        metrics['total_sleep_min'],
        metrics['sleep_score'],
        metrics['resting_hr'],
        metrics['vo2max_running'],
        metrics['vo2max_cycling'],
        metrics['training_status'],
        metrics['acute_training_load'],
        metrics['chronic_training_load'],
        metrics['fitness_age'],
        metrics['menstrual_phase'],
    ]


def get_existing_keys(sheet, key_column=1):
    """Получает существующие ключи (даты или ID) из листа"""
    existing_keys = set()
    try:
        all_data = sheet.get_all_values()
        if len(all_data) > 1:
            for row in all_data[1:]:
                if row and row[0]:
                    if key_column == 1:
                        existing_keys.add(row[0])  # Только дата
                    else:
                        # Для активностей используем комбинацию даты и ID
                        if len(row) > 1:
                            existing_keys.add((row[0], row[1]))
                        else:
                            existing_keys.add(row[0])
        print(f"Найдено {len(existing_keys)} существующих записей на листе {sheet.title}")
    except Exception as exc:
        print(f"⚠️  Не удалось проверить существующие данные на листе {sheet.title}: {exc}")
    return existing_keys


def insert_row_sorted(sheet, row, date_column=0):
    """Вставляет строку с сортировкой по дате (возрастание)"""
    all_data = sheet.get_all_values()
    
    if len(all_data) <= 1:
        # Если данных нет, просто добавляем
        sheet.append_row(row, value_input_option='USER_ENTERED')
        return
    
    new_date = row[date_column]
    insert_position = len(all_data) + 1
    
    # Ищем позицию для вставки (по возрастанию даты)
    for i in range(1, len(all_data)):
        existing_date = all_data[i][date_column] if all_data[i] and len(all_data[i]) > date_column else ''
        if existing_date and new_date < existing_date:
            insert_position = i + 1
            break
    
    sheet.insert_row(row, insert_position, value_input_option='USER_ENTERED')


def main():
    print(f"Начинаем синхронизацию Garmin (последние {SYNC_DAYS} дней)...")

    garmin_email = os.environ.get('GARMIN_EMAIL')
    garmin_password = os.environ.get('GARMIN_PASSWORD')
    google_creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    sheet_id = os.environ.get('SHEET_ID')

    if not google_creds_json and os.path.exists('credentials.json'):
        print("Загрузка Google credentials из credentials.json...")
        with open('credentials.json', 'r') as f:
            google_creds_json = f.read()

    if not all([garmin_email, garmin_password, google_creds_json, sheet_id]):
        print("❌ Отсутствуют необходимые переменные окружения")
        print(f"   GARMIN_EMAIL: {'✓' if garmin_email else '✗'}")
        print(f"   GARMIN_PASSWORD: {'✓' if garmin_password else '✗'}")
        print(f"   GOOGLE_CREDENTIALS: {'✓' if google_creds_json else '✗'}")
        print(f"   SHEET_ID: {'✓' if sheet_id else '✗'}")
        return

    print("Подключение к Garmin...")
    try:
        garmin = connect_garmin(garmin_email, garmin_password)
        print("✅ Подключено к Garmin")
    except Exception as exc:
        print(f"❌ Не удалось подключиться к Garmin: {exc}")
        return

    fitness_age = get_fitness_age(garmin)
    if fitness_age:
        print(f"Фитнес-возраст: {fitness_age}")

    end_date = datetime.today()
    start_date = end_date - timedelta(days=SYNC_DAYS)
    start_str = start_date.strftime('%Y-%m-%d')
    end_str = end_date.strftime('%Y-%m-%d')

    print(f"Получение активностей с {start_str} по {end_str}...")
    try:
        activities = garmin.get_activities_by_date(start_str, end_str) or []
        print(f"Найдено {len(activities)} активностей")
    except Exception as exc:
        print(f"❌ Не удалось получить активности: {exc}")
        return

    print("Подключение к Google Sheets...")
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
        spreadsheet = client.open_by_key(sheet_id)
        
        # Получаем или создаем листы
        try:
            activity_sheet = spreadsheet.worksheet("Лист1")
        except gspread.WorksheetNotFound:
            activity_sheet = spreadsheet.add_worksheet(title="Лист1", rows=100, cols=20)
            print("✅ Создан лист 'Лист1' для тренировок")
        
        try:
            daily_sheet = spreadsheet.worksheet("Лист2")
        except gspread.WorksheetNotFound:
            daily_sheet = spreadsheet.add_worksheet(title="Лист2", rows=100, cols=20)
            print("✅ Создан лист 'Лист2' для ежедневного состояния")
        
        print("✅ Подключено к Google Sheets")
    except Exception as exc:
        print(f"❌ Не удалось подключиться к Google Sheets: {exc}")
        return

    # Настраиваем заголовки на обоих листах
    ensure_headers(activity_sheet, ACTIVITY_HEADERS)
    ensure_headers(daily_sheet, DAILY_HEADERS)

    # Получаем существующие записи для проверки дубликатов
    existing_activities = get_existing_keys(activity_sheet, key_column=2)  # по ID
    existing_daily = get_existing_keys(daily_sheet, key_column=1)  # только даты

    daily_cache = {}
    new_activities = 0
    new_daily = 0

    for activity in activities:
        try:
            activity_date = activity.get('startTimeLocal', '')[:10]
            activity_name = activity.get('activityName', 'Activity')
            activity_id = str(activity.get('activityId', ''))
            
            # Проверяем, есть ли уже такая активность
            activity_key = (activity_date, activity_id) if activity_id else (activity_date, activity_name)
            if activity_key in existing_activities:
                print(f"⏭️  Пропускаем активность {activity_date} - {activity_name} (уже существует)")
                continue
            
            # Получаем ежедневные метрики (если еще не получены)
            if activity_date not in daily_cache:
                print(f"📊 Получение ежедневных метрик для {activity_date}...")
                daily_cache[activity_date] = get_daily_metrics(garmin, activity_date, fitness_age)
            
            metrics = daily_cache[activity_date]
            
            # Добавляем активность на Лист1
            activity_row = build_activity_row(activity)
            insert_row_sorted(activity_sheet, activity_row)
            existing_activities.add(activity_key)
            new_activities += 1
            print(f"✅ Добавлена активность: {activity_date} - {activity_name}")
            
            # Проверяем, есть ли уже ежедневные данные за эту дату
            if activity_date not in existing_daily:
                # Добавляем ежедневные метрики на Лист2
                daily_row = build_daily_row(activity_date, metrics)
                insert_row_sorted(daily_sheet, daily_row)
                existing_daily.add(activity_date)
                new_daily += 1
                print(f"✅ Добавлены ежедневные метрики: {activity_date}")
            
        except Exception as exc:
            print(f"❌ Ошибка при обработке активности: {exc}")
            continue

    # Выводим итоги
    print("\n" + "="*50)
    if new_activities > 0:
        print(f"🎉 Добавлено {new_activities} новых тренировок на Лист1")
    else:
        print("📭 Новых тренировок для добавления нет")
    
    if new_daily > 0:
        print(f"🎉 Добавлено {new_daily} новых записей состояния на Лист2")
    else:
        print("📭 Новых записей состояния для добавления нет")
    print("="*50)


if __name__ == "__main__":
    main()
