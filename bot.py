import asyncio
import logging
import os
from decimal import Decimal, InvalidOperation

import asyncpg
from aiogram import BaseMiddleware, Bot, Dispatcher, F
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
    "gift": "🎁 Подарок",
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
    price_value = State()
    message_view_user = State()

class TopUpFlow(StatesGroup):
    amount = State()
    sender = State()
    confirm = State()

class GiftFlow(StatesGroup):
    amount = State()
    text = State()
    visibility = State()

class WithdrawFlow(StatesGroup):
    amount = State()
    phone = State()
    bank = State()

class ReplyFlow(StatesGroup):
    text = State()

class SupportFlow(StatesGroup):
    new_text = State()
    user_reply = State()
    admin_reply = State()
    direct_target = State()
    direct_text = State()
    block_target = State()

def main_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="🔗 Моя ссылка")],
        [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="ℹ️ Помощь")],
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
        [InlineKeyboardButton(text=CATEGORIES["gift"], callback_data="category:gift"),
         InlineKeyboardButton(text=CATEGORIES["other"], callback_data="category:other")],
    ])

def postcard_keyboard(price: Decimal) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❤️ Красное сердечко", callback_data="card:select:heart")],
        [InlineKeyboardButton(text="🌹 Роза", callback_data="card:select:rose")],
        [InlineKeyboardButton(text="💔 Разбитое сердце", callback_data="card:select:broken")],
        [InlineKeyboardButton(text="💌 Тайный конверт", callback_data="card:select:envelope")],
        [InlineKeyboardButton(text="🎁 Подарить деньги", callback_data="card:gift")],
        [InlineKeyboardButton(text="Отправить без открытки", callback_data="card:free")],
    ])

def preview_keyboard(price: Decimal) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Купить за {price:.2f} ₽", callback_data="card:buy")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="card:back")],
    ])

def postcard_setting_key(key: str) -> str:
    return "heart_file_id" if key == "heart" else f"postcard_file_id_{key}"


def insufficient_balance_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Пополнить баланс", callback_data="topup:start")]
    ])

async def get_price(key: str) -> Decimal:
    defaults = {"card_price": CARD_PRICE, "reveal_price": REVEAL_PRICE, "withdraw_commission": Decimal("10.00")}
    async with db_pool.acquire() as conn:
        value = await conn.fetchval("SELECT value FROM nobody_settings WHERE key=$1", key)
    return Decimal(value) if value is not None else defaults[key]

