"""Тесты для parse_datetime_human и add_months."""

from __future__ import annotations

import calendar
import os
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

# Фиксируем TZ до импорта app.config
os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("OWNER_CHAT_ID", "1")
os.environ.setdefault("TIMEZONE", "Asia/Ashgabat")

from app.utils import parse_datetime_human  # noqa: E402
from app.handlers.renew import add_months  # noqa: E402

TZ = ZoneInfo("Asia/Ashgabat")


# ───────── parse_datetime_human ─────────


class TestParseDatetimeHuman(unittest.TestCase):
    """Парсинг строки YYYY-MM-DD HH:MM:SS → tz-aware datetime."""

    def test_valid(self):
        dt = parse_datetime_human("2025-03-15 10:30:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2025)
        self.assertEqual(dt.month, 3)
        self.assertEqual(dt.day, 15)
        self.assertEqual(dt.hour, 10)
        self.assertEqual(dt.minute, 30)
        self.assertEqual(dt.second, 0)
        # должна быть tz-aware
        self.assertIsNotNone(dt.tzinfo)

    def test_midnight(self):
        dt = parse_datetime_human("2025-01-01 00:00:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.hour, 0)

    def test_end_of_day(self):
        dt = parse_datetime_human("2025-12-31 23:59:59")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.second, 59)

    def test_empty(self):
        self.assertIsNone(parse_datetime_human(""))

    def test_none(self):
        self.assertIsNone(parse_datetime_human(None))

    def test_garbage(self):
        self.assertIsNone(parse_datetime_human("not a date"))

    def test_wrong_format_slash(self):
        self.assertIsNone(parse_datetime_human("15/03/2025 10:30:00"))

    def test_wrong_format_no_time(self):
        self.assertIsNone(parse_datetime_human("2025-03-15"))

    def test_wrong_format_no_seconds(self):
        self.assertIsNone(parse_datetime_human("2025-03-15 10:30"))

    def test_invalid_date_feb30(self):
        self.assertIsNone(parse_datetime_human("2025-02-30 00:00:00"))

    def test_invalid_date_month13(self):
        self.assertIsNone(parse_datetime_human("2025-13-01 00:00:00"))

    def test_leap_year_feb29(self):
        dt = parse_datetime_human("2024-02-29 12:00:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.day, 29)

    def test_non_leap_feb29(self):
        self.assertIsNone(parse_datetime_human("2025-02-29 12:00:00"))

    def test_whitespace_trimmed(self):
        dt = parse_datetime_human("  2025-06-01 08:00:00  ")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.month, 6)


# ───────── add_months ─────────


class TestAddMonths(unittest.TestCase):
    """Прибавление месяцев к дате с обработкой граничных дней."""

    # --- обычные случаи ---

    def test_basic_plus1(self):
        dt = datetime(2025, 3, 15, 10, 0, 0, tzinfo=TZ)
        result = add_months(dt, 1)
        self.assertEqual(result, datetime(2025, 4, 15, 10, 0, 0, tzinfo=TZ))

    def test_plus2(self):
        dt = datetime(2025, 1, 10, 0, 0, 0, tzinfo=TZ)
        result = add_months(dt, 2)
        self.assertEqual(result, datetime(2025, 3, 10, 0, 0, 0, tzinfo=TZ))

    # --- граничные случаи: 31.01 → февраль ---

    def test_jan31_to_feb_non_leap(self):
        """31 января + 1 месяц → 28 февраля (не високосный год)."""
        dt = datetime(2025, 1, 31, 12, 0, 0, tzinfo=TZ)
        result = add_months(dt, 1)
        self.assertEqual(result.month, 2)
        self.assertEqual(result.day, 28)
        self.assertEqual(result.year, 2025)

    def test_jan31_to_feb_leap(self):
        """31 января + 1 месяц → 29 февраля (високосный год)."""
        dt = datetime(2024, 1, 31, 12, 0, 0, tzinfo=TZ)
        result = add_months(dt, 1)
        self.assertEqual(result.month, 2)
        self.assertEqual(result.day, 29)
        self.assertEqual(result.year, 2024)

    def test_jan30_to_feb(self):
        """30 января + 1 месяц → 28 февраля."""
        dt = datetime(2025, 1, 30, 0, 0, 0, tzinfo=TZ)
        result = add_months(dt, 1)
        self.assertEqual(result.month, 2)
        self.assertEqual(result.day, 28)

    # --- граничные случаи: переход через год ---

    def test_dec15_to_jan_next_year(self):
        """15 декабря + 1 месяц → 15 января следующего года."""
        dt = datetime(2025, 12, 15, 18, 30, 0, tzinfo=TZ)
        result = add_months(dt, 1)
        self.assertEqual(result.year, 2026)
        self.assertEqual(result.month, 1)
        self.assertEqual(result.day, 15)
        # время не должно измениться
        self.assertEqual(result.hour, 18)
        self.assertEqual(result.minute, 30)

    def test_dec31_to_jan(self):
        """31 декабря + 1 месяц → 31 января следующего года."""
        dt = datetime(2025, 12, 31, 0, 0, 0, tzinfo=TZ)
        result = add_months(dt, 1)
        self.assertEqual(result, datetime(2026, 1, 31, 0, 0, 0, tzinfo=TZ))

    def test_nov30_to_dec(self):
        """30 ноября + 1 месяц → 30 декабря."""
        dt = datetime(2025, 11, 30, 0, 0, 0, tzinfo=TZ)
        result = add_months(dt, 1)
        self.assertEqual(result.month, 12)
        self.assertEqual(result.day, 30)

    # --- несколько месяцев с переходом года ---

    def test_oct31_plus3(self):
        """31 октября + 3 месяца → 31 января следующего года."""
        dt = datetime(2025, 10, 31, 0, 0, 0, tzinfo=TZ)
        result = add_months(dt, 3)
        self.assertEqual(result, datetime(2026, 1, 31, 0, 0, 0, tzinfo=TZ))

    def test_plus12_same_date(self):
        """+12 месяцев = ровно год."""
        dt = datetime(2025, 6, 15, 10, 0, 0, tzinfo=TZ)
        result = add_months(dt, 12)
        self.assertEqual(result, datetime(2026, 6, 15, 10, 0, 0, tzinfo=TZ))

    def test_feb29_plus12_non_leap(self):
        """29 февраля + 12 месяцев → 28 февраля (не високосный)."""
        dt = datetime(2024, 2, 29, 0, 0, 0, tzinfo=TZ)
        result = add_months(dt, 12)
        self.assertEqual(result.year, 2025)
        self.assertEqual(result.month, 2)
        self.assertEqual(result.day, 28)

    # --- крайние и нулевые ---

    def test_plus0(self):
        """0 месяцев — дата не меняется."""
        dt = datetime(2025, 5, 20, 8, 0, 0, tzinfo=TZ)
        result = add_months(dt, 0)
        self.assertEqual(result, dt)

    def test_mar31_plus1_to_apr30(self):
        """31 марта + 1 месяц → 30 апреля (в апреле 30 дней)."""
        dt = datetime(2025, 3, 31, 0, 0, 0, tzinfo=TZ)
        result = add_months(dt, 1)
        self.assertEqual(result.month, 4)
        self.assertEqual(result.day, 30)


if __name__ == "__main__":
    unittest.main()
