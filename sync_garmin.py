"""Модуль выгрузки и предобработки данных тренировок из Garmin Connect.

Обеспечивает авторизацию, получение сырых записей, очистку дат от артефактов
(ведущие апострофы, кавычки), приведение типов и экспорт в CSV.
"""

from datetime import datetime
import logging
import os
import re
from typing import Any, Dict, List, Optional
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
)
import pandas as pd

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


def clean_date_string(raw_date: Any) -> Optional[str]:
    """Нормализует дату к формату ISO (YYYY-MM-DD), удаляя артефакты форматирования

    (ведущие одинарные/двойные кавычки, обратные апострофы, пробелы).
    """
    if pd.isna(raw_date) or raw_date is None:
        return None

    val_str = str(raw_date).strip()

    # Точное извлечение YYYY-MM-DD регулярным выражением
    match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", val_str)
    if match:
        return match.group(1)

    # Запасной парсинг через ISO формат со снятием паразитных ведущих символов
    try:
        clean_str = val_str.lstrip("'\"` \t")
        return datetime.fromisoformat(clean_str).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        logger.warning("Не удалось распарсить дату: %s", raw_date)
        return None


def parse_activity_record(activity: Dict[str, Any]) -> Dict[str, Any]:
    """Преобразует отдельную запись Garmin API в нормализованный словарь

    с валидированными числовыми полями и очищенной датой.
    """
    raw_start_time = activity.get("startTimeLocal") or activity.get(
        "startTimeGMT"
    )
    clean_date = clean_date_string(raw_start_time)

    # Безопасное приведение метрик
    distance_meters = float(activity.get("distance") or 0.0)
    duration_seconds = float(activity.get("duration") or 0.0)

    # Извлечение типа активности (может быть вложенным dict или строкой)
    raw_type = activity.get("activityType")
    if isinstance(raw_type, dict):
        activity_type = raw_type.get("typeKey", "unknown")
    else:
        activity_type = raw_type or "unknown"

    return {
        "activity_id": activity.get("activityId"),
        "activity_date": clean_date,
        "start_time": (
            str(raw_start_time).lstrip("'\"` \t") if raw_start_time else None
        ),
        "activity_type": activity_type,
        "distance_km": round(distance_meters / 1000.0, 2),
        "duration_min": round(duration_seconds / 60.0, 2),
        "avg_hr": activity.get("averageHR"),
        "max_hr": activity.get("maxHR"),
        "elevation_gain_m": activity.get("elevationGain"),
    }


def fetch_garmin_activities(
    email: Optional[str] = None,
    password: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Авторизуется в сервисе Garmin Connect и загружает список последних активностей."""
    garmin_email = email or os.getenv("GARMIN_EMAIL")
    garmin_password = password or os.getenv("GARMIN_PASSWORD")

    if not garmin_email or not garmin_password:
        raise ValueError(
            "Не заданы учетные данные Garmin (GARMIN_EMAIL / GARMIN_PASSWORD)."
        )

    try:
        client = Garmin(garmin_email, garmin_password)
        client.login()
        logger.info("Успешная авторизация в Garmin Connect.")
        activities = client.get_activities(0, limit)
        logger.info("Получено записей активностей: %d", len(activities))
        return activities
    except (
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
    ) as exc:
        logger.error("Ошибка при подключении к Garmin Connect: %s", exc)
        raise


def transform_activities_to_df(
    raw_activities: List[Dict[str, Any]],
) -> pd.DataFrame:
    """Формирует типизированный Pandas DataFrame с валидацией колонок."""
    if not raw_activities:
        logger.warning("Передан пустой список активностей.")
        return pd.DataFrame()

    records = [parse_activity_record(item) for item in raw_activities]
    df = pd.DataFrame(records)

    # Принудительная типизация дат и сортировка
    df["activity_date"] = pd.to_datetime(
        df["activity_date"], format="%Y-%m-%d", errors="coerce"
    )
    df = df.dropna(subset=["activity_date"])
    df = df.sort_values(by="activity_date", ascending=False).reset_index(
        drop=True
    )

    return df


def export_to_csv(df: pd.DataFrame, output_path: str = "activities.csv") -> str:
    """Сохраняет датасет в CSV-файл с фиксацией формата даты без паразитных кавычек."""
    if df.empty:
        logger.warning("Датафрейм пуст. Файл не записан.")
        return output_path

    # date_format гарантирует сохранение '2026-09-21' без ведущих знаков апострофа
    df.to_csv(output_path, index=False, date_format="%Y-%m-%d", encoding="utf-8")
    logger.info("Данные успешно сохранены в: %s", output_path)
    return output_path


def run_pipeline(limit: int = 50, output_filename: str = "garmin_clean.csv"):
    """Запускает полный цикл: извлечение -> очистка -> экспорт."""
    raw_data = fetch_garmin_activities(limit=limit)
    df = transform_activities_to_df(raw_data)
    export_to_csv(df, output_filename)


if __name__ == "__main__":
    # Для автономного тестирования без прямого вызова API:
    dummy_activities = [
        {
            "activityId": 12345678,
            "startTimeLocal": "'2026-09-21 08:30:00",  # Кейс с паразитным ведущим апострофом
            "activityType": {"typeKey": "running"},
            "distance": 10250.0,
            "duration": 3120.0,
            "averageHR": 148,
            "maxHR": 165,
            "elevationGain": 85.0,
        },
        {
            "activityId": 12345679,
            "startTimeLocal": "2026-09-22T19:15:00",
            "activityType": {"typeKey": "treadmill_running"},
            "distance": 5000.0,
            "duration": 1500.0,
            "averageHR": 138,
            "maxHR": 152,
            "elevationGain": 0.0,
        },
    ]

    processed_df = transform_activities_to_df(dummy_activities)
    export_to_csv(processed_df, "test_activities.csv")
    print(processed_df[["activity_id", "activity_date", "distance_km"]])
