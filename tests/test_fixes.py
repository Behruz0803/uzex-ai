"""
Regression tests for the 05.11.2026 audit fixes + the near-match feature.

Run:  python -m pytest tests/ -q   (or: python tests/test_fixes.py)
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_helper as db
from agent import UzexAgent, detect_language, is_greeting, is_off_topic
from bot import split_message
from fallback_engine import FallbackEngine


class TestLanguageDetection(unittest.TestCase):
    """Audit fix #2 — Uzbek questions must not get Russian answers."""

    def test_uzbek_keywords(self):
        for q in ["Sement narxlari", "O'zbekneftgaz aylanmasi", "Eng yirik savdo",
                  "2024-yilda nechta bitim?", "Navoiy viloyatida savdo qancha?"]:
            self.assertEqual(detect_language(q), "uz", q)

    def test_short_code_defaults_uz(self):
        self.assertEqual(detect_language("BK001"), "uz")
        self.assertEqual(detect_language("100001"), "uz")

    def test_russian(self):
        for q in ["Крупнейший покупатель?", "Сколько сделок по цементу?",
                  "Какой общий объём за 2024 год?"]:
            self.assertEqual(detect_language(q), "ru", q)

    def test_mixed_uzbek_wins(self):
        self.assertEqual(detect_language("Цемент narxi qancha?"), "uz")

    def test_default_is_uz_not_ru(self):
        self.assertEqual(detect_language("DROP TABLE deals"), "uz")


class TestGibberishGuard(unittest.TestCase):
    """Audit fix #3 — no hallucinated answers for gibberish / injection."""

    def setUp(self):
        self.agent = UzexAgent()

    def test_gibberish_off_topic(self):
        a = self.agent.answer("asdfghjkl nimadir")
        self.assertTrue(a.off_topic)
        self.assertIsNone(a.sql)

    def test_injection_rejected(self):
        a = self.agent.answer("DROP TABLE deals; SELECT * FROM deals;")
        self.assertTrue(a.off_topic)
        self.assertIsNone(a.sql)

    def test_greeting(self):
        self.assertTrue(is_greeting("salom"))
        self.assertTrue(is_greeting("salom bot"))
        self.assertTrue(is_greeting("Привет"))
        self.assertFalse(is_greeting("salom, 2024-yilda aylanma qancha?"))

    def test_sql_validation(self):
        for bad in ["DROP TABLE deals", "DELETE FROM deals",
                    "SELECT 1; DROP TABLE deals", "PRAGMA table_info(deals)",
                    "UPDATE deals SET amount=0", "ATTACH DATABASE 'x' AS x"]:
            ok, _ = db.validate_sql(bad)
            self.assertFalse(ok, bad)
        ok, _ = db.validate_sql("SELECT COUNT(*) FROM deals")
        self.assertTrue(ok)


class TestChunking(unittest.TestCase):
    """Audit fix #4 — Telegram 4096-char limit."""

    def test_short(self):
        self.assertEqual(split_message("hello"), ["hello"])

    def test_long_split(self):
        text = "\n".join(f"line {i} " + "x" * 80 for i in range(120))
        parts = split_message(text)
        self.assertGreater(len(parts), 1)
        for p in parts:
            self.assertLessEqual(len(p), 4000)
        self.assertEqual("".join(parts).replace("\n", "").replace(" ", ""),
                         text.replace("\n", "").replace(" ", ""))

    def test_no_mid_word_cut_when_possible(self):
        text = "word " * 1500
        parts = split_message(text)
        for p in parts:
            self.assertLessEqual(len(p), 4000)


class TestEntitySearch(unittest.TestCase):
    """Audit fix #5 — all FTS tables + deal-number regex."""

    def test_broker_found(self):
        e = db.search_entities("BK001 brokeri")
        self.assertTrue(e["brokers"])
        self.assertEqual(e["brokers"][0]["broker_code"], "BK001")

    def test_product_found(self):
        e = db.search_entities("sement narxlari")
        self.assertTrue(e["products"])

    def test_deal_number_found(self):
        e = db.search_entities("604000 raqamli bitim")
        self.assertTrue(e["deals"])
        self.assertEqual(e["deals"][0]["deal_number"], 604000)

    def test_contract_found(self):
        e = db.search_entities("100001 shartnoma")
        self.assertTrue(e["contracts"])

    def test_inn_found(self):
        e = db.search_entities("STIR 999999999")
        self.assertIn("999999999", e["inns"])


class TestNearMatch(unittest.TestCase):
    """New feature — exact miss -> explicit close-results answer."""

    def setUp(self):
        self.agent = UzexAgent()

    def test_2018_near_match_uz(self):
        a = self.agent.answer("2018-yilda birjada nechta bitim tuzilgan?")
        self.assertTrue(a.near_match)
        self.assertIn("topilmadi", a.text)
        self.assertIn("yaqin", a.text)
        self.assertIsNotNone(a.sql)

    def test_2030_near_match(self):
        a = self.agent.answer("2030-yilda nechta bitim kutilmoqda?")
        self.assertTrue(a.near_match)

    def test_2018_near_match_ru(self):
        a = self.agent.answer("Сколько сделок было в 2018 году?")
        self.assertTrue(a.near_match)
        self.assertIn("не найдено", a.text)
        self.assertIn("близких", a.text)

    def test_no_data_when_truly_absent(self):
        a = self.agent.answer("Bitcoin yoki iPhone bo'yicha qanday bitimlar bor?")
        self.assertFalse(a.near_match)
        self.assertIn("mavjud emas", a.text)


class TestAccuracy(unittest.TestCase):
    """Ground-truth numbers from the audited database."""

    def setUp(self):
        self.agent = UzexAgent()

    def test_total_deals(self):
        a = self.agent.answer("Bazada jami nechta bitim mavjud?")
        self.assertIn("500 000", a.text)

    def test_bk001(self):
        a = self.agent.answer("BK001 brokeri orqali jami nechta bitim o'tkazilgan va umumiy summa qancha?")
        self.assertIn("4 587", a.text)

    def test_navoiy_2024(self):
        a = self.agent.answer("Navoiy viloyatida 2024-yilda nechta bitim tuzilgan?")
        self.assertIn("3 981", a.text)

    def test_2024_turnover(self):
        a = self.agent.answer("2024-yilda birjada jami qancha so'mlik bitim tuzilgan?")
        self.assertIn("42 724", a.text)

    def test_language_of_answer(self):
        a = self.agent.answer("Какой общий объём сделок за 2024 год?")
        self.assertEqual(a.lang, "ru")
        self.assertIn("сум", a.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