async def clear_flow_message(bot: Bot, chat_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    message_id = data.get("flow_bot_message_id")
    if message_id:
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            log.debug("Could not delete previous flow message %s", message_id)
        await state.update_data(flow_bot_message_id=None)

async def send_flow_message(bot: Bot, chat_id: int, state: FSMContext, text: str,
                            reply_markup=None) -> Message:
    await clear_flow_message(bot, chat_id, state)
    sent = await bot.send_message(chat_id, text, reply_markup=reply_markup)
    await state.update_data(flow_bot_message_id=sent.message_id)
    return sent

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
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_active_at TIMESTAMPTZ,
                activity_count BIGINT NOT NULL DEFAULT 0
            )
        """)
        await conn.execute("ALTER TABLE nobody_users ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMPTZ")
        await conn.execute("ALTER TABLE nobody_users ADD COLUMN IF NOT EXISTS activity_count BIGINT NOT NULL DEFAULT 0")
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
            INSERT INTO nobody_settings (key,value) VALUES
                ('card_price','10.00'), ('reveal_price','50.00')
            ON CONFLICT (key) DO NOTHING
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS nobody_transactions (
                id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES nobody_users(telegram_id),
                amount NUMERIC(12,2) NOT NULL, kind TEXT NOT NULL, description TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), ref TEXT UNIQUE
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS nobody_support_tickets (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES nobody_users(telegram_id),
                status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                user_can_close BOOLEAN NOT NULL DEFAULT TRUE
            )
        """)
        await conn.execute("ALTER TABLE nobody_support_tickets ADD COLUMN IF NOT EXISTS user_can_close BOOLEAN NOT NULL DEFAULT TRUE")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS nobody_support_messages (
                id BIGSERIAL PRIMARY KEY,
                ticket_id BIGINT NOT NULL REFERENCES nobody_support_tickets(id) ON DELETE CASCADE,
                sender_id BIGINT NOT NULL,
                sender_role TEXT NOT NULL CHECK (sender_role IN ('user','admin')),
                body TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS nobody_support_blocks (
                user_id BIGINT PRIMARY KEY REFERENCES nobody_users(telegram_id),
                blocked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS nobody_gifts (
                id BIGSERIAL PRIMARY KEY,
                sender_id BIGINT NOT NULL REFERENCES nobody_users(telegram_id),
                recipient_id BIGINT NOT NULL REFERENCES nobody_users(telegram_id),
                amount NUMERIC(12,2) NOT NULL CHECK (amount >= 10),
                body TEXT NOT NULL,
                anonymous BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                ref TEXT UNIQUE NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS nobody_pending_gifts (
                sender_id BIGINT PRIMARY KEY REFERENCES nobody_users(telegram_id) ON DELETE CASCADE,
                recipient_id BIGINT NOT NULL REFERENCES nobody_users(telegram_id),
                body TEXT NOT NULL,
                anonymous BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("ALTER TABLE nobody_pending_gifts ADD COLUMN IF NOT EXISTS anonymous BOOLEAN NOT NULL DEFAULT TRUE")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS nobody_withdrawals (
                id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES nobody_users(telegram_id),
                amount NUMERIC(12,2) NOT NULL CHECK (amount > 0), phone TEXT NOT NULL, bank TEXT NOT NULL,
                commission_percent NUMERIC(5,2) NOT NULL DEFAULT 10.00,
                payout_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), reviewed_at TIMESTAMPTZ
            )
        """)
        await conn.execute("ALTER TABLE nobody_withdrawals ADD COLUMN IF NOT EXISTS commission_percent NUMERIC(5,2) NOT NULL DEFAULT 10.00")
        await conn.execute("ALTER TABLE nobody_withdrawals ADD COLUMN IF NOT EXISTS payout_amount NUMERIC(12,2) NOT NULL DEFAULT 0")
        await conn.execute("UPDATE nobody_withdrawals SET payout_amount=amount WHERE payout_amount=0")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS nobody_topups (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES nobody_users(telegram_id),
                amount NUMERIC(12,2) NOT NULL CHECK (amount > 0),
                sender_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                reviewed_at TIMESTAMPTZ
            )
        """)
    log.info("VIP NOBODY database is ready")

async def save_user(user) -> None:
    if db_pool is None or user is None:
        return
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO nobody_users (telegram_id, username, first_name, last_name, last_active_at)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (telegram_id) DO UPDATE SET username=EXCLUDED.username,
                first_name=EXCLUDED.first_name, last_name=EXCLUDED.last_name,
                last_active_at=NOW(), activity_count=nobody_users.activity_count+1
        """, user.id, user.username, user.first_name or "", user.last_name)

class ActivityMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None:
            message = getattr(event, "message", None)
            callback = getattr(event, "callback_query", None)
            user = (message.from_user if message else None) or (callback.from_user if callback else None)
        if user is not None:
            await save_user(user)
        return await handler(event, data)

dp.update.outer_middleware(ActivityMiddleware())

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

async def message_actions_keyboard(message_id: int) -> InlineKeyboardMarkup:
    reveal_price = await get_price("reveal_price")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Ответить", callback_data=f"msg:reply:{message_id}")],
        [InlineKeyboardButton(text=f"🔎 Раскрыть личность — {reveal_price:.2f} ₽", callback_data=f"msg:reveal:{message_id}")],
    ])

async def send_message_to_recipient(bot: Bot, recipient_id: int, category: str, body: str, message_id: int, postcard: bool, postcard_key: str = "heart"):
    text = f"💌 Новая анонимка!\n\n{CATEGORIES.get(category, CATEGORIES['other'])}\n\n{body}"
    markup = await message_actions_keyboard(message_id)
    try:
        if postcard:
            async with db_pool.acquire() as conn:
                file_id = await conn.fetchval("SELECT value FROM nobody_settings WHERE key=$1", postcard_setting_key(postcard_key))
            if file_id:
                await bot.send_photo(recipient_id, file_id, caption=text, reply_markup=markup)
            else:
                await bot.send_message(recipient_id, text, reply_markup=markup)
        else:
            await bot.send_message(recipient_id, text, reply_markup=markup)
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
        sent = await message.answer(
            "Выберите категорию анонимного сообщения.\n\n"
            "Учти, твою личность могут раскрыть.",
            reply_markup=category_keyboard())
        await state.update_data(flow_bot_message_id=sent.message_id)
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
    await callback.answer()
    if key == "gift":
        await state.set_state(GiftFlow.visibility)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎭 Анонимно", callback_data="gift:visibility:anonymous")],
            [InlineKeyboardButton(text="👤 Показать, кто отправил", callback_data="gift:visibility:visible")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="gift:cancel")]
        ])
        async with db_pool.acquire() as conn:
            gift_photo = await conn.fetchval("SELECT value FROM nobody_settings WHERE key='postcard_file_id_gift'")
        caption = "🎁 Денежный подарок\n\nТак будет выглядеть подарок у получателя.\n\nКак отправить подарок?"
        if callback.message:
            try:
                await callback.message.delete()
            except Exception:
                pass
            if gift_photo:
                sent = await callback.message.answer_photo(gift_photo, caption=caption, reply_markup=kb)
            else:
                sent = await callback.message.answer(caption, reply_markup=kb)
            await state.update_data(flow_bot_message_id=sent.message_id)
        return
    await state.set_state(AnonymousFlow.text)
    if callback.message:
        await callback.message.edit_text(f"{CATEGORIES[key]}\n\nТеперь напишите текст сообщения.")
        await state.update_data(flow_bot_message_id=callback.message.message_id)

@dp.message(AnonymousFlow.text)
async def receive_text(message: Message, state: FSMContext):
    body = (message.text or "").strip()
    if not body or len(body) > 2000:
        await message.answer("Отправьте текст до 2000 символов."); return
    data = await state.get_data()
    if not message.from_user or not data.get("recipient_id") or data.get("category") not in CATEGORIES:
        await state.clear(); await message.answer("Начните заново по персональной ссылке."); return
    await state.update_data(body=body)
    card_price = await get_price("card_price")
    await send_flow_message(message.bot, message.chat.id, state,
                                "💌 Сообщение готово!\n\nУкрась его открыткой — так послание станет ещё выразительнее!\n\n❤️ Сердечко — тёплые чувства\n🌹 Роза — знак симпатии\n💔 Разбитое сердце — немного грусти\n💌 Тайный конверт — для важного сообщения\n🎁 Подарить деньги\n\nВыбирай открытку и отправляй красиво!",
                                reply_markup=postcard_keyboard(card_price))

@dp.callback_query(F.data == "gift:resume")
async def gift_resume(callback: CallbackQuery, state: FSMContext):
    async with db_pool.acquire() as conn:
        pending = await conn.fetchrow(
            "SELECT recipient_id,body,anonymous FROM nobody_pending_gifts WHERE sender_id=$1",
            callback.from_user.id
        )
    if not pending:
        await callback.answer("Сохранённый подарок не найден. Начни отправку заново.", show_alert=True)
        return
    await state.clear()
    await state.update_data(recipient_id=pending["recipient_id"], gift_body=pending["body"], anonymous=pending["anonymous"], category="gift")
    await state.set_state(GiftFlow.amount)
    await callback.answer()
    if callback.message:
        await callback.message.answer("🎁 Возвращаемся к подарку!\nУкажи сумму от 10 до 5 000 ₽:")

@dp.message(GiftFlow.amount)
async def gift_amount(message: Message, state: FSMContext):
    try:
        amount = Decimal((message.text or "").replace(",", ".").strip()).quantize(Decimal("0.01"))
        if amount < Decimal("10") or amount > Decimal("5000"): raise InvalidOperation
    except (InvalidOperation, ValueError):
        await message.answer("Сумма подарка должна быть от 10 до 5 000 ₽. Введи корректную сумму."); return
    if await get_balance(message.from_user.id) < amount:
        data = await state.get_data()
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO nobody_pending_gifts (sender_id,recipient_id,body,anonymous) VALUES ($1,$2,$3,$4) "
                "ON CONFLICT (sender_id) DO UPDATE SET recipient_id=EXCLUDED.recipient_id, body=EXCLUDED.body, anonymous=EXCLUDED.anonymous, created_at=NOW()",
                message.from_user.id, data["recipient_id"], data["gift_body"], data.get("anonymous", True)
            )
        await state.update_data(resume_gift=True)
        await message.answer(
            "Недостаточно средств для такого подарка. Пополни баланс — после подтверждения оплаты вернём тебя к подарку, и ты сможешь снова указать сумму.",
            reply_markup=insufficient_balance_keyboard()
        )
        return
    await state.update_data(gift_amount=str(amount))
    data = await state.get_data()
    sender_id = message.from_user.id
    recipient_id = int(data["recipient_id"])
    anonymous = bool(data.get("anonymous", True))
    if sender_id == recipient_id:
        await message.answer("Нельзя отправить подарок самому себе.")
        await state.clear()
        return
    gift_ref = f"gift:{sender_id}:{message.message_id}"
    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                sender_balance = await conn.fetchval("UPDATE nobody_users SET balance=balance-$2 WHERE telegram_id=$1 AND balance >= $2 RETURNING balance", sender_id, amount)
                if sender_balance is None:
                    raise ValueError("Недостаточно средств")
                await conn.execute("UPDATE nobody_users SET balance=balance+$2 WHERE telegram_id=$1", recipient_id, amount)
                gift_id = await conn.fetchval("INSERT INTO nobody_gifts (sender_id,recipient_id,amount,body,anonymous,ref) VALUES ($1,$2,$3,$4,$5,$6) RETURNING id", sender_id, recipient_id, amount, data["gift_body"], anonymous, gift_ref)
                await conn.execute("INSERT INTO nobody_transactions (user_id,amount,kind,description,ref) VALUES ($1,$2,'debit','Денежный подарок',$3)", sender_id, -amount, f"gift-send:{gift_ref}")
                await conn.execute("INSERT INTO nobody_transactions (user_id,amount,kind,description,ref) VALUES ($1,$2,'credit','Получен денежный подарок',$3)", recipient_id, amount, f"gift-receive:{gift_ref}")
    except ValueError:
        await message.answer("Недостаточно средств для подарка.", reply_markup=insufficient_balance_keyboard())
        return
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM nobody_pending_gifts WHERE sender_id=$1", sender_id)
        photo_id = await conn.fetchval("SELECT value FROM nobody_settings WHERE key='postcard_file_id_gift'")
    await state.clear()
    sender = message.from_user
    sender_label = "Отправитель пожелал остаться анонимным." if anonymous else f"Отправитель: {sender.full_name}" + (f" (@{sender.username})" if sender.username else "")
    caption = f"🎁 Вам отправили подарок на {amount:.2f} ₽!\n\n{data['gift_body']}\n\n{sender_label}"
    try:
        if photo_id:
            await message.bot.send_photo(recipient_id, photo_id, caption=caption)
        else:
            await message.bot.send_message(recipient_id, caption)
    except Exception:
        log.exception("Could not deliver gift %s to recipient %s", gift_id, recipient_id)
    await message.answer(f"✅ Денежный подарок на {amount:.2f} ₽ отправлен!\nТекущий баланс: {await get_balance(sender_id):.2f} ₽")

@dp.message(GiftFlow.text)
async def gift_text(message: Message, state: FSMContext):
    body = (message.text or "").strip()
    if not body or len(body) > 1000:
        await message.answer("Напиши текст от 1 до 1000 символов."); return
    await state.update_data(gift_body=body)
    await state.set_state(GiftFlow.amount)
    await message.answer("Укажи сумму подарка от 10 до 5 000 ₽:")

@dp.callback_query(F.data == "gift:cancel")
async def gift_cancel(callback: CallbackQuery, state: FSMContext):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM nobody_pending_gifts WHERE sender_id=$1", callback.from_user.id)
    await state.clear()
    await callback.answer("Подарок отменён.")
    if callback.message: await callback.message.answer("Подарок отменён. Можешь отправить обычное сообщение.")

@dp.callback_query(F.data.startswith("gift:visibility:"))
async def gift_visibility(callback: CallbackQuery, state: FSMContext):
    if not callback.data.endswith((":anonymous", ":visible")):
        await callback.answer("Неизвестный вариант.", show_alert=True)
        return
    await state.update_data(anonymous=callback.data.endswith(":anonymous"))
    await state.set_state(GiftFlow.text)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Напиши текст, который будет приложен к денежному подарку:")


@dp.callback_query(F.data.startswith("card:select:"))
async def select_postcard(callback: CallbackQuery, state: FSMContext, bot: Bot):
    key = callback.data.rsplit(":", 1)[1]
    if key not in ("heart", "rose", "broken", "envelope"):
        await callback.answer("Неизвестная открытка.", show_alert=True); return
    async with db_pool.acquire() as conn:
        file_id = await conn.fetchval("SELECT value FROM nobody_settings WHERE key=$1", postcard_setting_key(key))
    if not file_id:
        await callback.answer("Эта открытка пока не добавлена. Загляни позже!", show_alert=True); return
    await state.update_data(selected_postcard=key)
    await card_preview(callback, state, bot)

@dp.callback_query(F.data == "card:preview")
async def card_preview(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if not data.get("body"):
        await callback.answer("Начните отправку заново по ссылке.", show_alert=True); return
    postcard_key = data.get("selected_postcard", "heart")
    async with db_pool.acquire() as conn:
        file_id = await conn.fetchval("SELECT value FROM nobody_settings WHERE key=$1", postcard_setting_key(postcard_key))
    card_price = await get_price("card_price")
    await callback.answer()
    if callback.message:
        await clear_flow_message(bot, callback.from_user.id, state)
    if file_id and callback.message:
        sent = await bot.send_photo(callback.from_user.id, file_id,
            caption=f"{data['body']}\n\nВот так будет выглядеть анонимное сообщение",
            reply_markup=preview_keyboard(card_price))
        await state.update_data(flow_bot_message_id=sent.message_id)
    elif callback.message:
        await send_flow_message(bot, callback.from_user.id, state,
            f"{data['body']}\n\nВот так будет выглядеть анонимное сообщение",
            reply_markup=preview_keyboard(card_price))

@dp.callback_query(F.data == "card:back")
async def card_back(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    if callback.message:
        card_price = await get_price("card_price")
        await send_flow_message(bot, callback.from_user.id, state,
                                "💌 Укрась своё сообщение открыткой!\n\n❤️ Сердечко — тёплые чувства\n🌹 Роза — знак симпатии\n💔 Разбитое сердце — немного грусти\n💌 Тайный конверт — для важного сообщения\n🎁 Подарить деньги\n\nВыбирай настроение послания:", reply_markup=postcard_keyboard(card_price))

async def finish_anonymous(callback: CallbackQuery, state: FSMContext, bot: Bot, paid_card: bool):
    data = await state.get_data()
    sender_id = callback.from_user.id
    await clear_flow_message(bot, sender_id, state)
    recipient_id = data.get("recipient_id")
    category = data.get("category")
    body = data.get("body")
    if not recipient_id or category not in CATEGORIES or not body:
        await callback.answer("Данные сообщения потерялись. Начните заново.", show_alert=True); return
    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                if paid_card:
                    card_price = await get_price("card_price")
                    user = await conn.fetchrow("""
                        UPDATE nobody_users SET balance=balance-$2
                        WHERE telegram_id=$1 AND balance >= $2 RETURNING balance
                    """, sender_id, card_price)
                    if user is None:
                        raise ValueError("Недостаточно средств. Пополните баланс.")
                    await conn.execute("""
                        INSERT INTO nobody_transactions (user_id,amount,kind,description,ref)
                        VALUES ($1,$2,'debit','Покупка открытки-сердечка',$3)
                    """, sender_id, -card_price, f"card:{sender_id}:{callback.id}")
                row = await conn.fetchrow("""
                    INSERT INTO nobody_messages (recipient_id,sender_id,category,body,postcard)
                    VALUES ($1,$2,$3,$4,$5) RETURNING id
                """, recipient_id, sender_id, category, body, paid_card)
        await state.clear()
        await callback.answer("Сообщение отправлено!")
        if callback.message:
            await callback.message.answer(
                "✅ Анонимное сообщение отправлено!" + (f" Открытка куплена за {card_price:.2f} ₽." if paid_card else ""),
                reply_markup=main_keyboard(await is_admin(sender_id)))
        await send_message_to_recipient(bot, recipient_id, category, body, row["id"], paid_card, data.get("selected_postcard", "heart"))
    except ValueError as e:
        if "Недостаточно средств" in str(e):
            await state.clear()
            await callback.answer()
            if callback.message:
                await callback.message.answer(
                    "Недостаточно средств. Пополни баланс, чтобы купить открытку.",
                    reply_markup=insufficient_balance_keyboard()
                )
        else:
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

@dp.message(F.text == "🔗 Моя ссылка")
async def my_link(message: Message, bot: Bot):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=to_{message.from_user.id}"
    await message.answer("💜 Есть кое-что, что давно хотел сказать?\nПризнайся, задай вопрос или отправь комплимент — можешь остаться загадкой 👀\n\n👇 Напиши мне анонимно:\n" + link + "\n\nЖду твоё сообщение! 💌")

@dp.callback_query(F.data.startswith("msg:reply:"))
async def begin_message_reply(callback: CallbackQuery, state: FSMContext):
    message_id = int(callback.data.rsplit(":", 1)[1])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT recipient_id FROM nobody_messages WHERE id=$1", message_id
        )
    if not row or row["recipient_id"] != callback.from_user.id:
        await callback.answer("Это сообщение недоступно.", show_alert=True)
        return
    await state.clear()
    await state.update_data(reply_message_id=message_id)
    await state.set_state(ReplyFlow.text)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Напиши ответ. Он будет отправлен автору анонимно:")

@dp.message(ReplyFlow.text)
async def send_anonymous_reply(message: Message, state: FSMContext, bot: Bot):
    body = (message.text or "").strip()
    if not body or len(body) > 2000:
        await message.answer("Напиши текст ответа до 2000 символов.")
        return
    data = await state.get_data()
    message_id = data.get("reply_message_id")
    async with db_pool.acquire() as conn:
        original = await conn.fetchrow(
            "SELECT sender_id, recipient_id FROM nobody_messages WHERE id=$1", message_id
        )
    if not original or original["recipient_id"] != message.from_user.id:
        await state.clear()
        await message.answer("Не удалось найти исходное сообщение. Попробуй ещё раз.")
        return
    try:
        await bot.send_message(
            original["sender_id"],
            f"↩️ Тебе ответили на анонимное сообщение №{message_id}:\n\n{body}\n\n"
            "Ответ отправлен анонимно через VIP NOBODY."
        )
    except Exception:
        log.exception("Could not deliver anonymous reply to sender of message %s", message_id)
        await message.answer("Не удалось доставить ответ. Возможно, автор ещё не открыл бота или заблокировал его.")
        await state.clear()
        return
    await state.clear()
    await message.answer("✅ Ответ отправлен автору анонимно.", reply_markup=main_keyboard(await is_admin(message.from_user.id)))

@dp.callback_query(F.data.startswith("msg:reveal:"))
async def reveal_message_author(callback: CallbackQuery, bot: Bot):
    if not callback.from_user:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return
    message_id = int(callback.data.rsplit(":", 1)[1])
    user_id = callback.from_user.id
    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM nobody_messages WHERE id=$1 AND recipient_id=$2 FOR UPDATE",
                    message_id, user_id
                )
                if not row:
                    await callback.answer("Сообщение не найдено.", show_alert=True)
                    return
                author = await conn.fetchrow(
                    "SELECT telegram_id, username, first_name, last_name FROM nobody_users WHERE telegram_id=$1",
                    row["sender_id"]
                )
                if row["revealed"]:
                    already_revealed = True
                else:
                    reveal_price = await get_price("reveal_price")
                    updated = await conn.fetchrow(
                        "UPDATE nobody_users SET balance=balance-$2 WHERE telegram_id=$1 AND balance >= $2 RETURNING balance",
                        user_id, reveal_price
                    )
                    if updated is None:
                        raise ValueError("Недостаточно средств. Пополни баланс и попробуй снова.")
                    await conn.execute(
                        """INSERT INTO nobody_transactions (user_id, amount, kind, description, ref)
                           VALUES ($1,$2,'debit',$3,$4)""",
                        user_id, -reveal_price, f"Раскрытие автора анонимного сообщения №{message_id}",
                        f"reveal:{message_id}:{user_id}"
                    )
                    await conn.execute(
                        "UPDATE nobody_messages SET revealed=TRUE WHERE id=$1 AND recipient_id=$2",
                        message_id, user_id
                    )
                    already_revealed = False
        await callback.answer("Личность раскрыта!" if not already_revealed else "Уже раскрыто.")
        username = f"@{author['username']}" if author and author["username"] else "не указан"
        full_name = " ".join(part for part in [author["first_name"], author["last_name"]] if part) if author else "неизвестно"
        result_text = (
            f"🔎 Автор сообщения №{message_id}\n\n"
            f"Имя: {full_name}\nUsername: {username}\nTelegram ID: {author['telegram_id'] if author else row['sender_id']}"
        )
        if callback.message:
            await callback.message.answer(result_text)
    except ValueError as exc:
        if "Недостаточно средств" in str(exc):
            await callback.answer()
            if callback.message:
                await callback.message.answer(
                    "Недостаточно средств. Пополни баланс, чтобы раскрыть личность.",
                    reply_markup=insufficient_balance_keyboard()
                )
        else:
            await callback.answer(str(exc), show_alert=True)
    except Exception:
        log.exception("Failed to reveal author for message %s", message_id)
        await callback.answer("Не удалось выполнить операцию. Попробуй ещё раз.", show_alert=True)

@dp.message(F.text == "💰 Баланс")
async def balance(message: Message):
    amount = await get_balance(message.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Пополнить баланс", callback_data="topup:start")],
        [InlineKeyboardButton(text="💸 Вывести средства", callback_data="withdraw:start")]
    ])
    await message.answer(f"💰 Твой баланс: {amount:.2f} ₽", reply_markup=kb)

@dp.callback_query(F.data == "withdraw:start")
async def withdraw_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(WithdrawFlow.amount)
    await callback.answer()
    if callback.message:
        commission = await get_price("withdraw_commission")
        await callback.message.answer(
            f"💸 Вывод средств\n\nКомиссия на вывод: {commission:.2f}%\n"
            "Комиссия будет удержана из суммы вывода. На карту поступит сумма за вычетом комиссии.\n\n"
            "Введи сумму от 100 ₽:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отказаться", callback_data="withdraw:cancel")]
            ])
        )

@dp.callback_query(F.data == "withdraw:cancel")
async def withdraw_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass
    await callback.answer("Вывод отменён.")

@dp.message(WithdrawFlow.amount)
async def withdraw_amount(message: Message, state: FSMContext):
    try:
        amount = Decimal((message.text or "").replace(",", ".").strip()).quantize(Decimal("0.01"))
        if amount < Decimal("100") or amount > Decimal("1000000"): raise InvalidOperation
    except (InvalidOperation, ValueError):
        await message.answer(
            "Введи сумму от 100 ₽.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отказаться", callback_data="withdraw:cancel")]
            ])
        ); return
    if await get_balance(message.from_user.id) < amount:
        await state.clear()
        await message.answer("Недостаточно средств.", reply_markup=insufficient_balance_keyboard()); return
    commission = await get_price("withdraw_commission")
    payout = (amount * (Decimal("100") - commission) / Decimal("100")).quantize(Decimal("0.01"))
    await state.update_data(withdraw_amount=str(amount), withdraw_commission=str(commission), withdraw_payout=str(payout))
    await state.set_state(WithdrawFlow.phone)
    await message.answer(
        f"Сумма вывода: {amount:.2f} ₽\n"
        f"Комиссия ({commission:.2f}%): {amount - payout:.2f} ₽\n"
        f"На карту поступит: {payout:.2f} ₽\n\n"
        "Теперь укажи номер телефона для получения перевода:"
    )

@dp.message(WithdrawFlow.phone)
async def withdraw_phone(message: Message, state: FSMContext):
    phone = (message.text or "").strip()
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 10 or len(digits) > 15:
        await message.answer("Проверь номер телефона и введи ещё раз."); return
    await state.update_data(withdraw_phone=phone)
    await state.set_state(WithdrawFlow.bank)
    await message.answer("Укажи название банка:")

@dp.message(WithdrawFlow.bank)
async def withdraw_bank(message: Message, state: FSMContext, bot: Bot):
    bank = (message.text or "").strip()
    if len(bank) < 2 or len(bank) > 80:
        await message.answer("Введи название банка."); return
    data = await state.get_data()
    amount = Decimal(data["withdraw_amount"])
    uid = message.from_user.id
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            updated = await conn.fetchval("UPDATE nobody_users SET balance=balance-$2 WHERE telegram_id=$1 AND balance >= $2 RETURNING balance", uid, amount)
            if updated is None:
                await state.clear()
                await message.answer("Недостаточно средств. Заявка не создана.", reply_markup=insufficient_balance_keyboard()); return
            wid = await conn.fetchval("INSERT INTO nobody_withdrawals (user_id,amount,phone,bank,commission_percent,payout_amount) VALUES ($1,$2,$3,$4,$5,$6) RETURNING id", uid, amount, data["withdraw_phone"], bank, Decimal(data["withdraw_commission"]), Decimal(data["withdraw_payout"]))
            await conn.execute("INSERT INTO nobody_transactions (user_id,amount,kind,description,ref) VALUES ($1,$2,'debit','Заявка на вывод средств',$3)", uid, -amount, f"withdraw:{wid}")
    await state.clear()
    commission = Decimal(data["withdraw_commission"])
    payout = Decimal(data["withdraw_payout"])
    await message.answer(
        f"✅ Заявка на вывод №{wid} отправлена.\n\n"
        f"Сумма списания: {amount:.2f} ₽\nКомиссия ({commission:.2f}%): {amount - payout:.2f} ₽\n"
        f"К получению: {payout:.2f} ₽\n\nСредства списаны до проверки."
    )
    user = message.from_user
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Выплачено", callback_data=f"withdraw:approve:{wid}"), InlineKeyboardButton(text="❌ Отклонить и вернуть", callback_data=f"withdraw:reject:{wid}")]])
    await bot.send_message(ADMIN_ID, f"💸 ЗАЯВКА НА ВЫВОД №{wid}\n\nСписать с баланса: {amount:.2f} ₽\n"
        f"Комиссия ({commission:.2f}%): {amount - payout:.2f} ₽\nК выплате: {payout:.2f} ₽\n"
        f"Телефон: {data['withdraw_phone']}\nБанк: {bank}\nПользователь: {user.full_name}\n"
        f"Username: @{user.username if user.username else 'не указан'}\nTelegram ID: {uid}", reply_markup=kb)

@dp.callback_query(F.data.startswith("withdraw:approve:"))
async def withdraw_approve(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True); return
    wid = int(callback.data.rsplit(":",1)[1])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("UPDATE nobody_withdrawals SET status='approved',reviewed_at=NOW() WHERE id=$1 AND status='pending' RETURNING user_id,amount,payout_amount", wid)
    if not row:
        await callback.answer("Заявка уже обработана.", show_alert=True); return
    await callback.answer("Отмечено как выплачено.")
    if callback.message: await callback.message.edit_reply_markup(reply_markup=None)
    await callback.bot.send_message(row["user_id"], f"✅ Вывод №{wid} отмечен как выплаченный. На карту отправлено: {Decimal(row['payout_amount']):.2f} ₽.")

@dp.callback_query(F.data.startswith("withdraw:reject:"))
async def withdraw_reject(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True); return
    wid = int(callback.data.rsplit(":",1)[1])
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("UPDATE nobody_withdrawals SET status='rejected',reviewed_at=NOW() WHERE id=$1 AND status='pending' RETURNING user_id,amount", wid)
            if row:
                await conn.execute("UPDATE nobody_users SET balance=balance+$2 WHERE telegram_id=$1", row["user_id"], row["amount"])
                await conn.execute("INSERT INTO nobody_transactions (user_id,amount,kind,description,ref) VALUES ($1,$2,'credit','Возврат отклонённого вывода',$3)", row["user_id"], row["amount"], f"withdraw-refund:{wid}")
    if not row:
        await callback.answer("Заявка уже обработана.", show_alert=True); return
    await callback.answer("Средства возвращены.")
    if callback.message: await callback.message.edit_reply_markup(reply_markup=None)
    await callback.bot.send_message(row["user_id"], f"❌ Вывод №{wid} отклонён. {Decimal(row['amount']):.2f} ₽ возвращены на баланс.")

@dp.callback_query(F.data == "topup:start")
async def topup_start(callback: CallbackQuery, state: FSMContext):
    saved = await state.get_data()
    if not saved.get("resume_gift"):
        await state.clear()
    await state.set_state(TopUpFlow.amount)
    await callback.answer()
    if callback.message:
        await send_flow_message(
            callback.bot, callback.from_user.id, state,
            "💳 Введи сумму пополнения в рублях:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отменить пополнение", callback_data="topup:cancel")]
            ])
        )

@dp.message(TopUpFlow.amount)
async def topup_amount(message: Message, state: FSMContext):
    try:
        amount = Decimal((message.text or "").replace(",", ".").strip()).quantize(Decimal("0.01"))
        if amount <= 0 or amount > Decimal("1000000"):
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        await message.answer("Введи корректную сумму больше 0 ₽.")
        return
    try:
        await message.delete()
    except Exception:
        pass
    await state.update_data(topup_amount=str(amount))
    await state.set_state(TopUpFlow.sender)
    await send_flow_message(
        message.bot, message.chat.id, state,
        "Укажи имя и фамилию отправителя — как будет указано в переводе:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отменить пополнение", callback_data="topup:cancel")]
        ])
    )

@dp.message(TopUpFlow.sender)
async def topup_sender(message: Message, state: FSMContext):
    sender_name = (message.text or "").strip()
    if len(sender_name) < 2 or len(sender_name) > 80:
        try:
            await message.delete()
        except Exception:
            pass
        await send_flow_message(
            message.bot, message.chat.id, state,
            "Укажи имя отправителя (от 2 до 80 символов).",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отменить пополнение", callback_data="topup:cancel")]
            ])
        )
        return
    try:
        await message.delete()
    except Exception:
        pass
    await state.update_data(sender_name=sender_name)
    await state.set_state(TopUpFlow.confirm)
    data = await state.get_data()
    amount = Decimal(data["topup_amount"])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я отправил оплату", callback_data="topup:sent")],
        [InlineKeyboardButton(text="❌ Отменить пополнение", callback_data="topup:cancel")],
    ])
    await send_flow_message(
        message.bot, message.chat.id, state,
        f"💳 Пополнение баланса\n\nСумма: {amount:.2f} ₽\nОтправитель: {sender_name}\n\n"
        "Переведи указанную сумму по реквизитам:\n"
        "🏦 Сбербанк\nНомер: +79607178182\n\n"
        "После перевода нажми «Я отправил оплату». Баланс будет пополнен после проверки администратором.",
        reply_markup=kb
    )

@dp.callback_query(F.data == "topup:cancel")
async def topup_cancel(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await clear_flow_message(bot, callback.from_user.id, state)
    await state.clear()
    await callback.answer("Пополнение отменено.")
    if callback.message:
        await callback.message.answer("❌ Пополнение отменено.")

@dp.callback_query(F.data == "topup:sent")
async def topup_sent(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if not data.get("topup_amount") or not data.get("sender_name"):
        await callback.answer("Начни пополнение заново через раздел «Баланс».", show_alert=True)
        return
    amount = Decimal(data["topup_amount"])
    user = callback.from_user
    async with db_pool.acquire() as conn:
        topup_id = await conn.fetchval("""
            INSERT INTO nobody_topups (user_id, amount, sender_name)
            VALUES ($1, $2, $3) RETURNING id
        """, user.id, amount, data["sender_name"])
    await clear_flow_message(bot, callback.from_user.id, state)
    if data.get("resume_gift"):
        await state.set_state(None)
    else:
        await state.clear()
    await callback.answer("Заявка отправлена администратору!")
    if callback.message:
        await callback.message.answer("✅ Заявка на пополнение отправлена. Баланс будет зачислен после проверки оплаты.")
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"topup:approve:{topup_id}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"topup:reject:{topup_id}")]
    ])
    username = f"@{user.username}" if user.username else "не указан"
    try:
        await bot.send_message(
            ADMIN_ID,
            f"💳 НОВАЯ ЗАЯВКА НА ПОПОЛНЕНИЕ №{topup_id}\n\n"
            f"Сумма: {amount:.2f} ₽\nОтправитель: {data['sender_name']}\n"
            f"Пользователь: {user.full_name}\nUsername: {username}\nTelegram ID: {user.id}",
            reply_markup=admin_kb
        )
    except Exception:
        log.exception("Could not notify admin about top-up %s", topup_id)

@dp.callback_query(F.data.startswith("topup:approve:"))
async def topup_approve(callback: CallbackQuery, bot: Bot):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    topup_id = int(callback.data.rsplit(":", 1)[1])
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT * FROM nobody_topups WHERE id=$1 FOR UPDATE", topup_id)
            if not row or row["status"] != "pending":
                await callback.answer("Заявка уже обработана или не найдена.", show_alert=True)
                return
            inserted = await conn.fetchval("""
                INSERT INTO nobody_transactions (user_id, amount, kind, description, ref)
                VALUES ($1, $2, 'credit', $3, $4)
                ON CONFLICT (ref) DO NOTHING RETURNING id
            """, row["user_id"], row["amount"], f"Пополнение по заявке №{topup_id}", f"topup:{topup_id}")
            if inserted:
                await conn.execute("UPDATE nobody_users SET balance=balance+$2 WHERE telegram_id=$1", row["user_id"], row["amount"])
            await conn.execute("UPDATE nobody_topups SET status='approved', reviewed_at=NOW() WHERE id=$1", topup_id)
    await callback.answer("Пополнение подтверждено.")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"✅ Заявка №{topup_id} подтверждена. Начислено {Decimal(row['amount']):.2f} ₽.")
    pending_gift = None
    try:
        async with db_pool.acquire() as conn:
            pending_gift = await conn.fetchrow(
                "SELECT recipient_id,body FROM nobody_pending_gifts WHERE sender_id=$1",
                row["user_id"]
            )
        if pending_gift:
            user_state = await dp.fsm.get_context(bot=bot, chat_id=row["user_id"], user_id=row["user_id"])
            await user_state.update_data(
                recipient_id=pending_gift["recipient_id"],
                body=pending_gift["body"],
                category="gift",
                resume_gift=False
            )
            await user_state.set_state(GiftFlow.amount)
    except Exception:
        log.exception("Could not restore gift flow after top-up %s", topup_id)
    try:
        if pending_gift:
            resume_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎁 Вернуться к подарку", callback_data="gift:resume")]
            ])
            await bot.send_message(
                row["user_id"],
                f"✅ Оплата подтверждена! На баланс зачислено {Decimal(row['amount']):.2f} ₽.\n\n"
                "Твой подарок сохранён. Нажми кнопку ниже, чтобы продолжить и указать сумму:",
                reply_markup=resume_kb
            )
        else:
            await bot.send_message(
                row["user_id"],
                f"✅ Оплата подтверждена! На баланс зачислено {Decimal(row['amount']):.2f} ₽."
            )
    except Exception:
        log.exception("Could not notify user about approved top-up %s", topup_id)

@dp.callback_query(F.data.startswith("topup:reject:"))
async def topup_reject(callback: CallbackQuery, bot: Bot):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    topup_id = int(callback.data.rsplit(":", 1)[1])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE nobody_topups SET status='rejected', reviewed_at=NOW()
            WHERE id=$1 AND status='pending' RETURNING user_id, amount
        """, topup_id)
    if not row:
        await callback.answer("Заявка уже обработана или не найдена.", show_alert=True)
        return
    await callback.answer("Заявка отклонена.")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"❌ Заявка №{topup_id} отклонена.")
    try:
        await bot.send_message(row["user_id"], f"❌ Заявка на пополнение №{topup_id} отклонена. Если считаешь это ошибкой, обратись в поддержку.")
    except Exception:
        log.exception("Could not notify user about rejected top-up %s", topup_id)

