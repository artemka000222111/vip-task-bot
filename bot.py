import asyncio
import logging
import os
from decimal import Decimal, InvalidOperation

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
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID_RAW = os.getenv("ADMIN_ID")
ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW and ADMIN_ID_RAW.isdigit() else None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

dp = Dispatcher(storage=MemoryStorage())
db_pool: asyncpg.Pool | None = None

CANCEL_TEXT = "❌ Отменить"
SKIP_TEXT = "Пропустить"


class CreateTask(StatesGroup):
    description = State()
    link = State()
    slots = State()
    reward = State()
    confirm = State()


class AdminDecision(StatesGroup):
    reason = State()


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Доступные задания")],
            [KeyboardButton(text="➕ Создать задание")],
            [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="📚 История заказов")],
            [KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=CANCEL_TEXT)]],
        resize_keyboard=True,
    )


def skip_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=SKIP_TEXT)],
            [KeyboardButton(text=CANCEL_TEXT)],
        ],
        resize_keyboard=True,
    )


def confirm_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Отправить на модерацию")],
            [KeyboardButton(text=CANCEL_TEXT)],
        ],
        resize_keyboard=True,
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
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id BIGSERIAL PRIMARY KEY,
                creator_id BIGINT NOT NULL REFERENCES users(telegram_id),
                category TEXT NOT NULL,
                description TEXT NOT NULL,
                task_link TEXT,
                slots INTEGER NOT NULL CHECK (slots > 0),
                reward NUMERIC(12, 2) NOT NULL CHECK (reward >= 1),
                total_cost NUMERIC(14, 2) NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending_moderation',
                admin_reason TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                reviewed_at TIMESTAMPTZ
            )
            """
        )

    logging.info("PostgreSQL подключён, таблицы users и tasks проверены.")
    if ADMIN_ID is None:
        logging.warning("ADMIN_ID не задан. Заявки будут сохраняться, но уведомление админу не отправится.")


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


async def send_task_to_admin(bot: Bot, task_id: int, task: dict) -> None:
    if ADMIN_ID is None:
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Принять", callback_data=f"task:approve:{task_id}"),
                InlineKeyboardButton(text="❌ Отказать", callback_data=f"task:reject:{task_id}"),
            ],
            [
                InlineKeyboardButton(
                    text="📝 На доработку",
                    callback_data=f"task:revision:{task_id}",
                )
            ],
        ]
    )
    link_text = task["task_link"] or "Не требуется"
    await bot.send_message(
        ADMIN_ID,
        (
            f"🆕 Заказ #{task_id} — новая заявка на модерацию\n\n"
            f"Описание: {task['description']}\n"
            f"Ссылка: {link_text}\n"
            f"Исполнителей: {task['slots']}\n"
            f"Оплата одному: {task['reward']:.2f} ₽\n"
            f"Общая стоимость: {task['total_cost']:.2f} ₽\n"
            f"Заказчик Telegram ID: {task['creator_id']}\n\n"
            "Проверьте, что заказ описывает реальную услугу и не предполагает "
            "обмана, фиктивных отзывов или искусственного изменения показателей."
        ),
        reply_markup=keyboard,
    )


@dp.message(CommandStart())
async def command_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await register_user(message)
    await message.answer(
        "Добро пожаловать в VIP TASK!\n\n"
        "Здесь можно размещать допустимые задания и выполнять их.\n"
        "Выберите нужный раздел в меню ниже.",
        reply_markup=main_keyboard(),
    )


@dp.message(F.text == CANCEL_TEXT)
async def cancel_any_flow(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Сейчас нечего отменять.", reply_markup=main_keyboard())
        return
    await state.clear()
    await message.answer("Действие отменено. Деньги не списывались.", reply_markup=main_keyboard())


@dp.message(F.text == "📋 Доступные задания")
async def available_tasks(message: Message) -> None:
    if db_pool is None:
        await message.answer("База данных пока недоступна.")
        return

    async with db_pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT id, description, slots, reward
            FROM tasks
            WHERE status = 'approved'
            ORDER BY created_at DESC
            LIMIT 10
            """
        )

    if not rows:
        await message.answer("Пока опубликованных заданий нет.")
        return

    lines = ["📋 Одобренные задания:"]
    for row in rows:
        lines.append(
            f"\nЗаказ #{row['id']}\n"
            f"Описание: {row['description']}\n"
            f"Исполнителей требуется: {row['slots']}\n"
            f"Оплата: {row['reward']:.2f} ₽"
        )
    await message.answer("\n".join(lines))


