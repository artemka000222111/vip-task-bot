import asyncio
import logging
import os
from urllib.parse import urlparse

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID_RAW = os.getenv("ADMIN_ID")
ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW and ADMIN_ID_RAW.isdigit() else None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("vip-nobody")

dp = Dispatcher(storage=MemoryStorage())
db_pool: asyncpg.Pool | None = None

CATEGORIES = {
    "love": "❤️ Признание",
    "compliment": "😍 Комплимент",
    "question": "❓ Вопрос",
    "joke": "😂 Прикол",
    "other": "💬 Другое",
}


class AnonymousFlow(StatesGroup):
    category = State()
    text = State()


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📩 Входящие"), KeyboardButton(text="🔗 Моя ссылка")],
            [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


def category_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=CATEGORIES["love"], callback_data="category:love"),
            InlineKeyboardButton(text=CATEGORIES["compliment"], callback_data="category:compliment"),
        ],
        [
            InlineKeyboardButton(text=CATEGORIES["question"], callback_data="category:question"),
            InlineKeyboardButton(text=CATEGORIES["joke"], callback_data="category:joke"),
        ],
        [InlineKeyboardButton(text=CATEGORIES["other"], callback_data="category:other")],
    ])


async def init_database() -> None:
    global db_pool
    if not DATABASE_URL:
        raise RuntimeError("Не задана переменная DATABASE_URL в Railway.")

    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS nobody_users (
                telegram_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT,
                balance NUMERIC(12,2) NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS nobody_messages (
                id BIGSERIAL PRIMARY KEY,
                recipient_id BIGINT NOT NULL REFERENCES nobody_users(telegram_id),
                sender_id BIGINT NOT NULL REFERENCES nobody_users(telegram_id),
                category TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                is_read BOOLEAN NOT NULL DEFAULT FALSE,
                revealed BOOLEAN NOT NULL DEFAULT FALSE
            )
        """)
    log.info("VIP NOBODY database is ready")


async def save_user(user) -> None:
    if db_pool is None or user is None:
        return
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO nobody_users (telegram_id, username, first_name, last_name)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (telegram_id) DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name
        """, user.id, user.username, user.first_name or "", user.last_name)


async def get_bot_username(bot: Bot) -> str:
    me = await bot.get_me()
    return me.username or ""


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    if message.from_user is None:
        return
    await save_user(message.from_user)

    payload = ""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2:
        payload = parts[1].strip()

    if payload.startswith("to_"):
        try:
            recipient_id = int(payload[3:])
        except ValueError:
            recipient_id = 0
        if recipient_id == message.from_user.id:
            await message.answer("Нельзя отправить анонимное сообщение самому себе.")
            return
        if db_pool is None:
            await message.answer("Сервис временно недоступен. Попробуйте позже.")
            return
        async with db_pool.acquire() as conn:
            recipient = await conn.fetchrow(
                "SELECT telegram_id FROM nobody_users WHERE telegram_id = $1",
                recipient_id,
            )
        if recipient is None:
            await message.answer("Не удалось найти получателя. Попросите его сначала запустить бота.")
            return
        await state.update_data(recipient_id=recipient_id)
        await state.set_state(AnonymousFlow.category)
        await message.answer(
            "Вы отправляете анонимное сообщение. Сначала выберите категорию.\n\n"
            "Важно: сервис хранит технические данные отправителя. Получатель сможет "
            "раскрыть доступные данные профиля за 50 ₽. Не отправляйте то, что не готовы раскрыть.",
            reply_markup=category_keyboard(),
        )
        return

    await message.answer(
        "👋 Добро пожаловать в VIP NOBODY!\n\n"
        "Здесь тебе могут отправлять анонимные сообщения. "
        "Поделись своей ссылкой, чтобы получить первую анонимку.",
        reply_markup=main_keyboard(),
    )


@dp.callback_query(F.data.startswith("category:"))
async def choose_category(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 1)[1]
    if key not in CATEGORIES:
        await callback.answer("Неизвестная категория", show_alert=True)
        return
    await state.update_data(category=key)
    await state.set_state(AnonymousFlow.text)
    await callback.answer()
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"Категория: {CATEGORIES[key]}\n\nТеперь напишите сообщение. "
            "Своё фото загрузить нельзя. На следующем этапе добавим выбор фирменных картинок."
        )