@dp.message(F.text == "👤 Профиль")
async def profile(message: Message):
    user = message.from_user
    await message.answer(f"👤 Профиль VIP NOBODY\n\nИмя: {user.first_name or 'не указано'}\nUsername: @{user.username if user.username else 'не указан'}\nTelegram ID: {user.id}")

@dp.message(F.text == "ℹ️ Помощь")
async def help_message(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆘 Обратиться в поддержку", callback_data="support:start")]
    ])
    card_price = await get_price("card_price")
    reveal_price = await get_price("reveal_price")
    await message.answer(
        "💜 VIP NOBODY — место для анонимных признаний\n\n"
        "Здесь можно отправлять людям анонимные сообщения, признаваться в чувствах, "
        "задавать вопросы, делать комплименты или просто оставлять интересные послания.\n\n"
        "Как это работает?\n"
        "1. Открой раздел «Моя ссылка».\n"
        "2. Поделись своей ссылкой с друзьями.\n"
        "3. Получай анонимные сообщения.\n"
        "4. Отвечай отправителям анонимно.\n\n"
        "Дополнительные возможности:\n"
        f"❤️ Фирменная открытка к сообщению — {card_price:.2f} ₽.\n"
        f"🔎 Раскрытие личности отправителя — {reveal_price:.2f} ₽.\n\n"
        "Если возник вопрос или проблема — напиши в поддержку.",
        reply_markup=kb
    )

