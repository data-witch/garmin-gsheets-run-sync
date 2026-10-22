import unittest
from datetime import date

import sync_garmin as sync


class SyncLogicTest(unittest.TestCase):
    def test_dates_from_text_and_serial_roundtrip(self):
        day = date(2026, 10, 22)
        serial = sync.date_to_serial(day)
        self.assertEqual(sync.normalize_sheet_date(serial), "2026-10-22")
        self.assertEqual(sync.normalize_sheet_date("22.10.2026"), "2026-10-22")
        self.assertEqual(sync.normalize_sheet_date("07.02.2026"), "2026-02-07")
        self.assertEqual(sync.normalize_sheet_date("31.08.2026"), "2026-08-31")
        self.assertEqual(sync.normalize_sheet_date("2026-02-07 20:08:13"), "2026-02-07")
        self.assertEqual(sync.normalize_sheet_date("1.10.2026"), "2026-10-01")

    def test_russian_decimal_text(self):
        self.assertEqual(sync.parse_decimal_text("5,080"), 5.08)
        self.assertEqual(sync.parse_decimal_text("44,37"), 44.37)
        self.assertEqual(sync.parse_decimal_text("135"), 135)
        self.assertIsNone(sync.parse_decimal_text("running"))

    def test_blank_new_value_keeps_existing_cell(self):
        headers = ["Дата", "Ср. ЧСС", "Ср. каденс"]
        old = [46000, 150, 168]
        new = {"Дата": 46001, "Ср. ЧСС": 148, "Ср. каденс": ""}
        self.assertEqual(sync.build_output_row(headers, new, old), [46001, 148, 168])

    def test_custom_column_is_left_in_place(self):
        headers = ["Заметка", "Дата", "Шаги"]
        old = ["колено 2", "07.02.2026", "1000"]
        new = {"Дата": 46000, "Шаги": 5000}
        self.assertEqual(
            sync.build_output_row(headers, new, old),
            ["колено 2", 46000, 5000],
        )

    def test_daily_window_refreshes_recent_days_and_skips_today(self):
        today = date(2026, 10, 22)
        existing = {"2026-10-18", "2026-10-19"}
        dates = sync.daily_dates_to_sync(existing, today, date(2026, 10, 18), refresh_days=3)
        self.assertEqual(dates, ["2026-10-19", "2026-10-20", "2026-10-21"])
        self.assertNotIn(today.isoformat(), dates)

    def test_missing_history_is_included_once(self):
        today = date(2026, 10, 22)
        dates = sync.daily_dates_to_sync(set(), today, date(2026, 10, 20), refresh_days=3)
        self.assertEqual(dates, ["2026-10-20", "2026-10-21"])

    def test_activity_refresh_window_includes_today_only(self):
        today = date(2026, 10, 22)
        self.assertTrue(sync.activity_in_refresh_window("2026-10-22", today, 7))
        self.assertTrue(sync.activity_in_refresh_window("2026-10-15", today, 7))
        self.assertFalse(sync.activity_in_refresh_window("2026-10-14", today, 7))

    def test_sleep_score_survives_null_overall(self):
        minutes, score = sync.sleep_fields({
            "dailySleepDTO": {
                "sleepTimeSeconds": 24660,
                "sleepScores": {"overall": None},
            }
        })
        self.assertEqual(minutes, 411.0)
        self.assertEqual(score, "")

        minutes, score = sync.sleep_fields({
            "dailySleepDTO": {
                "sleepTimeSeconds": 30000,
                "sleepScores": {"overall": {"value": 80}},
            }
        })
        self.assertEqual(score, 80)

    def test_body_battery_from_point_pairs(self):
        battery_max, battery_min = sync.parse_body_battery(
            [{
                "calendarDate": "2026-10-21",
                "bodyBatteryValuesArray": [[1, 90], [2, 15], [3, 40]],
            }],
            "2026-10-21",
        )
        self.assertEqual((battery_max, battery_min), (90, 15))

    def test_activity_row_uses_serial_date_and_skips_missing_cadence(self):
        values = sync.activity_values({
            "activityId": 21788914335,
            "activityName": "Томск Бег",
            "startTimeLocal": "2026-02-07 08:00:00",
            "activityType": {"typeKey": "running"},
            "distance": 5080,
            "duration": 2662.2,
            "averageSpeed": 1.908,
            "averageHR": 150,
            "calories": 386,
        }, {})
        self.assertEqual(values["_day"], "2026-02-07")
        self.assertEqual(values["Дата"], sync.date_to_serial(date(2026, 2, 7)))
        self.assertEqual(values["Дистанция (км)"], 5.08)
        self.assertEqual(values["Ср. ЧСС"], 150)
        self.assertEqual(values["Ср. каденс"], "")
        self.assertEqual(values["ID активности"], 21788914335)

    def test_failed_summary_does_not_build_an_empty_day(self):
        class FakeGarmin:
            def get_user_summary(self, day):
                return {}

        self.assertIsNone(sync.build_daily_values(FakeGarmin(), "2026-10-21", {}, False))

    def test_daily_build_uses_summary_and_keeps_sleep_score(self):
        class FakeGarmin:
            def get_user_summary(self, day):
                return {
                    "totalSteps": 9959,
                    "floorsAscended": 3,
                    "averageStressLevel": 28,
                    "restingHeartRate": 48,
                    "bodyBatteryHighestValue": 92,
                    "bodyBatteryLowestValue": 18,
                    "averageSpo2": 96,
                    "averageWakingRespirationValue": 14.2,
                }

            def get_hrv_data(self, day):
                return {"hrvSummary": {"lastNightAvg": 75, "status": "BALANCED"}}

            def get_sleep_data(self, day):
                return {
                    "dailySleepDTO": {
                        "sleepTimeSeconds": 24660,
                        "sleepScores": {"overall": {"value": 78}},
                    }
                }

            def get_training_status(self, day):
                return {"mostRecentVO2Max": {"generic": {"vo2MaxValue": 43}}}

            def get_max_metrics(self, day):
                return {}

            def get_all_day_stress(self, day):
                raise AssertionError("stress endpoint is not needed")

        values = sync.build_daily_values(FakeGarmin(), "2026-10-21", {}, True)
        self.assertEqual(values["Шаги"], 9959)
        self.assertEqual(values["Сон Всего (мин)"], 411.0)
        self.assertEqual(values["Оценка сна"], 78)
        self.assertEqual(values["HRV Ср."], 75)
        self.assertEqual(values["VO2 Max Бег"], 43)
        self.assertEqual(values["Дата"], sync.date_to_serial(date(2026, 10, 21)))

    def test_sheet_id_can_be_a_full_url(self):
        url = "https://docs.google.com/spreadsheets/d/1CV5KDtmQUgRjUuY1CqJirFe0g8ncAFjuLdTdZ7djbok/edit?gid=1"
        self.assertEqual(
            sync.normalize_sheet_id(url),
            "1CV5KDtmQUgRjUuY1CqJirFe0g8ncAFjuLdTdZ7djbok",
        )

    def test_id_normalizes_sheet_floats(self):
        self.assertEqual(sync.normalize_id(21788914335.0), "21788914335")
        self.assertEqual(sync.normalize_id("21788914335.0"), "21788914335")


if __name__ == "__main__":
    unittest.main()
