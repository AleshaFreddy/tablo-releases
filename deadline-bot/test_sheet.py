"""Проверка разбора Google-таблицы: python -m unittest test_sheet"""
import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sheet import csv_url, parse_csv, parse_sheet_date

TZ = ZoneInfo("Europe/Moscow")
TODAY = date(2026, 11, 10)

# Так лист «26/27» выглядит в CSV-выгрузке (сокращено).
CSV = '''Название мероприятия,Сайт,Предмет,Что даёт?,(со)участие,Статусы,Ближайший дедлайн,Дедлайн конкурса,Дней до дедлайна,,Сегодня
Я – профессионал,,,master,"Айс, н м, оля, СЕНЯ","сайт лежит
upd. сайт работает",10 ноября,,0,,10.11.2026
Иностранные языки в современном мире,https://confhub.ru/rus/event/10703/,английский,баллы,"настя, сеня, Айс",,16 ноября,,6
XIII Беломорский студенческий форум,https://forms.yandex.ru/u/6ac/,,поездка,"айс, сеня",https://t.me/c/1/2,"15 ноября, 23:59",Форум пройдёт с 4 по 6 декабря 2026,6
воркшопе "Улицы центра Чебоксар",,,,"айс, сеня",,????,"18 - 29 ноября, г. Чебоксары",#VALUE!
универсиада ломоносов,https://universiade.msu.ru/,,,"айс, сеня",не проебать,,,-46336
III Всероссийский переводческий конкурс,https://ncfu.ru/x,английский,баллы,"сеня, настя м",настя подала,15 октября,резы 3 ноября,-26
X Международная конференция «Carpe Scientiam»,https://vk.ru/wall,экономика,cv,"сеня, настя, Айс",продумывание темы,8 ноября - заявка,18 ноября - конфа,#VALUE!
6th International Conference on Urban Planning,https://icupconference.org/,,CV,"айс, сеня",,23 ноября,"18 января 2027 г. - подача статьи",13
,,,,,,,,-46336
'''


class SheetDates(unittest.TestCase):
    def p(self, text):
        r = parse_sheet_date(text, TODAY, TZ)
        return (r[0].replace(tzinfo=None), r[1]) if r else None

    def test_formats(self):
        self.assertEqual(self.p("10 ноября"), (datetime(2026, 11, 10, 23, 59), ""))
        self.assertEqual(self.p("15 ноября, 23:59"), (datetime(2026, 11, 15, 23, 59), ""))
        self.assertEqual(self.p("8 ноября - заявка"), (datetime(2026, 11, 8, 23, 59), "заявка"))
        self.assertEqual(self.p("15 октября -- рега"), (datetime(2026, 10, 15, 23, 59), "рега"))
        self.assertEqual(self.p("18 января"), (datetime(2027, 1, 18, 23, 59), ""))
        self.assertEqual(self.p("18 января 2027 г. - подача статьи"), (datetime(2027, 1, 18, 23, 59), "подача статьи"))
        self.assertEqual(self.p("25.11.2026 18:00"), (datetime(2026, 11, 25, 18, 0), ""))
        self.assertEqual(self.p("рега 1 декабря в 12:00"), (datetime(2026, 12, 1, 12, 0), "рега"))
        self.assertIsNone(self.p("????"))
        self.assertIsNone(self.p(""))

    def test_url(self):
        self.assertEqual(
            csv_url("https://docs.google.com/spreadsheets/u/0/d/1VQZ/htmlview#gid=488004120"),
            "https://docs.google.com/spreadsheets/d/1VQZ/export?format=csv&gid=488004120")


class SheetRows(unittest.TestCase):
    def test_parse(self):
        res = parse_csv(CSV, TODAY, TZ)
        self.assertEqual(res.missing, [])
        by = {i.title: i for i in res.items}
        self.assertEqual(len(res.items), 6)
        self.assertEqual(by["Я – профессионал"].people, ["Айс", "н м", "оля", "СЕНЯ"])
        self.assertTrue(by["III Всероссийский переводческий конкурс"].done)
        self.assertFalse(by["XIII Беломорский студенческий форум"].done)
        self.assertEqual(by["X Международная конференция «Carpe Scientiam»"].stage, "заявка")
        self.assertIsNone(by["Я – профессионал"].link)
        self.assertEqual(len(res.problems), 1)  # «????»
        self.assertIn("????", res.problems[0])

    def test_missing_columns(self):
        res = parse_csv("a,b\n1,2\n", TODAY, TZ)
        self.assertEqual(res.missing, ["название мероприятия", "ближайший дедлайн"])


if __name__ == "__main__":
    unittest.main()