SUPPORT_SIGNATURE = "\n\n— Сообщение от поддержки VIP NOBODY"

def support_user_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Ответить администрации", callback_data=f"support:user_reply:{ticket_id}")],
        [InlineKeyboardButton(text="🔒 Завершить диалог", callback_data=f"support:user_close:{ticket_id}")]
    ])

def support_user_reply_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Ответить администрации", callback_data=f"support:user_reply:{ticket_id}")]
    ])

def support_admin_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Ответить", callback_data=f"support:admin_reply:{ticket_id}"),
         InlineKeyboardButton(text="🔒 Завершить диалог", callback_data=f"support:close:{ticket_id}")]
    ])

async def resolve_support_user(value: str):
    value = value.strip()
    async with db_pool.acquire() as conn:
        if value.startswith("@"):
            return await conn.fetchrow(
                "SELECT telegram_id, username, first_name FROM nobody_users WHERE LOWER(username)=LOWER($1)",
                value[1:]
            )
        try:
            user_id = int(value)
        except ValueError:
            return None
        return await conn.fetchrow(
            "SELECT telegram_id, username, first_name FROM nobody_users WHERE telegram_id=$1",
            user_id
        )

@dp.callback_query(F.data == "support:start")
async def support_start(callback: CallbackQuery, state: FSMContext):
    async with db_pool.acquire() as conn:
        blocked = await conn.fetchval("SELECT 1 FROM nobody_support_blocks WHERE user_id=$1", callback.from_user.id)
        ticket = await conn.fetchrow(
            "SELECT id FROM nobody_support_tickets WHERE user_id=$1 AND status='open' ORDER BY id DESC LIMIT 1",
            callback.from_user.id
        )
        ticket_id = ticket["id"] if ticket else None
        if not blocked and not ticket_id:
            ticket_id = await conn.fetchval(
                "INSERT INTO nobody_support_tickets (user_id) VALUES ($1) RETURNING id",
                callback.from_user.id
            )
    await callback.answer()
    if blocked:
        if callback.message:
            await callback.message.answer("Обращения в поддержку для твоего аккаунта временно недоступны.")
        return
    await state.clear()
    await state.update_data(support_ticket_id=ticket_id)
    if ticket:
        await state.set_state(SupportFlow.user_reply)
        if callback.message:
            await callback.message.answer(
                "У тебя уже есть открытый диалог с поддержкой. Если поддержка ответит, здесь появится кнопка для ответа."
            )
    else:
        await state.set_state(SupportFlow.new_text)
        if callback.message:
            await callback.message.answer("Опиши проблему или вопрос одним сообщением. Текст будет передан администрации.")

