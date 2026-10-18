"""Private Telegram interactions for feedback, applications, resume and AI drafts."""
import io
import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

import ai_service
import career

router = Router()
_db_factory = None
_is_allowed = None


def configure(database_factory, allowed_function):
    global _db_factory, _is_allowed
    _db_factory = database_factory
    _is_allowed = allowed_function


def user_allowed(chat_id):
    return _is_allowed is not None and _is_allowed(chat_id)


def button(action, key, label):
    return InlineKeyboardButton(text=label, callback_data=f"jh:{action}:{key[:16]}")


def job_keyboard(job):
    key = job.key
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("like", key, "👍 Нравится"),
         button("dislike", key, "👎 Не подходит")],
        [button("saved", key, "📌 В избранное"),
         button("applied", key, "✅ Откликнулся")],
        [button("review", key, "🤖 Анализ"),
         button("cv", key, "📄 Резюме"),
         button("letter", key, "✉️ Письмо")],
    ])


def application_keyboard(job):
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("applied", job.key, "✅ Откликнулся"),
         button("hr", job.key, "📞 HR")],
        [button("interview", job.key, "🧠 Интервью"),
         button("offer", job.key, "🎉 Оффер")],
        [button("rejected", job.key, "❌ Отказ"),
         button("saved", job.key, "📌 Сохранено")],
    ])


async def send_long(message, content):
    # Telegram max message length is 4096 characters.
    body = content.strip()
    while body:
        part = body[:3400]
        if len(body) > 3400:
            cut = part.rfind("\n")
            if cut > 2000:
                part = part[:cut]
        await message.answer(part)
        body = body[len(part):].lstrip()


@router.callback_query(F.data.startswith("jh:"))
async def vacancy_action(query: CallbackQuery):
    msg = query.message
    if not msg or not user_allowed(msg.chat.id):
        await query.answer("Нет доступа", show_alert=True)
        return
    chunks = (query.data or "").split(":")
    if len(chunks) != 3:
        await query.answer("Неверная кнопка", show_alert=True)
        return
    action, prefix = chunks[1], chunks[2]
    with _db_factory() as db:
        job = career.lookup_job(db, prefix)
        if not job:
            await query.answer("Эта вакансия не найдена. Запусти /check.", show_alert=True)
            return
        if action in ("like", "dislike"):
            career.put_feedback(db, msg.chat.id, job, action)
            await query.answer("👍 Предпочтение сохранено" if action == "like"
                               else "👎 Учту: похожие вакансии будут ниже")
            return
        if action in career.APPLICATION_STATUSES:
            career.set_application(db, msg.chat.id, job, action)
            await query.answer("Статус: " + career.APPLICATION_STATUSES[action])
            return
        resume_info = career.resume_get(db, msg.chat.id)
        consent = career.consent_get(db, msg.chat.id)

    if action not in ("review", "cv", "letter"):
        await query.answer("Неизвестная команда", show_alert=True)
        return
    if not resume_info:
        await query.answer("Сначала загрузи своё резюме через /resume", show_alert=True)
        return
    await query.answer("Готовлю черновик…")
    original_resume = resume_info[1]

    used_ai = bool(consent and ai_service.api_ready())
    if action == "review":
        fallback = career.offline_comparison(job, original_resume)
    elif action == "letter":
        fallback = career.offline_cover(job, original_resume)
    else:
        fallback = original_resume
    result = fallback
    if used_ai:
        try:
            result = await ai_service.generate(
                {"review": "review", "letter": "letter", "cv": "resume"}[action],
                original_resume, job)
        except Exception as exc:
            logging.warning("AI action %s failed: %s", action, type(exc).__name__)
            used_ai = False
            await msg.answer("AI временно недоступен. Подготовлю локальный черновик без AI.")
    elif not ai_service.api_ready():
        await msg.answer("OPENAI_API_KEY ещё не настроен. Использую локальный вариант без AI.")
    elif not consent:
        await msg.answer("AI выключен. Для обработки резюме внешним API сначала введи /ai_on.")

    if action == "review":
        prefix_text = "🤖 AI-сравнение резюме и вакансии\n\n" if used_ai else ""
        await send_long(msg, prefix_text + result +
                        "\n\nВсе условия найма и требования проверяй у работодателя.")
    elif action == "letter":
        await send_long(msg, ("🤖 Черновик письма (AI)\n\n" if used_ai
                              else "✉️ Черновик письма без AI\n\n") + result)
    else:
        try:
            docx_bytes = career.make_docx(job.title, result)
            pdf_bytes = career.make_pdf(job.title, result)
            short = job.key[:10]
            await msg.answer_document(BufferedInputFile(docx_bytes, filename=f"resume_{short}.docx"),
                                      caption=("AI-адаптация" if used_ai else "Исходное резюме") +
                                      " — черновик. Проверь все факты и оформление.")
            await msg.answer_document(BufferedInputFile(pdf_bytes, filename=f"resume_{short}.pdf"),
                                      caption="PDF-черновик. Перед откликом проверь текст.")
        except Exception as exc:
            logging.error("Document generation failed: %s", type(exc).__name__)
            await msg.answer("Не удалось сформировать документы. Попробуй другое резюме или позже.")


