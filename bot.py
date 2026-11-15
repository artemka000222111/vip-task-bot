import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Доступные задания")],
            [KeyboardButton(text="➕ Создать задание")],
            [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


dp = Dispatcher()


@dp.message(CommandStart())
async def command_start(message: Message) -> None:
    await message.answer(
        "Добро пожаловать в VIP TASK!\n\n"
        "Здесь можно выполнять задания и размещать собственные.\n"
        "Выберите нужный раздел в меню ниже.",
        reply_markup=main_keyboard(),
    )


@dp.message(F.text == "📋 Доступные задания")
async def available_tasks(message: Message) -> None:
    await message.answer("Раздел заданий готовится.")


@dp.message(F.text == "➕ Создать задание")
async def create_task(message: Message) -> None:
    await message.answer("Создание заданий готовится.")


@dp.message(F.text == "💰 Баланс")
async def balance(message: Message) -> None:
    await message.answer("Раздел баланса готовится.")


@dp.message(F.text == "👤 Профиль")
async def profile(message: Message) -> None:
    await message.answer("Раздел профиля готовится.")


@dp.message(F.text == "ℹ️ Помощь")
async def help_message(message: Message) -> None:
    await message.answer(
        "VIP TASK — платформа заданий.\n"
        "Допустимые категории: социальные сети, Яндекс Браузер и Яндекс Карты."
    )


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не задан BOT_TOKEN. Создайте файл .env и добавьте BOT_TOKEN=токен_бота"
        )

    bot = Bot(token=BOT_TOKEN)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