async def notify_admin_support(bot: Bot, ticket_id: int, user_id: int, body: str, first: bool = False):
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT username, first_name, last_name FROM nobody_users WHERE telegram_id=$1", user_id
        )
    username = f"@{user['username']}" if user and user["username"] else "не указан"
    name = " ".join(x for x in [user["first_name"], user["last_name"]] if x) if user else "неизвестно"
    prefix = "🆘 НОВОЕ ОБРАЩЕНИЕ" if first else "💬 СООБЩЕНИЕ В ПОДДЕРЖКУ"
    await bot.send_message(
        ADMIN_ID,
        f"{prefix}\n\nПользователь: {name}\nUsername: {username}\nTelegram ID: {user_id}\n\n{body}",
        reply_markup=support_admin_keyboard(ticket_id)
    )

@dp.message(SupportFlow.new_text)
async def support_new_text(message: Message, state: FSMContext, bot: Bot):
    body = (message.text or "").strip()
    if not body or len(body) > 3000:
        await message.answer("Опиши проблему текстом до 3000 символов.")
        return
    ticket_id = (await state.get_data()).get("support_ticket_id")
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO nobody_support_messages (ticket_id,sender_id,sender_role,body) VALUES ($1,$2,'user',$3)",
            ticket_id, message.from_user.id, body
        )
        await conn.execute("UPDATE nobody_support_tickets SET updated_at=NOW() WHERE id=$1", ticket_id)
    await state.set_state(SupportFlow.user_reply)
    await message.answer(
        "✅ Обращение отправлено администрации. Ожидай ответа поддержки."
    )
    try:
        await notify_admin_support(bot, ticket_id, message.from_user.id, body, True)
    except Exception:
        log.exception("Could not notify admin about support ticket %s", ticket_id)