@dp.message(AnonymousFlow.text)
async def receive_anonymous_text(message: Message, state: FSMContext, bot: Bot) -> None:
    body = (message.text or "").strip()
    if not body:
        await message.answer("Пожалуйста, отправьте текстовое сообщение.")
        return
    if len(body) > 2000:
        await message.answer("Сообщение слишком длинное. Максимум — 2000 символов.")
        return
    data = await state.get_data()
    sender = message.from_user
    recipient_id = data.get("recipient_id")
    category = data.get("category")
    if sender is None or recipient_id is None or category not in CATEGORIES or db_pool is None:
        await state.clear()
        await message.answer("Не удалось отправить сообщение. Начните заново по ссылке.")
        return

    await save_user(sender)
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO nobody_messages (recipient_id, sender_id, category, body)
                VALUES ($1, $2, $3, $4)
                RETURNING id
            """, recipient_id, sender.id, category, body)
        message_id = row["id"]
    except Exception:
        log.exception("Could not save anonymous message")
        await message.answer("Не получилось отправить сообщение. Попробуйте позже.")
        return

    await state.clear()
    await message.answer("✅ Анонимное сообщение отправлено!", reply_markup=main_keyboard())
    try:
        await bot.send_message(
            recipient_id,
            f"💌 Новая анонимка!\n\n{CATEGORIES[category]}\n\n{body}\n\n"
            f"Номер сообщения: #{message_id}\n"
            "Раскрытие личности для этого сообщения — 50 ₽ (функция скоро появится).",
        )
    except Exception:
        log.warning("Could not notify recipient %s", recipient_id)


@dp.message(F.text == "🔗 Моя ссылка")
async def my_link(message: Message, bot: Bot) -> None:
    if message.from_user is None:
        return
    username = await get_bot_username(bot)
    if not username:
        await message.answer("Не удалось получить ссылку бота.")
        return
    link = f"https://t.me/{username}?start=to_{message.from_user.id}"
    await message.answer(
        "🔗 Твоя персональная ссылка:\n"
        f"{link}\n\n"
        "Скопируй её и отправь друзьям или размести в соцсетях."
    )


@dp.message(F.text == "📩 Входящие")
async def inbox(message: Message) -> None:
    if message.from_user is None or db_pool is None:
        await message.answer("Не удалось загрузить сообщения.")
        return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, category, body, created_at
            FROM nobody_messages
            WHERE recipient_id = $1
            ORDER BY created_at DESC
            LIMIT 10
        """, message.from_user.id)
        await conn.execute(
            "UPDATE nobody_messages SET is_read = TRUE WHERE recipient_id = $1",
            message.from_user.id,
        )
    if not rows:
        await message.answer("📭 Пока сообщений нет. Поделись своей ссылкой!")
        return
    for row in rows:
        await message.answer(
            f"💌 Анонимное сообщение #{row['id']}\n"
            f"{CATEGORIES.get(row['category'], '💬 Другое')}\n\n"
            f"{row['body']}\n\n"
            "Раскрытие личности — 50 ₽ (скоро)."
        )


@dp.message(F.text == "💰 Баланс")
async def balance(message: Message) -> None:
    if message.from_user is None or db_pool is None:
        await message.answer("Баланс пока недоступен.")
        return
    async with db_pool.acquire() as conn:
        amount = await conn.fetchval(
            "SELECT balance FROM nobody_users WHERE telegram_id = $1",
            message.from_user.id,
        )
    await message.answer(f"💰 Твой баланс: {amount or Decimal('0.00')} ₽\n\nПополнение подключим следующим этапом.")


@dp.message(F.text == "👤 Профиль")
async def profile(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    username = f"@{user.username}" if user.username else "не указан"
    await message.answer(
        "👤 Профиль VIP NOBODY\n\n"
        f"Имя: {user.first_name or 'не указано'}\n"
        f"Username: {username}\n"
        f"Telegram ID: {user.id}"
    )


@dp.message(F.text == "⚙️ Настройки")
async def settings(message: Message) -> None:
    await message.answer("⚙️ Настройки\n\nДополнительные настройки добавим позже.")


@dp.message(F.text == "ℹ️ Помощь")
async def help_message(message: Message) -> None:
    await message.answer(
        "ℹ️ VIP NOBODY\n\n"
        "1. Нажми «Моя ссылка» и поделись ею.\n"
        "2. Другой человек перейдёт по ссылке и отправит анонимное сообщение.\n"
        "3. Сообщения появятся в разделе «Входящие».\n\n"
        "Обычная отправка бесплатная. Раскрытие личности планируется за 50 ₽. "
        "Анонимность не абсолютная: технические данные отправителя сохраняются."
    )


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN в переменных Railway.")
    await init_database()
    bot = Bot(token=BOT_TOKEN)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        if db_pool is not None:
            await db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
