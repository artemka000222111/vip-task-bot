import asyncio
import logging
import os
from decimal import Decimal, InvalidOperation

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = 5540327696

CARD_PRICE = Decimal("10.00")
REVEAL_PRICE = Decimal("50.00")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
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
POSTCARDS = {
    "heart": {"title": "❤️ Красное сердечко", "price": CARD_PRICE},
    "rose": {"title": "🌹 Роза", "price": CARD_PRICE},
    "broken": {"title": "💔 Разбитое сердце", "price": CARD_PRICE},
    "crown": {"title": "👑 Корона VIP", "price": CARD_PRICE},
    "fire": {"title": "🔥 Огонь", "price": CARD_PRICE},
    "gift": {"title": "🎁 Подарок", "price": CARD_PRICE},
    "envelope": {"title": "💌 Тайный конверт", "price": CARD_PRICE},
    "fun": {"title": "😂 Прикол", "price": CARD_PRICE},
}

class AnonymousFlow(StatesGroup):
    category = State()
    text = State()

class AdminFlow(StatesGroup):
    balance_user = State()
    balance_amount = State()
    card_upload = State()

def main_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="📩 Входящие"), KeyboardButton(text="🔗 Моя ссылка")],
        [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="🎴 Открытки"), KeyboardButton(text="ℹ️ Помощь")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="🛠 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, input_field_placeholder="Выберите действие")

def category_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=CATEGORIES["love"], callback_data="category:love"),
         InlineKeyboardButton(text=CATEGORIES["compliment"], callback_data="category:compliment")],
        [InlineKeyboardButton(text=CATEGORIES["question"], callback_data="category:question"),
         InlineKeyboardButton(text=CATEGORIES["joke"], callback_data="category:joke")],
        [InlineKeyboardButton(text=CATEGORIES["other"], callback_data="category:other")],
    ])

def postcard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❤️ Посмотреть сердечко — 10 ₽", callback_data="card:preview")],
        [InlineKeyboardButton(text="Отправить без открытки", callback_data="card:free")],
        [InlineKeyboardButton(text="Отмена", callback_data="card:cancel")],
    ])

def preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Купить за 10 ₽", callback_data="card:buy")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="card:back")],
    ])

async def init_database() -> None:
    global db_pool
    if not DATABASE_URL:
        raise RuntimeError("Не задана переменная DATABASE_URL в Railway.")
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS nobody_users (
                telegram_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT, balance NUMERIC(12,2) NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS nobody_messages (
                id BIGSERIAL PRIMARY KEY,
                recipient_id BIGINT NOT NULL REFERENCES nobody_users(telegram_id),
                sender_id BIGINT NOT NULL REFERENCES nobody_users(telegram_id),
                category TEXT NOT NULL, body TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                is_read BOOLEAN NOT NULL DEFAULT FALSE, revealed BOOLEAN NOT NULL DEFAULT FALSE,
                postcard BOOLEAN NOT NULL DEFAULT FALSE
            )
        """)
        await conn.execute("ALTER TABLE nobody_messages ADD COLUMN IF NOT EXISTS postcard BOOLEAN NOT NULL DEFAULT FALSE")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS nobody_settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS nobody_transactions (
                id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES nobody_users(telegram_id),
                amount NUMERIC(12,2) NOT NULL, kind TEXT NOT NULL, description TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), ref TEXT UNIQUE
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
            ON CONFLICT (telegram_id) DO UPDATE SET username=EXCLUDED.username,
                first_name=EXCLUDED.first_name, last_name=EXCLUDED.last_name
        """, user.id, user.username, user.first_name or "", user.last_name)

async def is_admin(user_id: int | None) -> bool:
    return user_id == ADMIN_ID

async def get_balance(user_id: int) -> Decimal:
    async with db_pool.acquire() as conn:
        value = await conn.fetchval("SELECT balance FROM nobody_users WHERE telegram_id=$1", user_id)
    return Decimal(value or 0)