@dp.callback_query(F.data.startswith("support:user_reply:"))
async def support_user_reply_start(callback: CallbackQuery, state: FSMContext):
    ticket_id = int(callback.data.rsplit(":", 1)[1])
    async with db_pool.acquire() as conn:
        ticket = await conn.fetchrow("SELECT user_id,status FROM nobody_support_tickets WHERE id=$1", ticket_id)
        blocked = await conn.fetchval("SELECT 1 FROM nobody_support_blocks WHERE user_id=$1", callback.from_user.id)
    if not ticket or ticket["user_id"] != callback.from_user.id or ticket["status"] != "open" or blocked:
        await callback.answer("Этот диалог закрыт или недоступен.", show_alert=True)
        return
    await state.clear()
    await state.update_data(support_ticket_id=ticket_id)
    await state.set_state(SupportFlow.user_reply)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Напиши сообщение для администрации:")

@dp.message(SupportFlow.user_reply)
async def support_user_reply(message: Message, state: FSMContext, bot: Bot):
    body = (message.text or "").strip()
    if not body or len(body) > 3000:
        await message.answer("Напиши текст до 3000 символов.")
        return
    ticket_id = (await state.get_data()).get("support_ticket_id")
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            ticket = await conn.fetchrow("SELECT user_id,status FROM nobody_support_tickets WHERE id=$1 FOR UPDATE", ticket_id)
            blocked = await conn.fetchval("SELECT 1 FROM nobody_support_blocks WHERE user_id=$1", message.from_user.id)
            if not ticket or ticket["user_id"] != message.from_user.id or ticket["status"] != "open" or blocked:
                await state.clear()
                await message.answer("Диалог закрыт или обращения в поддержку недоступны.")
                return
            await conn.execute(
                "INSERT INTO nobody_support_messages (ticket_id,sender_id,sender_role,body) VALUES ($1,$2,'user',$3)",
                ticket_id, message.from_user.id, body
            )
            await conn.execute("UPDATE nobody_support_tickets SET updated_at=NOW() WHERE id=$1", ticket_id)
    await message.answer("✅ Сообщение отправлено администрации. Ожидай ответа поддержки.")
    try:
        await notify_admin_support(bot, ticket_id, message.from_user.id, body)
    except Exception:
        log.exception("Could not forward user reply in ticket %s", ticket_id)

