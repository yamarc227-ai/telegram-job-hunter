"""Regression tests for personal tracker, resume and optional AI integrations.

No Telegram credentials or external API requests are required to run these.
"""
import io
import os
import tempfile
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import sqlite3
import unittest
from dataclasses import replace

from docx import Document
from pypdf import PdfReader

import ai_service
import bot_features
import career
from sources import Job


class PrivateCareerData(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        career.init_tables(self.db)
        self.chat = 12345
        self.job = Job("HH", "Product Analyst", "Example",
                       "https://example.com/vacancies/123",
                       "SQL, Python, retention, cohort analysis, GMV", remote=True,
                       remote_scope="worldwide")

    def tearDown(self):
        self.db.close()

    def test_snapshots_and_callbacks_are_bounded(self):
        career.store_job(self.db, self.job)
        self.assertEqual(career.lookup_job(self.db, self.job.key[:16]).title, self.job.title)
        self.assertIsNone(career.lookup_job(self.db, "../secret"))
        self.assertIsNone(career.lookup_job(self.db, self.job.key[:14]))
        keyboard = bot_features.job_keyboard(self.job)
        buttons = [item for row in keyboard.inline_keyboard for item in row]
        self.assertEqual(len(buttons), 7)
        self.assertTrue(all(len(btn.callback_data) <= 64 for btn in buttons))

    def test_feedback_changes_future_topic_priority(self):
        good = replace(self.job, url="https://example.com/vacancies/124",
                       title="Game Product Analyst")
        career.put_feedback(self.db, self.chat, good, "like")
        related = replace(self.job, title="Product Analyst at Game Studio",
                          url="https://example.com/vacancies/125")
        self.assertGreater(career.feedback_bonus(related, self.db, self.chat), 0)
        self.assertEqual(career.get_feedback(self.db, self.chat)[good.key], "like")
        career.put_feedback(self.db, self.chat, good, "dislike")
        self.assertLess(career.feedback_bonus(related, self.db, self.chat), 0)
        self.assertEqual(career.feedback_bonus(self.job, self.db, self.chat + 1), 0)

    def test_application_statuses_and_persistence(self):
        career.set_application(self.db, self.chat, self.job, "saved")
        career.set_application(self.db, self.chat, self.job, "applied")
        rows = career.get_applications(self.db, self.chat)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "applied")
        self.assertEqual(rows[0][0].url, self.job.url)
        self.assertEqual(career.get_applications(self.db, self.chat + 10), [])
        with self.assertRaises(ValueError):
            career.set_application(self.db, self.chat, self.job, "transferred_to_hr_system")

    def test_store_and_delete_resume(self):
        content = ("Кандидат: Иван Петров. Должность: Бизнес аналитик.\n"
                   "Ozon Fresh. Работаю с SQL, Python, продуктовыми метриками.\n"
                   "Создаю отчёты для бизнеса и изучаю пользовательские воронки.\n" * 2)
        career.resume_save(self.db, self.chat, "resume.pdf", content)
        self.assertEqual(career.resume_get(self.db, self.chat), ("resume.pdf", content))
        career.consent_set(self.db, self.chat, True)
        self.assertTrue(career.consent_get(self.db, self.chat))
        career.resume_remove(self.db, self.chat)
        self.assertIsNone(career.resume_get(self.db, self.chat))
        career.consent_set(self.db, self.chat, False)
        self.assertFalse(career.consent_get(self.db, self.chat))

    def test_pdf_word_text_upload_and_documents(self):
        text = ("ИВАН ПЕТРОВ\nБИЗНЕС-АНАЛИТИК\nОпыт работы в розничной аналитике. "
                "Создаю SQL-запросы, Python-отчёты, когортный анализ и визуализации.\n" * 3)
        document = Document()
        document.add_paragraph(text)
        mem = io.BytesIO()
        document.save(mem)
        self.assertIn("SQL", career.parse_resume("resume.docx", mem.getvalue()))
        self.assertIn("Python", career.parse_resume("resume.txt", text.encode()))
        word = career.make_docx("Product Analyst", text)
        output_pdf = career.make_pdf("Product Analyst", text)
        self.assertTrue(word.startswith(b"PK"))
        self.assertTrue(output_pdf.startswith(b"%PDF"))
        self.assertTrue("SQL" in (PdfReader(io.BytesIO(output_pdf)).pages[0].extract_text() or ""))

    def test_offline_no_invented_claims(self):
        resume = ("Я работаю аналитиком. Изучаю SQL и Excel. "
                  "Готовлю отчёты, использую SQL в запросах. " * 3)
        review = career.offline_comparison(self.job, resume)
        self.assertIn("не подтверждены резюме", review)
        self.assertIn("Python", review)
        self.assertIn("не AI", review)
        letter = career.offline_cover(self.job, resume)
        self.assertIn("SQL", letter)
        self.assertNotIn("Python", letter)
        self.assertIn("Черновик", letter)

    def test_only_allowed_resume_types(self):
        with self.assertRaises(ValueError):
            career.parse_resume("resume.exe", b"fake")
        with self.assertRaises(ValueError):
            career.parse_resume("resume.pdf", b"not a PDF")

    def test_api_output_parser(self):
        response = {"output": [{"content": [{"type": "output_text", "text": "Готовый результат"}]}]}
        self.assertEqual(ai_service.response_text(response), "Готовый результат")
        with self.assertRaises(RuntimeError):
            ai_service.response_text({"output": []})