@router.message(Command("resume"))
async def resume_help(message: Message):
    if not user_allowed(message.chat.id):
        return
    with _db_factory() as db:
        resume = career.resume_get(db, message.chat.id)
    if resume:
        await message.answer(
            f"Загружено: {resume[0]} ({len(resume[1])} символов).\n"
            "Чтобы заменить, отправь новый PDF/DOCX/TXT документ.\n"
            "/forget_resume — удалить резюме с сервера.\n"
            "/ai_on — разрешить отправку резюме и вакансии в OpenAI API.")
    else:
        await message.answer(
            "Отправь сюда файлом резюме в PDF, DOCX или TXT (до 5 МБ).\n"
            "Текст хранится в твоём приватном SQLite на Railway, НЕ в GitHub.\n"
            "По умолчанию внешняя AI-обработка выключена. Для неё нужны "
            "OPENAI_API_KEY в Railway и отдельное согласие /ai_on.\n"
            "После загрузки нажимай 🤖/📄/✉️ под вакансиями.")


@router.message(Command("forget_resume"))
async def resume_forget(message: Message):
    if not user_allowed(message.chat.id):
        return
    with _db_factory() as db:
        career.resume_remove(db, message.chat.id)
        career.consent_set(db, message.chat.id, False)
    await message.answer("Текст резюме удалён из рабочей базы. AI выключен. "
                         "Старые черновики в Telegram-чате при необходимости удали самостоятельно.")


@router.message(Command("ai_on"))
async def ai_enable(message: Message):
    if not user_allowed(message.chat.id):
        return
    with _db_factory() as db:
        career.consent_set(db, message.chat.id, True)
    if ai_service.api_ready():
        await message.answer("AI включён. При нажатии 🤖/📄/✉️ текст резюме и вакансии "
                             "будет отправлен в OpenAI API для обработки; запросы могут быть платными. "
                             "Отключение: /ai_off.")
    else:
        await message.answer("Согласие сохранено, но AI ещё не активен: "
                             "в Railway нет OPENAI_API_KEY. Пока работают локальные черновики. "
                             "Не отправляй ключ в Telegram или GitHub.")


@router.message(Command("ai_off"))
async def ai_disable(message: Message):
    if not user_allowed(message.chat.id):
        return
    with _db_factory() as db:
        career.consent_set(db, message.chat.id, False)
    await message.answer("AI отключён. Внешние запросы с твоим резюме больше не выполняются. "
                         "Локальные функции доступны.")


@router.message(Command("applications"))
async def applications(message: Message):
    if not user_allowed(message.chat.id):
        return
    with _db_factory() as db:
        rows = career.get_applications(db, message.chat.id, limit=15)
    if not rows:
        await message.answer("Пока нет сохранённых откликов. "
                             "Нажми 📌 или ✅ под любой вакансией.")
        return
    await message.answer(f"📋 Трекер откликов: {len(rows)} последних позиций. "
                         "Кнопками обновляй этапы:")
    for job, status in rows:
        await message.answer(
            f"{career.APPLICATION_STATUSES[status]}\n{job.title[:120]}\n"
            f"{job.company[:100]}\n{job.url}",
            reply_markup=application_keyboard(job), disable_web_page_preview=True)


@router.message(F.document)
async def upload_resume(message: Message):
    if not user_allowed(message.chat.id):
        return
    document = message.document
    filename = Path(document.file_name or "resume").name
    if document.file_size and document.file_size > 5 * 1024 * 1024:
        await message.answer("Файл больше 5 МБ. Уменьши его размер.")
        return
    if not filename.lower().endswith((".txt", ".pdf", ".docx")):
        await message.answer("Для резюме поддерживаются только PDF, DOCX и TXT.")
        return
    try:
        dest = io.BytesIO()
        await message.bot.download(document, destination=dest)
        content = career.parse_resume(filename, dest.getvalue())
        with _db_factory() as db:
            career.resume_save(db, message.chat.id, filename, content)
        await message.answer("✅ Резюме сохранено в приватной базе Railway. "
                             "Теперь кнопки 🤖 Анализ, 📄 Резюме и ✉️ Письмо работают. "
                             "Без OpenAI API доступна локальная версия. /ai_on — согласие на AI.")
    except ValueError as exc:
        await message.answer("Не удалось принять резюме: " + str(exc))
    except Exception as exc:
        logging.warning("Resume upload failed: %s", type(exc).__name__)
        await message.answer("Не получилось прочитать файл. Попробуй PDF с текстом, DOCX или TXT.")