@dp.callback_query(F.data.startswith("support:admin_reply:"))
async def support_admin_reply_start(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    ticket_id = int(callback.data.rsplit(":", 1)[1])
    async with db_pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM nobody_support_tickets WHERE id=$1", ticket_id)
    if status != "open":
        await callback.answer("Диалог уже закрыт.", show_alert=True)
        return
    await state.clear()
    await state.update_data(support_ticket_id=ticket_id)
    await state.set_state(SupportFlow.admin_reply)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Введи ответ пользователю:")

@dp.message(SupportFlow.admin_reply)
async def support_admin_reply(message: Message, state: FSMContext, bot: Bot):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    body = (message.text or "").strip()
    if not body or len(body) > 3000:
        await message.answer("Введи текст ответа до 3000 символов.")
        return
    ticket_id = (await state.get_data()).get("support_ticket_id")
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            ticket = await conn.fetchrow("SELECT user_id,status,user_can_close FROM nobody_support_tickets WHERE id=$1 FOR UPDATE", ticket_id)
            if not ticket or ticket["status"] != "open":
                await state.clear()
                await message.answer("Диалог уже закрыт.")
                return
            await conn.execute(
                "INSERT INTO nobody_support_messages (ticket_id,sender_id,sender_role,body) VALUES ($1,$2,'admin',$3)",
                ticket_id, message.from_user.id, body
            )
            await conn.execute("UPDATE nobody_support_tickets SET updated_at=NOW() WHERE id=$1", ticket_id)
    try:
        await bot.send_message(
            ticket["user_id"], f"💬 Ответ поддержки:\n\n{body}{SUPPORT_SIGNATURE}",
            reply_markup=(support_user_keyboard(ticket_id) if ticket["user_can_close"] else support_user_reply_keyboard(ticket_id))
        )
        await message.answer("✅ Ответ отправлен.")
    except Exception:
        log.exception("Could not deliver support response for ticket %s", ticket_id)
        await message.answer("Не удалось доставить ответ пользователю.")
    await state.clear()

@dp.callback_query(F.data.startswith("support:user_close:"))
async def support_user_close(callback: CallbackQuery, bot: Bot):
    ticket_id = int(callback.data.rsplit(":", 1)[1])
    async with db_pool.acquire() as conn:
        user_id = await conn.fetchval(
            "UPDATE nobody_support_tickets SET status='closed',updated_at=NOW() "
            "WHERE id=$1 AND user_id=$2 AND status='open' RETURNING user_id",
            ticket_id, callback.from_user.id
        )
    if not user_id:
        await callback.answer("Диалог уже закрыт или не найден.", show_alert=True)
        return
    await callback.answer("Диалог завершён.")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("🔒 Ты завершил диалог с поддержкой.")
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🔒 Пользователь завершил диалог с поддержкой. Telegram ID: {user_id}"
        )
    except Exception:
        log.exception("Could not notify admin that user closed support ticket %s", ticket_id)


@dp.callback_query(F.data.startswith("support:close:"))
async def support_close(callback: CallbackQuery, bot: Bot):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    ticket_id = int(callback.data.rsplit(":", 1)[1])
    async with db_pool.acquire() as conn:
        user_id = await conn.fetchval(
            "UPDATE nobody_support_tickets SET status='closed',updated_at=NOW() WHERE id=$1 AND status='open' RETURNING user_id",
            ticket_id
        )
    if not user_id:
        await callback.answer("Диалог уже закрыт или не найден.", show_alert=True)
        return
    await callback.answer("Диалог завершён.")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
    try:
        await bot.send_message(user_id, "🔒 Диалог с поддержкой завершён администрацией.")
    except Exception:
        log.exception("Could not notify user about closed support ticket %s", ticket_id)

@dp.callback_query(F.data == "support:direct")
async def support_direct_start(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    await state.set_state(SupportFlow.direct_target)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Введи Telegram ID или @username пользователя. Он должен ранее запустить бота.")

@dp.message(SupportFlow.direct_target)
async def support_direct_target(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    user = await resolve_support_user(message.text or "")
    if not user:
        await message.answer("Не нашёл пользователя. Проверь ID или @username — пользователь должен запускать бота.")
        return
    await state.update_data(direct_user_id=user["telegram_id"])
    await state.set_state(SupportFlow.direct_text)
    await message.answer(f"Получатель найден: {user['first_name']} (ID {user['telegram_id']}). Теперь введи сообщение.")

@dp.message(SupportFlow.direct_text)
async def support_direct_text(message: Message, state: FSMContext, bot: Bot):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    body = (message.text or "").strip()
    if not body or len(body) > 3000:
        await message.answer("Введи текст до 3000 символов.")
        return
    user_id = (await state.get_data()).get("direct_user_id")
    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                ticket_id = await conn.fetchval(
                    "INSERT INTO nobody_support_tickets (user_id,user_can_close) VALUES ($1,FALSE) RETURNING id",
                    user_id
                )
                await conn.execute(
                    "INSERT INTO nobody_support_messages (ticket_id,sender_id,sender_role,body) VALUES ($1,$2,'admin',$3)",
                    ticket_id, message.from_user.id, body
                )
        await bot.send_message(
            user_id,
            f"💌 Личное сообщение администрации:\n\n{body}{SUPPORT_SIGNATURE}",
            reply_markup=support_user_reply_keyboard(ticket_id)
        )
        await message.answer("✅ Сообщение отправлено. Пользователь сможет ответить, а завершить диалог сможешь только ты.")
    except Exception:
        log.exception("Could not send direct support message to %s", user_id)
        await message.answer("Не удалось отправить сообщение. Возможно, пользователь заблокировал бота.")
    await state.clear()

@dp.callback_query(F.data == "support:block")
async def support_block_start(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    await state.set_state(SupportFlow.block_target)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Введи Telegram ID или @username для блокировки. Чтобы снять запрет, введи: unblock ID или unblock @username."
        )

@dp.message(SupportFlow.block_target)
async def support_block_target(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    value = (message.text or "").strip()
    removing = value.lower().startswith("unblock ")
    lookup = value[8:].strip() if removing else value
    target = await resolve_support_user(lookup)
    if not target:
        await message.answer("Пользователь не найден. Проверь ID или @username.")
        return
    async with db_pool.acquire() as conn:
        if removing:
            await conn.execute("DELETE FROM nobody_support_blocks WHERE user_id=$1", target["telegram_id"])
        else:
            await conn.execute(
                "INSERT INTO nobody_support_blocks (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
                target["telegram_id"]
            )
    await state.clear()
    if removing:
        await message.answer(f"✅ Запрет обращений снят для ID {target['telegram_id']}.")
    else:
        await message.answer(f"🚫 Обращения в поддержку заблокированы для ID {target['telegram_id']}.")

@dp.message(Command("admin"))
@dp.message(F.text == "🛠 Админ-панель")
async def admin_panel(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await message.answer("Нет доступа."); return
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Начислить баланс", callback_data="admin:credit"),
         InlineKeyboardButton(text="➖ Списать баланс", callback_data="admin:debit")],
        [InlineKeyboardButton(text="✉️ Написать пользователю", callback_data="support:direct")],
        [InlineKeyboardButton(text="🚫 Блокировка поддержки", callback_data="support:block")],
        [InlineKeyboardButton(text="💵 Изменить цены", callback_data="admin:prices")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="🏆 Топ 10 активных", callback_data="admin:top_active")],
        [InlineKeyboardButton(text="🧹 Сбросить тестовые данные", callback_data="admin:reset_stats")],
        [InlineKeyboardButton(text="👁 Просмотр", callback_data="admin:view_messages")],
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
    elif action == "prices":
        card_price = await get_price("card_price")
        reveal_price = await get_price("reveal_price")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"❤️ Цена открытки: {card_price:.2f} ₽", callback_data="price:card")],
            [InlineKeyboardButton(text=f"🔎 Цена раскрытия: {reveal_price:.2f} ₽", callback_data="price:reveal")],
            [InlineKeyboardButton(text=f"💸 Комиссия вывода: {await get_price('withdraw_commission'):.2f}%", callback_data="price:withdraw")],
        ])
        if callback.message:
            await callback.message.answer("💵 НАСТРОЙКА ЦЕН\n\nВыбери, какую цену изменить:", reply_markup=kb)
    elif action == "stats":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Люди", callback_data="stats:people")],
            [InlineKeyboardButton(text="💰 Заработок", callback_data="stats:earnings")],
        ])
        if callback.message:
            await callback.message.answer("📊 СТАТИСТИКА VIP NOBODY\n\nВыбери раздел:", reply_markup=kb)

    elif action == "reset_stats":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, обнулить всё", callback_data="admin:reset_confirm")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:reset_cancel")],
        ])
        if callback.message:
            await callback.message.answer(
                "⚠️ СБРОС ТЕСТОВЫХ ДАННЫХ\n\n"
                "У всех пользователей, включая администратора, баланс станет 0 ₽.\n"
                "История тестовых пополнений и списаний будет очищена из аналитики.\n\n"
                "Пользователи, сообщения, подарки и обращения в поддержку останутся.\n\n"
                "Подтверди сброс:", reply_markup=kb
            )
    elif action == "reset_cancel":
        if callback.message:
            await callback.message.answer("Сброс отменён. Данные не изменены.")
    elif action == "reset_confirm":
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("UPDATE nobody_users SET balance=0")
                await conn.execute("DELETE FROM nobody_transactions")
                await conn.execute("DELETE FROM nobody_topups")
        if callback.message:
            await callback.message.answer(
                "✅ Тестовые данные обнулены.\n\n"
                "Балансы всех пользователей: 0 ₽\n"
                "История пополнений и трат: очищена.\n"
                "Пользователи и переписки сохранены."
            )

    elif action == "top_active":
        async with db_pool.acquire() as conn:
            users = await conn.fetch(
                "SELECT username, first_name, last_active_at, activity_count "
                "FROM nobody_users WHERE last_active_at IS NOT NULL "
                "ORDER BY activity_count DESC, last_active_at DESC LIMIT 10"
            )
        if callback.message:
            if not users:
                await callback.message.answer("Пока нет активных пользователей.")
            else:
                lines = ["🏆 ТОП 10 АКТИВНЫХ ПОЛЬЗОВАТЕЛЕЙ\n"]
                for index, user in enumerate(users, 1):
                    username = f"@{user['username']}" if user["username"] else "username не указан"
                    name = user["first_name"] or "Без имени"
                    active_at = user["last_active_at"].strftime("%d.%m.%Y %H:%M")
                    lines.append(f"{index}. {username} — {name}\n   Действий: {user['activity_count']} · Последняя активность: {active_at}")
                await callback.message.answer("\n".join(lines))
    elif action == "view_messages":
        await state.clear()
        await state.set_state(AdminFlow.message_view_user)
        if callback.message:
            await callback.message.answer("Введи @username пользователя, чтобы посмотреть последние 10 сообщений, которые ему отправляли.")