@dp.message(F.text == "➕ Создать задание")
async def create_task(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CreateTask.description)
    await message.answer(
        "Опишите задание своими словами: что нужно сделать и какой результат "
        "должен предоставить исполнитель. Размещайте только реальные услуги; "
        "накрутка и фиктивные отзывы запрещены.",
        reply_markup=cancel_keyboard(),
    )


@dp.message(CreateTask.description)
async def task_description(message: Message, state: FSMContext) -> None:
    description = (message.text or "").strip()
    if len(description) < 10:
        await message.answer("Описание слишком короткое. Напишите не менее 10 символов.")
        return
    if len(description) > 3000:
        await message.answer("Описание слишком длинное. Максимум — 3000 символов.")
        return
    await state.update_data(description=description)
    await state.set_state(CreateTask.link)
    await message.answer(
        "Отправьте ссылку, необходимую для выполнения задания, "
        "или нажмите «Пропустить».",
        reply_markup=skip_keyboard(),
    )


@dp.message(CreateTask.link)
async def task_link(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if value == SKIP_TEXT:
        link = None
    else:
        if len(value) > 1000 or not value.startswith(("https://", "http://")):
            await message.answer("Отправьте полную ссылку, начинающуюся с https:// или http://, либо нажмите «Пропустить».")
            return
        link = value
    await state.update_data(task_link=link)
    await state.set_state(CreateTask.slots)
    await message.answer(
        "Сколько исполнителей должно выполнить задание? Введите целое число от 1 до 10000.",
        reply_markup=cancel_keyboard(),
    )


@dp.message(CreateTask.slots)
async def task_slots(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not value.isdigit() or not 1 <= int(value) <= 10000:
        await message.answer("Введите целое число от 1 до 10000.")
        return
    await state.update_data(slots=int(value))
    await state.set_state(CreateTask.reward)
    await message.answer(
        "Сколько рублей получит один исполнитель? Минимум — 1 ₽. "
        "Можно указать копейки, например 12.50.",
        reply_markup=cancel_keyboard(),
    )


@dp.message(CreateTask.reward)
async def task_reward(message: Message, state: FSMContext) -> None:
    try:
        reward = Decimal((message.text or "").strip().replace(",", ".")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        await message.answer("Введите сумму числом, например 15 или 12.50.")
        return

    if not reward.is_finite() or reward < Decimal("1.00") or reward > Decimal("100000.00"):
        await message.answer("Сумма должна быть от 1 ₽ до 100000 ₽ за одного исполнителя.")
        return

    data = await state.get_data()
    total_cost = reward * data["slots"]
    await state.update_data(reward=str(reward), total_cost=str(total_cost))
    await state.set_state(CreateTask.confirm)

    link_text = data.get("task_link") or "Не требуется"
    await message.answer(
        "📋 Проверьте заказ перед отправкой\n\n"
        f"Описание: {data['description']}\n"
        f"Ссылка: {link_text}\n"
        f"Исполнителей: {data['slots']}\n"
        f"Оплата одному: {reward:.2f} ₽\n"
        f"Общая стоимость: {total_cost:.2f} ₽\n\n"
        "После отправки заказ попадёт администратору на модерацию. "
        "На этом этапе деньги не списываются.",
        reply_markup=confirm_keyboard(),
    )


@dp.message(CreateTask.confirm)
async def task_confirm(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.text != "✅ Отправить на модерацию":
        await message.answer("Нажмите «✅ Отправить на модерацию» или отмените создание.")
        return
    if message.from_user is None or db_pool is None:
        await message.answer("Не удалось сохранить заказ. Попробуйте позже.")
        return

    data = await state.get_data()
    reward = Decimal(data["reward"])
    total_cost = Decimal(data["total_cost"])

    async with db_pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO users (telegram_id, username, first_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (telegram_id) DO NOTHING
            """,
            message.from_user.id,
            message.from_user.username,
            message.from_user.first_name or "",
        )
        row = await connection.fetchrow(
            """
            INSERT INTO tasks
                (creator_id, category, description, task_link, slots, reward, total_cost)
            VALUES ($1, 'Свободное описание', $2, $3, $4, $5, $6)
            RETURNING id
            """,
            message.from_user.id,
            data["description"],
            data.get("task_link"),
            data["slots"],
            reward,
            total_cost,
        )

    task_id = row["id"]
    await state.clear()
    await message.answer(
        f"✅ Заказ #{task_id} отправлен на модерацию!\n\n"
        "Администратор проверит его и пришлёт решение. "
        "Деньги пока не списаны.",
        reply_markup=main_keyboard(),
    )

    task_for_admin = {
        "description": data["description"],
        "task_link": data.get("task_link"),
        "slots": data["slots"],
        "reward": reward,
        "total_cost": total_cost,
        "creator_id": message.from_user.id,
    }
    try:
        await send_task_to_admin(bot, task_id, task_for_admin)
    except Exception:
        logging.exception("Не удалось отправить заказ админу: #%s", task_id)
        await message.answer(
            "Заявка сохранена, но уведомление администратору не отправилось. "
            "Проверьте настройку ADMIN_ID в Railway."
        )


async def begin_admin_decision(
    callback: CallbackQuery,
    state: FSMContext,
    task_id: int,
    decision: str,
) -> None:
    if callback.from_user is None or ADMIN_ID is None or callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    if db_pool is None:
        await callback.answer("База данных недоступна.", show_alert=True)
        return

    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT id, status FROM tasks WHERE id = $1",
            task_id,
        )
    if row is None or row["status"] != "pending_moderation":
        await callback.answer("Эта заявка уже обработана или не найдена.", show_alert=True)
        return

    await state.set_state(AdminDecision.reason)
    await state.update_data(task_id=task_id, decision=decision)
    prompt = "Напишите причину отказа для заказчика:" if decision == "rejected" else "Напишите, что заказчику нужно исправить:"
    await callback.answer()
    if callback.message:
        await callback.message.answer(f"Заказ #{task_id}. {prompt}")


@dp.callback_query(F.data.startswith("task:approve:"))
async def admin_approve(callback: CallbackQuery) -> None:
    if callback.from_user is None or ADMIN_ID is None or callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    task_id = int(callback.data.rsplit(":", 1)[1])
    if db_pool is None:
        await callback.answer("База данных недоступна.", show_alert=True)
        return
    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            UPDATE tasks SET status = 'approved', reviewed_at = NOW()
            WHERE id = $1 AND status = 'pending_moderation'
            RETURNING creator_id
            """,
            task_id,
        )
    if row is None:
        await callback.answer("Заявка уже обработана или не найдена.", show_alert=True)
        return
    await callback.answer("Заказ одобрен.")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"✅ Заказ #{task_id} одобрен.")
    await callback.bot.send_message(
        row["creator_id"],
        f"✅ Ваш заказ #{task_id} одобрен модератором. "
        "Следующим этапом подключим резервирование оплаты и публикацию для исполнителей.",
    )


@dp.callback_query(F.data.startswith("task:reject:"))
async def admin_reject(callback: CallbackQuery, state: FSMContext) -> None:
    task_id = int(callback.data.rsplit(":", 1)[1])
    await begin_admin_decision(callback, state, task_id, "rejected")


@dp.callback_query(F.data.startswith("task:revision:"))
async def admin_revision(callback: CallbackQuery, state: FSMContext) -> None:
    task_id = int(callback.data.rsplit(":", 1)[1])
    await begin_admin_decision(callback, state, task_id, "revision")


@dp.message(AdminDecision.reason)
async def admin_reason(message: Message, state: FSMContext) -> None:
    if message.from_user is None or ADMIN_ID is None or message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    reason = (message.text or "").strip()
    if len(reason) < 3:
        await message.answer("Напишите причину подробнее (минимум 3 символа).")
        return
    if len(reason) > 1500:
        await message.answer("Слишком длинный текст. Максимум — 1500 символов.")
        return

    data = await state.get_data()
    task_id = data["task_id"]
    decision = data["decision"]
    if db_pool is None:
        await message.answer("База данных недоступна. Попробуйте позже.")
        return

    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            UPDATE tasks
            SET status = $2, admin_reason = $3, reviewed_at = NOW()
            WHERE id = $1 AND status = 'pending_moderation'
            RETURNING creator_id
            """,
            task_id,
            decision,
            reason,
        )
    await state.clear()
    if row is None:
        await message.answer("Заявка уже обработана или не найдена.", reply_markup=main_keyboard())
        return

    if decision == "rejected":
        await message.answer(f"❌ Заказ #{task_id} отклонён. Причина отправлена заказчику.")
        user_text = f"❌ Ваш заказ #{task_id} отклонён.\nПричина: {reason}"
    else:
        await message.answer(f"📝 Заказ #{task_id} отправлен на доработку.")
        user_text = (
            f"📝 Ваш заказ #{task_id} отправлен на доработку.\n"
            f"Комментарий администратора: {reason}\n\n"
            "Исправьте описание и отправьте заказ повторно через создание задания."
        )
    await message.bot.send_message(row["creator_id"], user_text)


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
            user = message.from_user
            await connection.execute(
                """
                INSERT INTO users (telegram_id, username, first_name)
                VALUES ($1, $2, $3)
                ON CONFLICT (telegram_id) DO NOTHING
                """,
                user.id,
                user.username,
                user.first_name or "",
            )
            row = await connection.fetchrow(
                """
                SELECT telegram_id, username, first_name, created_at
                FROM users
                WHERE telegram_id = $1
                """,
                user.id,
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


@dp.message(F.text == "📚 История заказов")
async def task_history(message: Message) -> None:
    if message.from_user is None or db_pool is None:
        await message.answer("Не удалось загрузить историю. Попробуйте позже.")
        return

    async with db_pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT id, description, total_cost, status, admin_reason, created_at
            FROM tasks
            WHERE creator_id = $1
            ORDER BY created_at DESC
            LIMIT 10
            """,
            message.from_user.id,
        )

    if not rows:
        await message.answer("📚 У вас пока нет заказов.", reply_markup=main_keyboard())
        return

    status_names = {
        "pending_moderation": "⏳ На модерации",
        "approved": "✅ Одобрен",
        "rejected": "❌ Отклонён",
        "revision": "📝 Требует доработки",
    }

    lines = ["📚 Последние 10 заказов:"]
    for row in rows:
        created = row["created_at"].strftime("%d.%m.%Y %H:%M")
        status = status_names.get(row["status"], row["status"])
        lines.append(
            f"\\nЗаказ #{row['id']} — {status}\\n"
            f"Дата: {created}\\n"
            f"Стоимость: {row['total_cost']:.2f} ₽\\n"
            f"Описание: {row['description'][:350]}"
        )
        if row["admin_reason"]:
            lines.append(f"Комментарий модератора: {row['admin_reason'][:500]}")

    await message.answer("\\n\\n".join(lines), reply_markup=main_keyboard())


@dp.message(F.text == "ℹ️ Помощь")
async def help_message(message: Message) -> None:
    await message.answer(
        "VIP TASK — биржа реальных услуг.\n\n"
        "Опишите заказ своими словами: категории выбирать не нужно. "
        "Можно размещать создание текстов, дизайн, монтаж, переводы, "
        "расшифровку аудио, исследования, сбор информации, тестирование "
        "сайтов и другие реальные услуги.\n\n"
        "Запрещены накрутка показателей и фиктивные отзывы. "
        "Каждый заказ проверяется администратором до публикации."
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
