"""Проверка логики: python -m unittest test_logic"""
import unittest
from datetime import date, time

from logic import (DAY, HOUR, humanize_left, parse_date, parse_time, passed_reminders,
                   reminder_to_send)

DUE = 1_000_000_000


class Reminders(unittest.TestCase):
    def test_sequence(self):
        sent = passed_reminders(DUE, DUE - 10 * DAY)
        self.assertEqual(sent, set())
        got = []
        for t in range(DUE - 10 * DAY, DUE + HOUR, 60):
            key, mark = reminder_to_send(DUE, t, sent)
            if key:
                got.append((key, DUE - t))
                sent |= mark
        self.assertEqual(got, [("7d", 7 * DAY), ("2d", 2 * DAY), ("1d", DAY), ("12h", 12 * HOUR)])

    def test_created_late_skips_past(self):
        sent = passed_reminders(DUE, DUE - 3 * DAY)
        self.assertEqual(sent, {"7d"})
        self.assertEqual(reminder_to_send(DUE, DUE - 3 * DAY, sent), (None, set()))

    def test_downtime_sends_only_latest(self):
        key, mark = reminder_to_send(DUE, DUE - 20 * HOUR, set())
        self.assertEqual(key, "1d")
        self.assertEqual(mark, {"7d", "2d", "1d"})

    def test_after_due_nothing(self):
        self.assertEqual(reminder_to_send(DUE, DUE + 1, set()), (None, set()))


class Texts(unittest.TestCase):
    def test_humanize(self):
        self.assertEqual(humanize_left(7 * DAY), "неделя")
        self.assertEqual(humanize_left(2 * DAY), "2 суток")
        self.assertEqual(humanize_left(DAY), "сутки")
        self.assertEqual(humanize_left(12 * HOUR), "12 часов")
        self.assertEqual(humanize_left(DAY + 5 * HOUR), "1 сутки 5 часов")
        self.assertEqual(humanize_left(21 * HOUR), "21 час")
        self.assertEqual(humanize_left(600), "10 минут")

    def test_parse(self):
        today = date(2026, 11, 10)
        self.assertEqual(parse_date("25.11", today), (date(2026, 11, 25), None))
        self.assertEqual(parse_date("01.02", today), (date(2027, 2, 1), None))
        self.assertEqual(parse_date("25.11.26 18:00", today), (date(2026, 11, 25), time(18, 0)))
        self.assertIsNone(parse_date("31.02", today))
        self.assertIsNone(parse_date("завтра", today))
        self.assertEqual(parse_time("9:05"), time(9, 5))
        self.assertIsNone(parse_time("25:00"))


if __name__ == "__main__":
    unittest.main()