@dp.message(AdminFlow.message_view_user)
async def admin_view_user_messages(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    username = (message.text or "").strip().lstrip("@").lower()
    if not username or len(username) > 64 or any(ch.isspace() for ch in username):
        await message.answer("Введи корректный @username пользователя.")
        return
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT telegram_id, username FROM nobody_users WHERE LOWER(username)=$1",
            username
        )
        rows = []
        if user:
            rows = await conn.fetch(
                "SELECT category, body, created_at FROM ("
                "SELECT category, body, created_at, id AS sort_id FROM nobody_messages WHERE recipient_id=$1 "
                "UNION ALL "
                "SELECT 'gift' AS category, body, created_at, id AS sort_id FROM nobody_gifts WHERE recipient_id=$1"
                ") AS received ORDER BY created_at DESC, sort_id DESC LIMIT 10",
                user["telegram_id"]
            )
    if not user:
        await message.answer("Пользователь с таким username не найден. Проверь написание.")
        return
    await state.clear()
    if not rows:
        await message.answer(f"У @{user['username']} пока нет полученных анонимных сообщений.")
        return
    lines = [f"👁 ПОСЛЕДНИЕ 10 СООБЩЕНИЙ ДЛЯ @{user['username']}\n"]
    for index, row in enumerate(rows, 1):
        category = CATEGORIES.get(row["category"], "💬 Другое")
        created = row["created_at"].strftime("%d.%m.%Y %H:%M")
        body = row["body"].strip()
        if len(body) > 300:
            body = body[:300].rstrip() + "…"
        lines.append(f"{index}. {category} · {created}\n{body}")
    await message.answer("\n\n".join(lines))



@dp.callback_query(F.data == "stats:people")
async def stats_people(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    async with db_pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM nobody_users")
        week = await conn.fetchval(
            "SELECT COUNT(*) FROM nobody_users WHERE last_active_at >= NOW() - INTERVAL '7 days'"
        )
        today = await conn.fetchval(
            "SELECT COUNT(*) FROM nobody_users WHERE last_active_at >= CURRENT_DATE"
        )
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            f"👥 ЛЮДИ\n\n"
            f"Всего людей: {total}\n"
            f"Активность за неделю: {week}\n"
            f"Активность сегодня: {today}"
        )

@dp.callback_query(F.data == "stats:earnings")
async def stats_earnings(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    async with db_pool.acquire() as conn:
        balance = await conn.fetchval(
            "SELECT COALESCE(SUM(balance), 0) FROM nobody_users"
        )
        spent = await conn.fetchval(
            "SELECT COALESCE(-SUM(amount), 0) FROM nobody_transactions WHERE amount < 0 AND description <> 'Ручное списание администратором'"
        )
        topups_today = await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM nobody_topups "
            "WHERE status='approved' AND reviewed_at >= CURRENT_DATE"
        )
        spent_today = await conn.fetchval(
            "SELECT COALESCE(-SUM(amount), 0) FROM nobody_transactions "
            "WHERE amount < 0 AND description <> 'Ручное списание администратором' AND created_at >= CURRENT_DATE"
        )
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            f"💰 ЗАРАБОТОК\n\n"
            f"Общий баланс людей: {Decimal(balance):.2f} ₽\n"
            f"Всего потрачено: {Decimal(spent):.2f} ₽\n\n"
            f"Пополнения за сегодня: {Decimal(topups_today):.2f} ₽\n"
            f"Траты за сегодня: {Decimal(spent_today):.2f} ₽"
        )

@dp.callback_query(F.data.in_({"price:card", "price:reveal", "price:withdraw"}))
async def admin_price_start(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    kind = {"price:card": "card_price", "price:reveal": "reveal_price", "price:withdraw": "withdraw_commission"}[callback.data]
    title = {"card_price": "открытки", "reveal_price": "раскрытия личности", "withdraw_commission": "комиссию вывода"}[kind]
    await state.clear()
    await state.update_data(price_kind=kind)
    await state.set_state(AdminFlow.price_value)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Введи комиссию вывода в процентах (от 0 до 100):" if kind == "withdraw_commission" else f"Введи новую цену {title} в рублях:")

@dp.message(AdminFlow.price_value)
async def admin_price_save(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        value = Decimal((message.text or "").replace(",", ".").strip()).quantize(Decimal("0.01"))
        data_for_limit = await state.get_data()
        limit = Decimal("100") if data_for_limit.get("price_kind") == "withdraw_commission" else Decimal("1000000")
        if value < 0 or value > limit:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        await message.answer("Введи корректное значение: комиссия от 0 до 100%, цена от 0 до 1 000 000 ₽.")
        return
    data = await state.get_data()
    key = data.get("price_kind")
    if key not in ("card_price", "reveal_price", "withdraw_commission"):
        await state.clear()
        await message.answer("Не удалось определить тип цены. Открой настройки заново.")
        return
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO nobody_settings (key,value) VALUES ($1,$2) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            key, str(value)
        )
    await state.clear()
    label = {"card_price": "открытки", "reveal_price": "раскрытия личности", "withdraw_commission": "комиссии вывода"}[key]
    suffix = "%" if key == "withdraw_commission" else " ₽"
    await message.answer(f"✅ Новое значение {label}: {value:.2f}{suffix}")

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
