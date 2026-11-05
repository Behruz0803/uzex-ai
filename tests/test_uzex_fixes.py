# -*- coding: utf-8 -*-
"""
Regression tests for the real UZEX agent (uzex/ package) — audit fixes + the
near-match feature. Runs against data/uzex.db (unpack data/uzex.db.gz first).

Run:  python tests/test_uzex_fixes.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from uzex.agent import answer_sync, detect_language, relax_sql, NEAR_PREFIX_UZ, NEAR_PREFIX_RU
from uzex.db import is_safe_select, search_entities


class TestLanguage(unittest.TestCase):
    """Audit fix #2 — Uzbek questions must not get Russian answers."""

    def test_uzbek(self):
        for q in ["BK001", "Sement narxlari", "O'zbekneftgaz aylanmasi",
                  "Eng yirik savdo", "2024-yilda nechta bitim?"]:
            self.assertEqual(detect_language(q), "uz", q)

    def test_russian(self):
        for q in ["Крупнейший покупатель?", "Сколько сделок по цементу?",
                  "Какой общий объём за 2024 год?"]:
            self.assertEqual(detect_language(q), "ru", q)

    def test_default_uz_not_ru(self):
        self.assertEqual(detect_language("DROP TABLE deals"), "uz")


class TestSafety(unittest.TestCase):
    """Audit fix #3 — read-only single SELECT."""

    def test_unsafe_rejected(self):
        for bad in ["DROP TABLE deals", "DELETE FROM deals",
                    "SELECT 1; DROP TABLE deals", "UPDATE deals SET deal_sum=0",
                    "PRAGMA table_info(deals)", "ATTACH DATABASE 'x' AS x"]:
            ok, _ = is_safe_select(bad)
            self.assertFalse(ok, bad)

    def test_safe_allowed(self):
        ok, _ = is_safe_select("SELECT COUNT(*) FROM deals")
        self.assertTrue(ok)


class TestEntitySearch(unittest.TestCase):
    """Audit fix #5 — all FTS tables + deal/contract numbers."""

    def test_broker(self):
        self.assertTrue(search_entities("BK001 brokeri")["brokers"])

    def test_product_synonym(self):
        self.assertTrue(search_entities("sement narxlari")["products"])

    def test_deal_number(self):
        e = search_entities("604000 raqamli bitim")
        self.assertTrue(e["deals"])

    def test_contract_number(self):
        self.assertTrue(search_entities("100001 shartnoma")["contracts"])

    def test_inn(self):
        self.assertIn("999999999", search_entities("STIR 999999999")["inns"])


class TestNearMatch(unittest.TestCase):
    """New feature — exact miss -> explicit close-results answer (UZ/RU)."""

    def test_relax_year(self):
        sql = "SELECT COUNT(*) AS cnt FROM deals WHERE deal_date BETWEEN '2018-01-01' AND '2018-12-31'"
        relaxed, notes = relax_sql(sql)
        self.assertIsNotNone(relaxed)
        self.assertNotIn("2018", relaxed)

    def test_uz_near_match(self):
        a = answer_sync("2018-yilda nechta bitim tuzilgan?")
        self.assertIn("topilmadi", a)
        self.assertIn("yaqin", a)

    def test_ru_near_match(self):
        a = answer_sync("Сколько сделок было в 2030 году?")
        self.assertIn("не найдено", a)
        self.assertIn("близких", a)

    def test_truly_absent_no_invention(self):
        a = answer_sync("Bitcoin bo'yicha bitimlar bormi?")
        self.assertIn("mavjud emas", a)


class TestAccuracy(unittest.TestCase):
    """Ground-truth numbers from the audited database."""

    def test_bk001(self):
        self.assertIn("4587", answer_sync("BK001 brokeri nechta bitimda qatnashgan?"))

    def test_2024_turnover(self):
        self.assertIn("42 724", answer_sync("2024-yilda jami qancha so'mlik bitim?"))

    def test_navoiy_region_filter(self):
        a = answer_sync("Navoiy viloyatida 2024-yilda nechta bitim tuzilgan?")
        # must be region-filtered, not the whole-2024 count (106647)
        self.assertNotIn("106647", a.replace(" ", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
