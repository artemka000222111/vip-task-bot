import asyncio
import logging
import os
from decimal import Decimal

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

dp = Dispatcher()
db_pool: asyncpg.Pool | None = None


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Доступные задания")],
            [KeyboardButton(text="➕ Создать задание")],
            [
                KeyboardButton(text="💰 Баланс"),
                KeyboardButton(text="👤 Профиль"),
            ],
            [KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


async def init_database() -> None:
    global db_pool

    if not DATABASE_URL:
        raise RuntimeError(
            "Не задан DATABASE_URL. Добавьте ссылку на PostgreSQL в Variables Railway."
        )

    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    async with db_pool.acquire() as connection:
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL DEFAULT '',
                balance NUMERIC(12, 2) NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

    logging.info("PostgreSQL подключён, таблица users проверена.")


async def register_user(message: Message) -> None:
    if message.from_user is None or db_pool is None:
        return

    user = message.from_user

    async with db_pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO users (telegram_id, username, first_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (telegram_id) DO UPDATE
            SET username = EXCLUDED.username,
                first_name = EXCLUDED.first_name
            """,
            user.id,
            user.username,
            user.first_name or "",
        )


@dp.message(CommandStart())
async def command_start(message: Message) -> None:
    await register_user(message)
    await message.answer(
        "Добро пожаловать в VIP TASK!\n\n"
        "Здесь можно выполнять задания и размещать собственные.\n"
        "Выберите нужный раздел в меню ниже.",
        reply_markup=main_keyboard(),
    )


@dp.message(F.text == "📋 Доступные задания")
async def available_tasks(message: Message) -> None:
    await message.answer("Пока доступных заданий нет. Раздел находится в разработке.")


@dp.message(F.text == "➕ Создать задание")
async def create_task(message: Message) -> None:
    await message.answer("Создание заданий скоро станет доступно.")


@dp.message(F.text == "💰 Баланс")
async def balance(message: Message) -> None:
    if message.from_user is None or db_pool is None:
        await message.answer("Не удалось загрузить баланс. Попробуйте позже.")
        return

    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT balance FROM users WHERE telegram_id = $1",
            message.from_user.id,
        )

    if row is None:
        await register_user(message)
        amount = Decimal("0.00")
    else:
        amount = row["balance"]

    await message.answer(
        f"💰 Ваш баланс: {amount:.2f} ₽\n\n"
        "Пополнение и вывод средств будут подключены следующим этапом."
    )


@dp.message(F.text == "👤 Профиль")
async def profile(message: Message) -> None:
    if message.from_user is None or db_pool is None:
        await message.answer("Не удалось загрузить профиль. Попробуйте позже.")
        return

    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT telegram_id, username, first_name, created_at
            FROM users
            WHERE telegram_id = $1
            """,
            message.from_user.id,
        )

    if row is None:
        await register_user(message)
        row = await connection.fetchrow(
            """
            SELECT telegram_id, username, first_name, created_at
            FROM users
            WHERE telegram_id = $1
            """,
            message.from_user.id,
        )

    username = f"@{row['username']}" if row["username"] else "не указан"
    joined = row["created_at"].strftime("%d.%m.%Y")

    await message.answer(
        "👤 Ваш профиль\n\n"
        f"Имя: {row['first_name']}\n"
        f"Username: {username}\n"
        f"ID: {row['telegram_id']}\n"
        f"Дата регистрации: {joined}"
    )


@dp.message(F.text == "ℹ️ Помощь")
async def help_message(message: Message) -> None:
    await message.answer(
        "VIP TASK — платформа заданий.\n"
        "Допустимые категории: социальные сети, Яндекс Браузер и Яндекс Карты."
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
