"""
Tests for the Gemini integration (audit fix #1) using a fake LLM — no API key
or network needed. Verifies retry/backoff, model fallback, SQL extraction,
self-correction and the Gemini-driven near-match flow.

Run:  python tests/test_gemini.py
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as ag


class FakeLLM:
    """Scripted LLM: returns queued responses, can raise retryable errors."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.available = True

    def generate(self, prompt, **kw):
        self.calls += 1
        if not self.responses:
            raise ag.LLMUnavailable("no more scripted responses")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class TestSQLExtraction(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(ag.UzexAgent._extract_sql("SELECT 1"), "SELECT 1")

    def test_markdown_fence(self):
        self.assertEqual(ag.UzexAgent._extract_sql("```sql\nSELECT COUNT(*) FROM deals;\n```"),
                         "SELECT COUNT(*) FROM deals")

    def test_no_query(self):
        self.assertIsNone(ag.UzexAgent._extract_sql("NO_QUERY"))
        self.assertIsNone(ag.UzexAgent._extract_sql("I cannot help. NO_QUERY"))

    def test_non_sql(self):
        self.assertIsNone(ag.UzexAgent._extract_sql("Here is your answer: 42"))


class TestRetryBackoff(unittest.TestCase):
    """Fix #1 — 503/429 must retry with backoff and fall back across models."""

    def test_retries_then_succeeds(self):
        llm = ag.LLM(api_key="fake", models=["m1", "m2"])
        calls = {"n": 0}

        class FakeModels:
            def generate_content(self, model, contents, config=None):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise Exception("503 UNAVAILABLE")
                class R:  # noqa: D401
                    text = "SELECT 1"
                return R()

        class FakeClient:
            models = FakeModels()

        llm._client = FakeClient()
        # patch sleep so the test is instant
        orig_sleep = ag.time.sleep
        ag.time.sleep = lambda *_: None
        try:
            out = llm.generate("prompt")
        finally:
            ag.time.sleep = orig_sleep
        self.assertEqual(out, "SELECT 1")
        self.assertEqual(calls["n"], 3)

    def test_non_retryable_raises_immediately(self):
        llm = ag.LLM(api_key="fake", models=["m1"])

        class FakeModels:
            def generate_content(self, **kw):
                raise Exception("400 API key not valid")

        class FakeClient:
            models = FakeModels()

        llm._client = FakeClient()
        with self.assertRaises(ag.LLMUnavailable):
            llm.generate("prompt")


class TestGeminiAgentFlow(unittest.TestCase):
    def setUp(self):
        self.agent = ag.UzexAgent()

    def test_gemini_sql_and_answer(self):
        fake = FakeLLM([
            "SELECT COUNT(*) AS deal_count FROM deals",      # SQL
            "Bazada jami 500 000 ta bitim mavjud.",            # answer
        ])
        self.agent.llm = fake
        a = self.agent.answer("Bazada jami nechta bitim mavjud?")
        self.assertEqual(a.engine, "gemini")
        self.assertIn("500 000", a.text)
        self.assertEqual(fake.calls, 2)

    def test_gemini_no_query_guard(self):
        # contains a year so it passes the local guard and reaches the LLM,
        # which then refuses with NO_QUERY (fix #3).
        fake = FakeLLM(["NO_QUERY"])
        self.agent.llm = fake
        a = self.agent.answer("2024-yilda ob-havo qanday bo'lgan?")
        self.assertTrue(a.off_topic)
        self.assertEqual(fake.calls, 1)

    def test_gemini_self_correction(self):
        fake = FakeLLM([
            "SELECT COUNT(*) FROM nonexistent_table",          # bad SQL
            "SELECT COUNT(*) AS deal_count FROM deals",        # corrected
            "Bazada 500 000 ta bitim.",                        # answer
        ])
        self.agent.llm = fake
        a = self.agent.answer("Bazada jami nechta bitim mavjud?")
        self.assertIn("500 000", a.text)
        self.assertGreaterEqual(fake.calls, 3)

    def test_gemini_unavailable_falls_back_local(self):
        fake = FakeLLM([ag.LLMUnavailable("503")])
        self.agent.llm = fake
        a = self.agent.answer("Bazada jami nechta bitim mavjud?")
        self.assertEqual(a.engine, "local")
        self.assertIn("500 000", a.text)

    def test_gemini_near_match(self):
        # The deterministic relaxation broadens the 2018 query automatically;
        # the LLM is then asked only to phrase the near-match answer.
        fake = FakeLLM([
            "SELECT COUNT(*) AS deal_count FROM deals WHERE deal_date LIKE '2018%'",  # 0 rows
            ("Aynan sizning shartlaringizga to'g'ri keladigan ma'lumot topilmadi, "
             "lekin shunga yaqin natijalar bor: bazada 466 281 ta bitim."),
        ])
        self.agent.llm = fake
        a = self.agent.answer("2018-yilda nechta bitim tuzilgan?")
        self.assertTrue(a.near_match)
        self.assertIn("topilmadi", a.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