class TelegramActions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "test.sqlite3"
        @contextmanager
        def database():
            db = sqlite3.connect(self.db_path)
            try:
                with db:
                    yield db
            finally:
                db.close()
        self.db = database
        bot_features.configure(self.db, lambda chat_id: chat_id == 12345)
        self.job = Job("HH", "Product Analyst", "Example",
                       "https://example.com/jobs/223", "SQL, Python, retention",
                       remote=True)
        with self.db() as conn:
            career.init_tables(conn)
            career.store_job(conn, self.job)
        self.message = SimpleNamespace(chat=SimpleNamespace(id=12345),
                                       answer=AsyncMock(), answer_document=AsyncMock())

    async def asyncTearDown(self):
        self.temp.cleanup()

    def callback(self, action, chat_id=12345):
        message = SimpleNamespace(chat=SimpleNamespace(id=chat_id),
                                  answer=AsyncMock(), answer_document=AsyncMock())
        return SimpleNamespace(message=message, data=f"jh:{action}:{self.job.key[:16]}",
                               answer=AsyncMock())

    async def test_buttons_store_like_and_application(self):
        q = self.callback("like")
        await bot_features.vacancy_action(q)
        q.answer.assert_awaited_once()
        with self.db() as conn:
            self.assertEqual(career.get_feedback(conn, 12345)[self.job.key], "like")
        q = self.callback("interview")
        await bot_features.vacancy_action(q)
        with self.db() as conn:
            self.assertEqual(career.get_applications(conn, 12345)[0][1], "interview")

    async def test_chat_authentication_applies_to_buttons(self):
        q = self.callback("like", chat_id=99999)
        await bot_features.vacancy_action(q)
        with self.db() as conn:
            self.assertEqual(career.get_feedback(conn, 12345), {})

    async def test_no_ai_call_without_key_and_consent(self):
        resume = ("Работал аналитиком, использовал SQL и Excel, "
                  "занимался отчётностью и продуктовой аналитикой. " * 3)
        with self.db() as conn:
            career.resume_save(conn, 12345, "resume.txt", resume)
            career.consent_set(conn, 12345, False)
        q = self.callback("review")
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            with patch.object(ai_service, "generate", AsyncMock(side_effect=AssertionError("AI called"))):
                await bot_features.vacancy_action(q)
        self.assertGreaterEqual(q.message.answer.await_count, 1)
        self.assertIn("SQL", str(q.message.answer.await_args.args[0]))

    async def test_upload_resume_and_forget(self):
        candidate = ("Я аналитик, работаю с SQL и Python. "
                     "Изучаю пользовательские метрики и делаю отчёты. " * 3)
        async def fake_download(document, destination):
            destination.write(candidate.encode("utf-8"))
        msg = SimpleNamespace(chat=SimpleNamespace(id=12345),
                              document=SimpleNamespace(file_name="sample.txt", file_size=500),
                              bot=SimpleNamespace(download=AsyncMock(side_effect=fake_download)),
                              answer=AsyncMock())
        await bot_features.upload_resume(msg)
        with self.db() as conn:
            self.assertIsNotNone(career.resume_get(conn, 12345))
        await bot_features.resume_forget(msg)
        with self.db() as conn:
            self.assertIsNone(career.resume_get(conn, 12345))


if __name__ == "__main__":
    unittest.main()