async def change_balance(user_id: int, amount: Decimal, description: str, ref: str) -> bool:
    """Atomic balance change. Unique ref prevents repeating the same operation."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            inserted = await conn.fetchval("""
                INSERT INTO nobody_transactions (user_id, amount, kind, description, ref)
                VALUES ($1,$2,$3,$4,$5) ON CONFLICT (ref) DO NOTHING RETURNING id
            """, user_id, amount, "credit" if amount >= 0 else "debit", description, ref)
            if not inserted:
                return False
            if amount < 0:
                updated = await conn.fetchval("""
                    UPDATE nobody_users SET balance=balance+$2
                    WHERE telegram_id=$1 AND balance >= $3 RETURNING balance
                """, user_id, amount, -amount)
                if updated is None:
                    raise ValueError("Недостаточно средств")
            else:
                await conn.execute("UPDATE nobody_users SET balance=balance+$2 WHERE telegram_id=$1", user_id, amount)
    return True

async def send_message_to_recipient(bot: Bot, recipient_id: int, category: str, body: str, message_id: int, postcard: bool):
    text = f"💌 Новая анонимка!\n\n{CATEGORIES.get(category, CATEGORIES['other'])}\n\n{body}\n\nСообщение #{message_id}"
    try:
        if postcard:
            async with db_pool.acquire() as conn:
                file_id = await conn.fetchval("SELECT value FROM nobody_settings WHERE key='heart_file_id'")
            if file_id:
                await bot.send_photo(recipient_id, file_id, caption=text)
            else:
                await bot.send_message(recipient_id, "❤️ Открытка-сердечко\n\n" + text)
        else:
            await bot.send_message(recipient_id, text)
    except Exception:
        log.exception("Could not notify recipient %s", recipient_id)

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    if not message.from_user:
        return
    await save_user(message.from_user)
    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1].strip() if len(parts) == 2 else ""
    if payload.startswith("to_"):
        try:
            recipient_id = int(payload[3:])
        except ValueError:
            recipient_id = 0
        if recipient_id == message.from_user.id:
            await message.answer("Нельзя отправить сообщение самому себе.")
            return
        async with db_pool.acquire() as conn:
            exists = await conn.fetchval("SELECT 1 FROM nobody_users WHERE telegram_id=$1", recipient_id)
        if not exists:
            await message.answer("Получатель ещё не запускал бота. Попросите его открыть VIP NOBODY.")
            return
        await state.update_data(recipient_id=recipient_id)
        await state.set_state(AnonymousFlow.category)
        await message.answer(
            "Выберите категорию анонимного сообщения.\n\n"
            "Важно: сервис сохраняет технические данные отправителя. Получатель сможет раскрыть доступные данные профиля за 50 ₽.",
            reply_markup=category_keyboard())
        return
    await message.answer(
        "👋 Добро пожаловать в VIP NOBODY!\n\nПоделись своей ссылкой, чтобы получить анонимное сообщение.",
        reply_markup=main_keyboard(await is_admin(message.from_user.id)))

@dp.callback_query(F.data.startswith("category:"))
async def choose_category(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    if key not in CATEGORIES:
        await callback.answer("Неизвестная категория", show_alert=True); return
    await state.update_data(category=key)
    await state.set_state(AnonymousFlow.text)
    await callback.answer()
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"{CATEGORIES[key]}\n\nТеперь напишите текст сообщения.")

@dp.message(AnonymousFlow.text)
async def receive_text(message: Message, state: FSMContext):
    body = (message.text or "").strip()
    if not body or len(body) > 2000:
        await message.answer("Отправьте текст до 2000 символов."); return
    data = await state.get_data()
    if not message.from_user or not data.get("recipient_id") or data.get("category") not in CATEGORIES:
        await state.clear(); await message.answer("Начните заново по персональной ссылке."); return
    await state.update_data(body=body)
    await message.answer("Текст готов! Можно добавить фирменную открытку:", reply_markup=postcard_keyboard())

@dp.callback_query(F.data == "card:preview")
async def card_preview(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if not data.get("body"):
        await callback.answer("Начните отправку заново по ссылке.", show_alert=True); return
    async with db_pool.acquire() as conn:
        file_id = await conn.fetchval("SELECT value FROM nobody_settings WHERE key='heart_file_id'")
    await callback.answer()
    if file_id and callback.message:
        await bot.send_photo(callback.from_user.id, file_id,
            caption="❤️ Красное сердечко — 10 ₽\n\nТак будет выглядеть открытка у получателя.",
            reply_markup=preview_keyboard())
    elif callback.message:
        await callback.message.answer(
            "❤️ ПРЕДПРОСМОТР ОТКРЫТКИ\n\n"
            "Красное сердечко на тёмном фоне.\n"
            "Логотип VIP NOBODY будет маленьким и аккуратным в углу.\n\n"
            "Стоимость: 10 ₽",
            reply_markup=preview_keyboard())

@dp.callback_query(F.data == "card:back")
async def card_back(callback: CallbackQuery):
    await callback.answer()
    if callback.message:
        await callback.message.answer("Выберите действие:", reply_markup=postcard_keyboard())

async def finish_anonymous(callback: CallbackQuery, state: FSMContext, bot: Bot, paid_card: bool):
    data = await state.get_data()
    sender_id = callback.from_user.id
    recipient_id = data.get("recipient_id")
    category = data.get("category")
    body = data.get("body")
    if not recipient_id or category not in CATEGORIES or not body:
        await callback.answer("Данные сообщения потерялись. Начните заново.", show_alert=True); return
    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                if paid_card:
                    user = await conn.fetchrow("""
                        UPDATE nobody_users SET balance=balance-$2
                        WHERE telegram_id=$1 AND balance >= $2 RETURNING balance
                    """, sender_id, CARD_PRICE)
                    if user is None:
                        raise ValueError("Недостаточно средств. Пополните баланс.")
                    await conn.execute("""
                        INSERT INTO nobody_transactions (user_id,amount,kind,description,ref)
                        VALUES ($1,$2,'debit','Покупка открытки-сердечка',$3)
                    """, sender_id, -CARD_PRICE, f"card:{sender_id}:{callback.id}")
                row = await conn.fetchrow("""
                    INSERT INTO nobody_messages (recipient_id,sender_id,category,body,postcard)
                    VALUES ($1,$2,$3,$4,$5) RETURNING id
                """, recipient_id, sender_id, category, body, paid_card)
        await state.clear()
        await callback.answer("Сообщение отправлено!")
        if callback.message:
            await callback.message.answer(
                "✅ Анонимное сообщение отправлено!" + (" Открытка куплена за 10 ₽." if paid_card else ""),
                reply_markup=main_keyboard(await is_admin(sender_id)))
        await send_message_to_recipient(bot, recipient_id, category, body, row["id"], paid_card)
    except ValueError as e:
        await callback.answer(str(e), show_alert=True)
    except Exception:
        log.exception("Failed to send anonymous message")
        await callback.answer("Не удалось отправить сообщение. Попробуйте ещё раз.", show_alert=True)

@dp.callback_query(F.data == "card:buy")
async def buy_card(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await finish_anonymous(callback, state, bot, True)

@dp.callback_query(F.data == "card:free")
async def send_without_card(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await finish_anonymous(callback, state, bot, False)

@dp.callback_query(F.data == "card:cancel")
async def cancel_card(callback: CallbackQuery, state: FSMContext):
    await state.clear(); await callback.answer("Отменено.")
    if callback.message:
        await callback.message.answer("Отправка отменена.")

@dp.message(F.text == "🔗 Моя ссылка")
async def my_link(message: Message, bot: Bot):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=to_{message.from_user.id}"
    await message.answer("💜 Есть кое-что, что давно хотел сказать?\nПризнайся, задай вопрос или отправь комплимент — можешь остаться загадкой 👀\n\n👇 Напиши мне анонимно:\n" + link + "\n\nЖду твоё сообщение! 💌")

@dp.message(F.text == "📩 Входящие")
async def inbox(message: Message):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id,category,body,created_at,postcard FROM nobody_messages WHERE recipient_id=$1 ORDER BY created_at DESC LIMIT 10", message.from_user.id)
        await conn.execute("UPDATE nobody_messages SET is_read=TRUE WHERE recipient_id=$1", message.from_user.id)
    if not rows:
        await message.answer("📭 Пока сообщений нет. Поделись своей ссылкой!"); return
    for row in rows:
        await message.answer(f"💌 Анонимное сообщение #{row['id']}\n{CATEGORIES.get(row['category'], CATEGORIES['other'])}\n\n{row['body']}" + ("\n❤️ С открыткой" if row["postcard"] else "") + "\n\nРаскрытие личности — 50 ₽ (следующий этап).")

@dp.message(F.text == "💰 Баланс")
async def balance(message: Message):
    amount = await get_balance(message.from_user.id)
    await message.answer(f"💰 Твой баланс: {amount:.2f} ₽\n\nПополнение будет подтверждаться администратором вручную.")

@dp.message(F.text == "👤 Профиль")
async def profile(message: Message):
    user = message.from_user
    await message.answer(f"👤 Профиль VIP NOBODY\n\nИмя: {user.first_name or 'не указано'}\nUsername: @{user.username if user.username else 'не указан'}\nTelegram ID: {user.id}")

@dp.message(F.text == "🎴 Открытки")
async def postcards(message: Message):
    await message.answer("❤️ Красное сердечко — 10 ₽\n🌹 Роза — в разработке\n💔 Разбитое сердце — в разработке\n👑 Корона VIP — в разработке\n🔥 Огонь — в разработке\n🎁 Подарок — в разработке\n💌 Тайный конверт — в разработке\n😂 Прикол — в разработке\n\nОткрытку можно выбрать при отправке анонимного сообщения.")

@dp.message(F.text == "ℹ️ Помощь")
async def help_message(message: Message):
    await message.answer("ℹ️ VIP NOBODY\n\n1. Поделись ссылкой.\n2. Получай анонимные сообщения.\n3. Отправитель может добавить открытку за 10 ₽.\n\nТехнические данные отправителя сохраняются; абсолютная анонимность не гарантируется.")

@dp.message(Command("admin"))
@dp.message(F.text == "🛠 Админ-панель")
async def admin_panel(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await message.answer("Нет доступа."); return
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Начислить баланс", callback_data="admin:credit"),
         InlineKeyboardButton(text="➖ Списать баланс", callback_data="admin:debit")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="❤️ Загрузить сердечко", callback_data="admin:upload_card")],
    ])
    await message.answer("🛠 АДМИН-ПАНЕЛЬ VIP NOBODY", reply_markup=kb)

@dp.callback_query(F.data.startswith("admin:"))
async def admin_actions(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True); return
    action = callback.data.split(":")[1]
    await callback.answer()
    if action in ("credit", "debit"):
        await state.update_data(balance_action=action)
        await state.set_state(AdminFlow.balance_user)
        if callback.message: await callback.message.answer("Отправь Telegram ID пользователя.")
    elif action == "stats":
        async with db_pool.acquire() as conn:
            users = await conn.fetchval("SELECT COUNT(*) FROM nobody_users")
            msgs = await conn.fetchval("SELECT COUNT(*) FROM nobody_messages")
            total = await conn.fetchval("SELECT COALESCE(SUM(balance),0) FROM nobody_users")
        if callback.message: await callback.message.answer(f"📊 Статистика\nПользователей: {users}\nСообщений: {msgs}\nБалансов пользователей: {total:.2f} ₽")
    elif action == "upload_card":
        await state.set_state(AdminFlow.card_upload)
        if callback.message: await callback.message.answer("Отправь сюда фотографию красного сердечка как обычное фото. Я сохраню её для предпросмотра и отправки.")

@dp.message(AdminFlow.balance_user)
async def admin_balance_user(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    try: target = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужен числовой Telegram ID."); return
    async with db_pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM nobody_users WHERE telegram_id=$1", target)
    if not exists:
        await message.answer("Пользователь ещё не запускал бота. Сначала он должен нажать /start."); return
    await state.update_data(target_id=target)
    await state.set_state(AdminFlow.balance_amount)
    await message.answer("Введи сумму в рублях. Для списания напиши положительное число — действие будет выбрано по предыдущему шагу.")

@dp.message(AdminFlow.balance_amount)
async def admin_balance_amount(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    try:
        amount = Decimal((message.text or "").replace(",", ".").strip()).quantize(Decimal("0.01"))
        if amount <= 0 or amount > Decimal("1000000"): raise InvalidOperation
    except (InvalidOperation, ValueError):
        await message.answer("Введи корректную сумму больше 0."); return
    data = await state.get_data()
    target = data["target_id"]
    action = data["balance_action"]
    delta = amount if action == "credit" else -amount
    ref = f"admin:{message.from_user.id}:{message.message_id}"
    try:
        await change_balance(target, delta, "Ручное начисление администратором" if delta > 0 else "Ручное списание администратором", ref)
    except ValueError:
        await message.answer("У пользователя недостаточно средств."); return
    await state.clear()
    await message.answer(f"✅ Операция выполнена. Новый баланс: {await get_balance(target):.2f} ₽")

@dp.message(AdminFlow.card_upload, F.photo)
async def admin_card_upload(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    file_id = message.photo[-1].file_id
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO nobody_settings(key,value) VALUES('heart_file_id',$1)
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value
        """, file_id)
    await state.clear()
    await message.answer("✅ Сердечко сохранено! Теперь пользователи смогут увидеть фото в предпросмотре.")

@dp.message(AdminFlow.card_upload)
async def admin_card_upload_wrong(message: Message):
    await message.answer("Нужно отправить именно фотографию, не файл и не текст.")

async def main():
    if not BOT_TOKEN: raise RuntimeError("Не задан BOT_TOKEN в переменных Railway.")
    await init_database()
    bot = Bot(token=BOT_TOKEN)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        if db_pool is not None: await db_pool.close()

if __name__ == "__main__":
    asyncio.run(main())
