import asyncio
import logging
import os
from urllib.parse import quote
from decimal import Decimal, InvalidOperation

import asyncpg
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import TelegramNetworkError, TelegramServerError
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
    "heart": {"title": "❤️ Красное сердечко"},
    "rose": {"title": "🌹 Роза"},
    "broken": {"title": "💔 Разбитое сердце"},
    "crown": {"title": "👑 Корона VIP"},
    "fire": {"title": "🔥 Огонь"},
    "gift": {"title": "🎁 Подарок"},
    "envelope": {"title": "💌 Тайный конверт"},
    "fun": {"title": "😂 Прикол"},
}

class AnonymousFlow(StatesGroup):
    category = State()
    text = State()
    confirm = State()

class AdminFlow(StatesGroup):
    balance_user = State()
    balance_amount = State()
    price_value = State()
    message_view_user = State()
    broadcast_text = State()
    broadcast_confirm = State()
    referral_bonus_amount = State()

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

class PartnerWithdrawFlow(StatesGroup):
    amount = State()
    phone = State()
    bank = State()

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
        [KeyboardButton(text="💼 Мой заработок")],
        [KeyboardButton(text="ℹ️ Помощь")],
        [KeyboardButton(text="⬅️ Главное меню")],
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
        [InlineKeyboardButton(text="⬅️ В главное меню", callback_data="nav:home")],
    ])

def message_send_options_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить", callback_data="card:free")],
        [InlineKeyboardButton(text="🖼 Открытки", callback_data="card:choose")],
    ])

def postcard_keyboard(price: Decimal) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❤️ Красное сердечко", callback_data="card:select:heart")],
        [InlineKeyboardButton(text="🌹 Роза", callback_data="card:select:rose")],
        [InlineKeyboardButton(text="💔 Разбитое сердце", callback_data="card:select:broken")],
        [InlineKeyboardButton(text="💌 Тайный конверт", callback_data="card:select:envelope")],
        [InlineKeyboardButton(text="🎁 Подарить деньги", callback_data="card:gift")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="card:actions")],
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
    async with db_pool.acquire() as conn:
        value = await conn.fetchval("SELECT value FROM nobody_settings WHERE key=$1", key)
    if value is None:
        raise ValueError("Администратор ещё не установил цену для этой услуги.")
    return Decimal(value)

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
    sent = await bot.send_message(chat_id, text, reply_markup=add_menu_back_button(reply_markup))
    await state.update_data(flow_bot_message_id=sent.message_id, ui_bot_message_id=sent.message_id)
    return sent


async def send_ui_message(bot: Bot, chat_id: int, state: FSMContext, text: str,
                          reply_markup=None) -> Message:
    data = await state.get_data()
    previous_ids = {data.get("flow_bot_message_id"), data.get("ui_bot_message_id")}
    for message_id in previous_ids:
        if message_id:
            try:
                await bot.delete_message(chat_id, message_id)
            except Exception:
                pass
    sent = await bot.send_message(chat_id, text, reply_markup=add_menu_back_button(reply_markup))
    await state.update_data(flow_bot_message_id=sent.message_id, ui_bot_message_id=sent.message_id)
    return sent

def add_menu_back_button(reply_markup):
    if reply_markup is None:
        return None
    rows = [list(row) for row in reply_markup.inline_keyboard]
    if not any(
        button.callback_data == "nav:home"
        for row in rows for button in row
    ):
        rows.append([InlineKeyboardButton(text="⬅️ В главное меню", callback_data="nav:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def replace_callback_message(callback: CallbackQuery, text: str, reply_markup=None, **kwargs):
    """Keep callback navigation in one bot message instead of stacking new messages."""
    old_message = callback.message
    if old_message is None:
        return None
    markup = add_menu_back_button(reply_markup)
    try:
        return await old_message.edit_text(text, reply_markup=markup, **kwargs)
    except Exception:
        try:
            await old_message.delete()
        except Exception:
            pass
        return await callback.bot.send_message(
            chat_id=callback.from_user.id,
            text=text,
            reply_markup=markup,
            **kwargs
        )


class FlowInputCleanupMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        state = data.get("state")
        if isinstance(event, Message) and event.chat.type == "private" and (event.text or "").strip() == "⬅️ Главное меню":
            if state is not None:
                saved = await state.get_data()
                for message_id in {saved.get("flow_bot_message_id"), saved.get("ui_bot_message_id")}:
                    if message_id:
                        try:
                            await event.bot.delete_message(event.chat.id, message_id)
                        except Exception:
                            pass
                await state.clear()
            await event.answer(
                "👋 Главное меню VIP NOBODY. Выбери нужный раздел:",
                reply_markup=main_keyboard(await is_admin(event.from_user.id))
            )
            try:
                await event.delete()
            except Exception:
                pass
            return None

        result = await handler(event, data)
        if isinstance(event, Message) and event.chat.type == "private":
            try:
                await event.delete()
            except Exception:
                log.debug("Could not delete incoming message %s", event.message_id)
        return result


dp.message.middleware(FlowInputCleanupMiddleware())

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
        await conn.execute("ALTER TABLE nobody_users ADD COLUMN IF NOT EXISTS referred_by BIGINT REFERENCES nobody_users(telegram_id)")
        await conn.execute("ALTER TABLE nobody_users ADD COLUMN IF NOT EXISTS referral_locked BOOLEAN NOT NULL DEFAULT TRUE")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS nobody_partner_ledger (
                id BIGSERIAL PRIMARY KEY, partner_id BIGINT NOT NULL REFERENCES nobody_users(telegram_id),
                source_user_id BIGINT REFERENCES nobody_users(telegram_id), amount NUMERIC(12,2) NOT NULL,
                kind TEXT NOT NULL, description TEXT NOT NULL, ref TEXT UNIQUE NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS nobody_partner_withdrawals (
                id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES nobody_users(telegram_id),
                amount NUMERIC(12,2) NOT NULL CHECK (amount >= 100), phone TEXT NOT NULL, bank TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), reviewed_at TIMESTAMPTZ
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
            INSERT INTO nobody_settings (key, value)
            VALUES ('min_topup_amount', '1'), ('min_withdraw_amount', '100'),
                   ('referral_bonus_enabled', 'true'), ('referral_bonus_amount', '1')
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
        await conn.execute("ALTER TABLE nobody_withdrawals ADD COLUMN IF NOT EXISTS referrer_id BIGINT REFERENCES nobody_users(telegram_id)")
        await conn.execute("ALTER TABLE nobody_withdrawals ADD COLUMN IF NOT EXISTS referrer_percent NUMERIC(5,2) NOT NULL DEFAULT 0")
        await conn.execute("ALTER TABLE nobody_withdrawals ADD COLUMN IF NOT EXISTS referrer_commission NUMERIC(12,2) NOT NULL DEFAULT 0")
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
            INSERT INTO nobody_users (telegram_id, username, first_name, last_name, last_active_at, referral_locked)
            VALUES ($1, $2, $3, $4, NOW(), FALSE)
            ON CONFLICT (telegram_id) DO UPDATE SET username=EXCLUDED.username,
                first_name=EXCLUDED.first_name, last_name=EXCLUDED.last_name,
                last_active_at=NOW(), activity_count=nobody_users.activity_count+1
        """, user.id, user.username, user.first_name or "", user.last_name)

async def get_partner_balance(user_id: int, conn=None) -> Decimal:
    if conn is not None:
        value = await conn.fetchval("SELECT COALESCE(SUM(amount),0) FROM nobody_partner_ledger WHERE partner_id=$1", user_id)
    else:
        async with db_pool.acquire() as db:
            value = await db.fetchval("SELECT COALESCE(SUM(amount),0) FROM nobody_partner_ledger WHERE partner_id=$1", user_id)
    return Decimal(value or 0)


async def add_partner_commission(conn, buyer_id: int, purchase_amount: Decimal, ref: str, description: str, percent: Decimal = Decimal("10")):
    referrer_id = await conn.fetchval("SELECT referred_by FROM nobody_users WHERE telegram_id=$1", buyer_id)
    if not referrer_id or referrer_id == buyer_id:
        return None
    commission = (purchase_amount * percent / Decimal("100")).quantize(Decimal("0.01"))
    if commission <= 0:
        return None
    inserted = await conn.fetchval(
        """INSERT INTO nobody_partner_ledger
           (partner_id,source_user_id,amount,kind,description,ref)
           VALUES ($1,$2,$3,'commission',$4,$5)
           ON CONFLICT (ref) DO NOTHING RETURNING id""",
        referrer_id, buyer_id, commission, description, ref
    )
    return (referrer_id, commission) if inserted else None


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
    if payload.startswith("ref_"):
        try:
            referrer_id = int(payload[4:])
        except ValueError:
            referrer_id = 0
        referral_bonus = None
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                if referrer_id and referrer_id != message.from_user.id:
                    linked_referrer = await conn.fetchval(
                        """UPDATE nobody_users SET referred_by=$2, referral_locked=TRUE
                           WHERE telegram_id=$1 AND referral_locked=FALSE AND referred_by IS NULL
                           AND EXISTS (SELECT 1 FROM nobody_users WHERE telegram_id=$2)
                           RETURNING referred_by""",
                        message.from_user.id, referrer_id
                    )
                    if linked_referrer:
                        bonus_enabled = await conn.fetchval(
                            "SELECT value FROM nobody_settings WHERE key='referral_bonus_enabled'"
                        )
                        if str(bonus_enabled or "true").lower() == "true":
                            bonus_amount = Decimal(await conn.fetchval(
                                "SELECT value FROM nobody_settings WHERE key='referral_bonus_amount'"
                            ) or "1")
                            inserted = await conn.fetchval(
                                """INSERT INTO nobody_partner_ledger
                                   (partner_id,source_user_id,amount,kind,description,ref)
                                   VALUES ($1,$2,$3,'referral_bonus',$4,$5)
                                   ON CONFLICT (ref) DO NOTHING RETURNING id""",
                                linked_referrer, message.from_user.id, bonus_amount,
                                "Бонус за нового реферала", f"referral-bonus:{message.from_user.id}"
                            )
                            if inserted:
                                referral_bonus = (linked_referrer, bonus_amount)
                else:
                    await conn.execute("UPDATE nobody_users SET referral_locked=TRUE WHERE telegram_id=$1", message.from_user.id)
        if referral_bonus:
            referrer_id, credited_amount = referral_bonus
            try:
                await bot.send_message(
                    referrer_id,
                    "🎉 У вас новый реферал!\n\n"
                    f"На ваш партнёрский баланс зачислено {credited_amount:g} ₽.\n\n"
                    "Также вы получаете:\n"
                    "💌 10% с каждой его покупки\n"
                    "💸 5% с его вывода после одобрения заявки."
                )
            except Exception:
                log.exception("Could not notify referrer %s about new referral", referrer_id)
    else:
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE nobody_users SET referral_locked=TRUE WHERE telegram_id=$1 AND referral_locked=FALSE", message.from_user.id)
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
            [InlineKeyboardButton(text="❌ Отменить", callback_data="gift:cancel")],
            [InlineKeyboardButton(text="⬅️ В главное меню", callback_data="nav:home")]
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
                sent = await callback.message.answer_photo(gift_photo, caption=caption, reply_markup=add_menu_back_button(kb))
            else:
                sent = await replace_callback_message(callback, caption, reply_markup=kb)
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
        await send_flow_message(message.bot, message.chat.id, state, "Отправьте текст до 2000 символов."); return
    data = await state.get_data()
    if not message.from_user or not data.get("recipient_id") or data.get("category") not in CATEGORIES:
        await state.clear(); await send_flow_message(message.bot, message.chat.id, state, "Начните заново по персональной ссылке."); return
    await state.update_data(body=body)
    await state.set_state(AnonymousFlow.confirm)
    await send_flow_message(message.bot, message.chat.id, state,
                                "💌 Сообщение готово! Что делаем дальше?",
                                reply_markup=message_send_options_keyboard())

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
        await replace_callback_message(callback, "🎁 Возвращаемся к подарку!\nУкажи сумму от 10 до 5 000 ₽:")

@dp.message(GiftFlow.amount)
async def gift_amount(message: Message, state: FSMContext):
    try:
        amount = Decimal((message.text or "").replace(",", ".").strip()).quantize(Decimal("0.01"))
        if amount < Decimal("10") or amount > Decimal("5000"): raise InvalidOperation
    except (InvalidOperation, ValueError):
        await send_flow_message(message.bot, message.chat.id, state, "Сумма подарка должна быть от 10 до 5 000 ₽. Введи корректную сумму."); return
    if await get_balance(message.from_user.id) < amount:
        data = await state.get_data()
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO nobody_pending_gifts (sender_id,recipient_id,body,anonymous) VALUES ($1,$2,$3,$4) "
                "ON CONFLICT (sender_id) DO UPDATE SET recipient_id=EXCLUDED.recipient_id, body=EXCLUDED.body, anonymous=EXCLUDED.anonymous, created_at=NOW()",
                message.from_user.id, data["recipient_id"], data["gift_body"], data.get("anonymous", True)
            )
        await state.update_data(resume_gift=True)
        await send_flow_message(message.bot, message.chat.id, state, 
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
        await send_flow_message(message.bot, message.chat.id, state, "Нельзя отправить подарок самому себе.")
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
        await send_flow_message(message.bot, message.chat.id, state, "Недостаточно средств для подарка.", reply_markup=insufficient_balance_keyboard())
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
    await send_flow_message(message.bot, message.chat.id, state, f"✅ Денежный подарок на {amount:.2f} ₽ отправлен!\nТекущий баланс: {await get_balance(sender_id):.2f} ₽")

@dp.message(GiftFlow.text)
async def gift_text(message: Message, state: FSMContext):
    body = (message.text or "").strip()
    if not body or len(body) > 1000:
        await send_flow_message(message.bot, message.chat.id, state, "Напиши текст от 1 до 1000 символов."); return
    await state.update_data(gift_body=body)
    await state.set_state(GiftFlow.amount)
    await send_flow_message(message.bot, message.chat.id, state, "Укажи сумму подарка от 10 до 5 000 ₽:")

@dp.callback_query(F.data == "gift:cancel")
async def gift_cancel(callback: CallbackQuery, state: FSMContext):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM nobody_pending_gifts WHERE sender_id=$1", callback.from_user.id)
    await state.clear()
    await callback.answer("Подарок отменён.")
    if callback.message: await replace_callback_message(callback, "Подарок отменён. Можешь отправить обычное сообщение.")

@dp.callback_query(F.data.startswith("gift:visibility:"))
async def gift_visibility(callback: CallbackQuery, state: FSMContext):
    if not callback.data.endswith((":anonymous", ":visible")):
        await callback.answer("Неизвестный вариант.", show_alert=True)
        return
    await state.update_data(anonymous=callback.data.endswith(":anonymous"))
    await state.set_state(GiftFlow.text)
    await callback.answer()
    if callback.message:
        await replace_callback_message(callback, "Напиши текст, который будет приложен к денежному подарку:")


@dp.callback_query(F.data == "card:choose")
async def choose_postcard_option(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if not data.get("body"):
        await callback.answer("Сначала напиши текст сообщения.", show_alert=True)
        return
    card_price = await get_price("card_price")
    await callback.answer()
    if callback.message:
        await send_flow_message(
            bot, callback.from_user.id, state,
            "💌 Выбери открытку для своего сообщения:",
            reply_markup=postcard_keyboard(card_price)
        )

@dp.callback_query(F.data == "card:actions")
async def back_to_message_actions(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if not data.get("body"):
        await callback.answer("Начни отправку заново по ссылке.", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await send_flow_message(
            bot, callback.from_user.id, state,
            "💌 Сообщение готово! Что делаем дальше?",
            reply_markup=message_send_options_keyboard()
        )

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
                                "💌 Выбери открытку для своего сообщения:",
                                reply_markup=postcard_keyboard(card_price))

async def finish_anonymous(callback: CallbackQuery, state: FSMContext, bot: Bot, paid_card: bool):
    data = await state.get_data()
    sender_id = callback.from_user.id
    await clear_flow_message(bot, sender_id, state)
    recipient_id = data.get("recipient_id")
    category = data.get("category")
    body = data.get("body")
    if not recipient_id or category not in CATEGORIES or not body:
        await callback.answer("Данные сообщения потерялись. Начните заново.", show_alert=True); return
    referral_commission = None
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
                    referral_commission = await add_partner_commission(
                        conn, sender_id, card_price, f"ref-card:{sender_id}:{callback.id}",
                        "Комиссия за покупку открытки приглашённым пользователем"
                    )
                row = await conn.fetchrow("""
                    INSERT INTO nobody_messages (recipient_id,sender_id,category,body,postcard)
                    VALUES ($1,$2,$3,$4,$5) RETURNING id
                """, recipient_id, sender_id, category, body, paid_card)
        await state.clear()
        if paid_card and referral_commission:
            try:
                await bot.send_message(referral_commission[0], f"💰 Вам начислено {referral_commission[1]:.2f} ₽ за покупку открытки приглашённым пользователем.")
            except Exception:
                log.exception("Could not notify referral partner %s", referral_commission[0])
        await callback.answer("Сообщение отправлено!")
        if callback.message:
            await replace_callback_message(callback, 
                "✅ Анонимное сообщение отправлено!" + (f" Открытка куплена за {card_price:.2f} ₽." if paid_card else ""),
                reply_markup=main_keyboard(await is_admin(sender_id)))
        await send_message_to_recipient(bot, recipient_id, category, body, row["id"], paid_card, data.get("selected_postcard", "heart"))
    except ValueError as e:
        if "Недостаточно средств" in str(e):
            await state.clear()
            await callback.answer()
            if callback.message:
                await replace_callback_message(callback, 
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

@dp.message(F.text == "⬅️ Главное меню")
async def return_to_main_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Главное меню VIP NOBODY. Выбери нужный раздел:",
        reply_markup=main_keyboard(await is_admin(message.from_user.id))
    )


@dp.callback_query(F.data == "nav:home")
async def callback_return_to_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass
    await callback.bot.send_message(
        callback.from_user.id,
        "👋 Главное меню VIP NOBODY. Выбери нужный раздел:",
        reply_markup=main_keyboard(await is_admin(callback.from_user.id))
    )


@dp.message(F.text == "🔗 Моя ссылка")
async def my_link(message: Message, bot: Bot, state: FSMContext):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=to_{message.from_user.id}"
    await send_ui_message(
        bot, message.chat.id, state,
        "🔥 VIP NOBODY — анонимность, подарки и заработок!\n\n"
        "💌 Отправляй анонимные сообщения.\n"
        "🎁 Создавай красивые открытки.\n"
        "💸 Дарите друг другу настоящие деньги.\n"
        "🕵️ Узнавай, кто скрывается за анонимным сообщением.\n"
        "💰 Приглашай друзей и получай до 10% с покупок и 5% с выводов!\n\n"
        "👇 Переходи по моей ссылке, чтобы отправить мне анонимное сообщение:\n\n"
        + link
    )

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
        await replace_callback_message(callback, "Напиши ответ. Он будет отправлен автору анонимно:")

@dp.message(ReplyFlow.text)
async def send_anonymous_reply(message: Message, state: FSMContext, bot: Bot):
    body = (message.text or "").strip()
    if not body or len(body) > 2000:
        await send_flow_message(message.bot, message.chat.id, state, "Напиши текст ответа до 2000 символов.")
        return
    data = await state.get_data()
    message_id = data.get("reply_message_id")
    async with db_pool.acquire() as conn:
        original = await conn.fetchrow(
            "SELECT sender_id, recipient_id FROM nobody_messages WHERE id=$1", message_id
        )
    if not original or original["recipient_id"] != message.from_user.id:
        await state.clear()
        await send_flow_message(message.bot, message.chat.id, state, "Не удалось найти исходное сообщение. Попробуй ещё раз.")
        return
    try:
        await bot.send_message(
            original["sender_id"],
            f"↩️ Тебе ответили на анонимное сообщение №{message_id}:\n\n{body}\n\n"
            "Ответ отправлен анонимно через VIP NOBODY."
        )
    except Exception:
        log.exception("Could not deliver anonymous reply to sender of message %s", message_id)
        await send_flow_message(message.bot, message.chat.id, state, "Не удалось доставить ответ. Возможно, автор ещё не открыл бота или заблокировал его.")
        await state.clear()
        return
    await state.clear()
    await send_flow_message(message.bot, message.chat.id, state, "✅ Ответ отправлен автору анонимно.", reply_markup=main_keyboard(await is_admin(message.from_user.id)))

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
                    reveal_price = None
                    balance_after = None
                else:
                    reveal_price = await get_price("reveal_price")
                    updated = await conn.fetchrow(
                        "UPDATE nobody_users SET balance=balance-$2 WHERE telegram_id=$1 AND balance >= $2 RETURNING balance",
                        user_id, reveal_price
                    )
                    if updated is None:
                        raise ValueError("Недостаточно средств. Пополни баланс и попробуй снова.")
                    balance_after = updated["balance"]
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
                    referral_commission = await add_partner_commission(
                        conn, user_id, reveal_price, f"ref-reveal:{message_id}:{user_id}",
                        "Комиссия за раскрытие личности приглашённым пользователем"
                    )
                    already_revealed = False
        if not already_revealed and referral_commission:
            try:
                await bot.send_message(referral_commission[0], f"💰 Вам начислено {referral_commission[1]:.2f} ₽ за раскрытие личности приглашённым пользователем.")
            except Exception:
                log.exception("Could not notify referral partner %s", referral_commission[0])
        await callback.answer("Личность раскрыта!" if not already_revealed else "Уже раскрыто.")
        username = f"@{author['username']}" if author and author["username"] else "не указан"
        full_name = " ".join(part for part in [author["first_name"], author["last_name"]] if part) if author else "неизвестно"
        result_text = (
            f"🔎 Автор сообщения №{message_id}\n\n"
            f"Имя: {full_name}\nUsername: {username}\nTelegram ID: {author['telegram_id'] if author else row['sender_id']}"
        )
        if callback.message:
            if not already_revealed:
                await replace_callback_message(callback, 
                    f"💸 Списано с баланса: {reveal_price:.2f} ₽\n"
                    f"Остаток на балансе: {balance_after:.2f} ₽"
                )
            try:
                photos = await bot.get_user_profile_photos(row["sender_id"], limit=1)
                if photos.total_count and photos.photos:
                    photo_file_id = photos.photos[0][-1].file_id
                    await callback.message.answer_photo(photo_file_id, caption=result_text)
                else:
                    await replace_callback_message(callback, result_text)
            except Exception:
                log.exception("Could not fetch profile photo for revealed author %s", row["sender_id"])
                await replace_callback_message(callback, result_text)
    except ValueError as exc:
        if "Недостаточно средств" in str(exc):
            await callback.answer()
            if callback.message:
                await replace_callback_message(callback, 
                    "Недостаточно средств. Пополни баланс, чтобы раскрыть личность.",
                    reply_markup=insufficient_balance_keyboard()
                )
        else:
            await callback.answer(str(exc), show_alert=True)
    except Exception:
        log.exception("Failed to reveal author for message %s", message_id)
        await callback.answer("Не удалось выполнить операцию. Попробуй ещё раз.", show_alert=True)

@dp.message(F.text == "💼 Мой заработок")
async def partner_cabinet(message: Message, bot: Bot):
    async with db_pool.acquire() as conn:
        bonus_enabled = await conn.fetchval(
            "SELECT value FROM nobody_settings WHERE key='referral_bonus_enabled'"
        )
        bonus_amount = await conn.fetchval(
            "SELECT value FROM nobody_settings WHERE key='referral_bonus_amount'"
        )
    bonus_enabled = str(bonus_enabled or "true").lower() == "true"
    bonus_amount = Decimal(bonus_amount or "1")
    signup_bonus_text = (
        f"Приглашай друзей и получай {bonus_amount:g} ₽ за каждого нового реферала, "
        "10% с его покупок и 5% с одобренных выводов."
        if bonus_enabled else
        "Приглашай друзей и получай 10% с его покупок и 5% с одобренных выводов."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Аналитика", callback_data="partner:analytics")],
        [InlineKeyboardButton(text="📖 Как это работает?", callback_data="partner:howto")],
        [InlineKeyboardButton(text="📤 Вывести заработок", callback_data="partner:withdraw")],
        [InlineKeyboardButton(text="📨 Поделиться ссылкой", callback_data="partner:share")],
    ])
    await message.answer(
        "💼 МОЙ ЗАРАБОТОК\n\n" + signup_bonus_text,
        reply_markup=kb
    )


@dp.callback_query(F.data == "partner:analytics")
async def partner_analytics(callback: CallbackQuery):
    uid = callback.from_user.id
    async with db_pool.acquire() as conn:
        invited = await conn.fetchval(
            "SELECT COUNT(*) FROM nobody_users WHERE referred_by=$1", uid
        )
        active = await conn.fetchval(
            "SELECT COUNT(*) FROM nobody_users WHERE referred_by=$1 "
            "AND last_active_at >= NOW()-INTERVAL '30 days'", uid
        )
        earned = await conn.fetchval(
            "SELECT COALESCE(SUM(amount),0) FROM nobody_partner_ledger "
            "WHERE partner_id=$1 AND kind IN ('referral_bonus','commission','withdrawal_commission')", uid
        )
    await callback.answer()
    if callback.message:
        await replace_callback_message(callback, 
            "📊 ТВОЯ АНАЛИТИКА\n\n"
            f"👥 Всего рефералов: {invited}\n"
            f"🟢 Активных за 30 дней: {active}\n"
            f"💰 Всего заработано: {Decimal(earned or 0):.2f} ₽"
        )


@dp.callback_query(F.data == "partner:howto")
async def partner_howto(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    if callback.message:
        async with db_pool.acquire() as conn:
            bonus_enabled = await conn.fetchval(
                "SELECT value FROM nobody_settings WHERE key='referral_bonus_enabled'"
            )
            bonus_amount = await conn.fetchval(
                "SELECT value FROM nobody_settings WHERE key='referral_bonus_amount'"
            )
        bonus_enabled = str(bonus_enabled or "true").lower() == "true"
        bonus_amount = Decimal(bonus_amount or "1")
        me = await bot.get_me()
        link = f"https://t.me/{me.username}?start=ref_{callback.from_user.id}"
        share_url = f"https://t.me/share/url?url={quote(link, safe='')}"
        signup_bonus_text = (
            f"👥 Новый реферал — {bonus_amount:g} ₽ сразу после первого перехода по твоей ссылке\n"
            if bonus_enabled else ""
        )
        signup_bonus_footer = (
            "Бонус за реферала и начисления за покупки поступают сразу, за вывод — после одобрения заявки."
            if bonus_enabled else
            "Начисления за покупки поступают сразу, за одобренный вывод — после подтверждения заявки."
        )
        await replace_callback_message(callback, 
            "📖 КАК ЭТО РАБОТАЕТ?\n\n"
            "1. Нажми «Поделиться ссылкой» под этим сообщением.\n"
            "2. Отправь ссылку друзьям. После запуска бота по ней они станут твоими рефералами.\n"
            "3. Получай процент с его операций:\n\n"
            + signup_bonus_text +
            "💌 Покупка открытки — 10%\n"
            "🔎 Раскрытие личности — 10%\n"
            "💸 Одобренный вывод баланса — 5%\n\n"
            + signup_bonus_footer,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 Поделиться ссылкой", url=share_url)]
            ])
        )


@dp.callback_query(F.data == "partner:share")
async def partner_share(callback: CallbackQuery, bot: Bot):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{callback.from_user.id}"
    await callback.answer()
    if callback.message:
        await replace_callback_message(callback, 
            "🔗 Твоя реферальная ссылка:\n" + link,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📨 Поделиться", url=f"https://t.me/share/url?url={link}")],
            ])
        )


@dp.callback_query(F.data == "partner:withdraw")
async def partner_withdraw_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    balance = await get_partner_balance(callback.from_user.id)
    if balance < Decimal("100"):
        await callback.answer("Для вывода нужно накопить минимум 100 ₽.", show_alert=True)
        return
    await state.set_state(PartnerWithdrawFlow.amount)
    await callback.answer()
    if callback.message:
        await replace_callback_message(callback, 
            "📤 ВЫВОД ПАРТНЁРСКОГО ЗАРАБОТКА\n\n"
            "Вывод доступен только через СБП.\n"
            "Понадобятся номер телефона, привязанный к СБП, и название банка.\n\n"
            f"Доступно: {balance:.2f} ₽\n"
            "Введи сумму для вывода (минимум 100 ₽):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отказаться", callback_data="partner:cancel")]
            ])
        )


@dp.callback_query(F.data == "partner:cancel")
async def partner_withdraw_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass
    await callback.answer("Вывод отменён.")


@dp.message(PartnerWithdrawFlow.amount)
async def partner_withdraw_amount(message: Message, state: FSMContext):
    try:
        amount = Decimal((message.text or "").replace(",", ".").strip()).quantize(Decimal("0.01"))
        available = await get_partner_balance(message.from_user.id)
        if amount < Decimal("100") or amount > available:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        available = await get_partner_balance(message.from_user.id)
        await send_flow_message(message.bot, message.chat.id, state, f"Введи сумму от 100 ₽, не больше доступных {available:.2f} ₽.")
        return
    await state.update_data(partner_amount=str(amount))
    await state.set_state(PartnerWithdrawFlow.phone)
    await send_flow_message(message.bot, message.chat.id, state, 
        "📱 Введи номер телефона, который привязан к СБП.\n"
        "На этот номер будет отправлен перевод:"
    )


@dp.message(PartnerWithdrawFlow.phone)
async def partner_withdraw_phone(message: Message, state: FSMContext):
    phone = (message.text or "").strip()
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 10 or len(digits) > 15:
        await send_flow_message(message.bot, message.chat.id, state, "Проверь номер телефона и введи ещё раз.")
        return
    await state.update_data(partner_phone=phone)
    await state.set_state(PartnerWithdrawFlow.bank)
    await send_flow_message(message.bot, message.chat.id, state, 
        "🏦 Укажи название банка, в котором подключён СБП:"
    )


@dp.message(PartnerWithdrawFlow.bank)
async def partner_withdraw_bank(message: Message, state: FSMContext, bot: Bot):
    bank = (message.text or "").strip()
    if len(bank) < 2 or len(bank) > 80:
        await send_flow_message(message.bot, message.chat.id, state, "Введи название банка.")
        return
    data = await state.get_data()
    uid = message.from_user.id
    amount = Decimal(data["partner_amount"])
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", uid)
            available = await conn.fetchval(
                "SELECT COALESCE(SUM(amount),0) FROM nobody_partner_ledger WHERE partner_id=$1", uid
            )
            if Decimal(available or 0) < amount:
                await state.clear()
                await send_flow_message(message.bot, message.chat.id, state, "Недостаточно средств для вывода.")
                return
            wid = await conn.fetchval(
                "INSERT INTO nobody_partner_withdrawals (user_id,amount,phone,bank) VALUES ($1,$2,$3,$4) RETURNING id",
                uid, amount, data["partner_phone"], bank
            )
            await conn.execute(
                """INSERT INTO nobody_partner_ledger (partner_id,amount,kind,description,ref)
                   VALUES ($1,$2,'withdrawal_reserve',$3,$4)""",
                uid, -amount, f"Резерв партнёрского вывода №{wid}", f"partner-withdraw:{wid}:reserve"
            )
    await state.clear()
    await send_flow_message(message.bot, message.chat.id, state, f"✅ Заявка №{wid} на вывод партнёрского заработка отправлена. Сумма: {amount:.2f} ₽.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Выплачено", callback_data=f"partner:approve:{wid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"partner:reject:{wid}")
    ]])
    await bot.send_message(
        ADMIN_ID,
        f"📤 ЗАЯВКА НА ВЫВОД ПАРТНЁРСКОГО ЗАРАБОТКА №{wid}\n\n"
        f"Сумма: {amount:.2f} ₽\n"
        f"Способ: СБП\n"
        f"Телефон: {data['partner_phone']}\n"
        f"Банк: {bank}\n"
        f"Telegram ID: {uid}",
        reply_markup=kb
    )


@dp.callback_query(F.data.startswith("partner:approve:"))
async def partner_withdraw_approve(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    wid = int(callback.data.rsplit(":", 1)[1])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE nobody_partner_withdrawals SET status='approved',reviewed_at=NOW()
               WHERE id=$1 AND status='pending' RETURNING user_id,amount""", wid
        )
    if not row:
        await callback.answer("Заявка уже обработана.", show_alert=True)
        return
    await callback.answer("Отмечено как выплачено.")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.bot.send_message(row["user_id"], f"✅ Партнёрский вывод №{wid} одобрен. Выплачено {Decimal(row['amount']):.2f} ₽.")


@dp.callback_query(F.data.startswith("partner:reject:"))
async def partner_withdraw_reject(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    wid = int(callback.data.rsplit(":", 1)[1])
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """UPDATE nobody_partner_withdrawals SET status='rejected',reviewed_at=NOW()
                   WHERE id=$1 AND status='pending' RETURNING user_id,amount""", wid
            )
            if row:
                await conn.execute(
                    """INSERT INTO nobody_partner_ledger (partner_id,amount,kind,description,ref)
                       VALUES ($1,$2,'withdrawal_refund',$3,$4) ON CONFLICT (ref) DO NOTHING""",
                    row["user_id"], row["amount"], f"Возврат отклонённого партнёрского вывода №{wid}",
                    f"partner-withdraw:{wid}:refund"
                )
    if not row:
        await callback.answer("Заявка уже обработана.", show_alert=True)
        return
    await callback.answer("Средства возвращены.")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.bot.send_message(row["user_id"], f"❌ Партнёрский вывод №{wid} отклонён. {Decimal(row['amount']):.2f} ₽ возвращены.")


@dp.message(F.text == "💰 Баланс")
async def balance(message: Message, state: FSMContext):
    amount = await get_balance(message.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Пополнить баланс", callback_data="topup:start")],
        [InlineKeyboardButton(text="💸 Вывести средства", callback_data="withdraw:start")]
    ])
    await send_ui_message(message.bot, message.chat.id, state, f"💰 Твой баланс: {amount:.2f} ₽", reply_markup=kb)

@dp.callback_query(F.data == "withdraw:start")
async def withdraw_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(WithdrawFlow.amount)
    await callback.answer()
    if callback.message:
        async with db_pool.acquire() as conn:
            referrer_id = await conn.fetchval("SELECT referred_by FROM nobody_users WHERE telegram_id=$1", callback.from_user.id)
        project_percent = Decimal("5") if referrer_id else Decimal("10")
        partner_percent = Decimal("5") if referrer_id else Decimal("0")
        await replace_callback_message(callback, 
            f"💸 Вывод средств\n\nКомиссия проекта: {project_percent:.0f}%"
            + (f"\nКомиссия рефереру: {partner_percent:.0f}%" if partner_percent else "")
            + "\nНа карту поступит 90% от суммы вывода. Комиссии начисляются после одобрения заявки.\n\n"
            f"Введи сумму от {await get_price('min_withdraw_amount'):.2f} ₽:",
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
        minimum = await get_price("min_withdraw_amount")
        if amount < minimum or amount > Decimal("1000000"): raise InvalidOperation
    except (InvalidOperation, ValueError):
        minimum = await get_price("min_withdraw_amount")
        await send_flow_message(message.bot, message.chat.id, state, 
            f"Введи сумму от {minimum:.2f} ₽.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отказаться", callback_data="withdraw:cancel")]
            ])
        ); return
    if await get_balance(message.from_user.id) < amount:
        await state.clear()
        await send_flow_message(message.bot, message.chat.id, state, "Недостаточно средств.", reply_markup=insufficient_balance_keyboard()); return
    async with db_pool.acquire() as conn:
        referrer_id = await conn.fetchval("SELECT referred_by FROM nobody_users WHERE telegram_id=$1", message.from_user.id)
    project_percent = Decimal("5") if referrer_id else Decimal("10")
    partner_percent = Decimal("5") if referrer_id else Decimal("0")
    project_fee = (amount * project_percent / Decimal("100")).quantize(Decimal("0.01"))
    partner_fee = (amount * partner_percent / Decimal("100")).quantize(Decimal("0.01"))
    payout = amount - project_fee - partner_fee
    await state.update_data(
        withdraw_amount=str(amount), withdraw_commission=str(project_percent),
        withdraw_partner_percent=str(partner_percent), withdraw_partner_id=referrer_id,
        withdraw_partner_fee=str(partner_fee), withdraw_payout=str(payout)
    )
    await state.set_state(WithdrawFlow.phone)
    await send_flow_message(message.bot, message.chat.id, state, 
        f"Сумма вывода: {amount:.2f} ₽\n"
        f"Комиссия проекта ({project_percent:.0f}%): {project_fee:.2f} ₽\n"
        + (f"Комиссия рефереру ({partner_percent:.0f}%): {partner_fee:.2f} ₽\n" if partner_percent else "")
        + f"На карту поступит: {payout:.2f} ₽\n\n"
        "Теперь укажи номер телефона для получения перевода:"
    )

@dp.message(WithdrawFlow.phone)
async def withdraw_phone(message: Message, state: FSMContext):
    phone = (message.text or "").strip()
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 10 or len(digits) > 15:
        await send_flow_message(message.bot, message.chat.id, state, "Проверь номер телефона и введи ещё раз."); return
    await state.update_data(withdraw_phone=phone)
    await state.set_state(WithdrawFlow.bank)
    await send_flow_message(message.bot, message.chat.id, state, "Укажи название банка:")

@dp.message(WithdrawFlow.bank)
async def withdraw_bank(message: Message, state: FSMContext, bot: Bot):
    bank = (message.text or "").strip()
    if len(bank) < 2 or len(bank) > 80:
        await send_flow_message(message.bot, message.chat.id, state, "Введи название банка."); return
    data = await state.get_data()
    amount = Decimal(data["withdraw_amount"])
    uid = message.from_user.id
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            updated = await conn.fetchval("UPDATE nobody_users SET balance=balance-$2 WHERE telegram_id=$1 AND balance >= $2 RETURNING balance", uid, amount)
            if updated is None:
                await state.clear()
                await send_flow_message(message.bot, message.chat.id, state, "Недостаточно средств. Заявка не создана.", reply_markup=insufficient_balance_keyboard()); return
            wid = await conn.fetchval(
                """INSERT INTO nobody_withdrawals
                   (user_id,amount,phone,bank,commission_percent,payout_amount,referrer_id,referrer_percent,referrer_commission)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id""",
                uid, amount, data["withdraw_phone"], bank, Decimal(data["withdraw_commission"]),
                Decimal(data["withdraw_payout"]), data.get("withdraw_partner_id"),
                Decimal(data["withdraw_partner_percent"]), Decimal(data["withdraw_partner_fee"])
            )
            await conn.execute("INSERT INTO nobody_transactions (user_id,amount,kind,description,ref) VALUES ($1,$2,'debit','Заявка на вывод средств',$3)", uid, -amount, f"withdraw:{wid}")
    await state.clear()
    project_percent = Decimal(data["withdraw_commission"])
    partner_percent = Decimal(data["withdraw_partner_percent"])
    project_fee = (amount * project_percent / Decimal("100")).quantize(Decimal("0.01"))
    partner_fee = Decimal(data["withdraw_partner_fee"])
    payout = Decimal(data["withdraw_payout"])
    await send_flow_message(message.bot, message.chat.id, state, 
        f"✅ Заявка на вывод №{wid} отправлена.\n\n"
        f"Сумма списания: {amount:.2f} ₽\nКомиссия проекта ({project_percent:.0f}%): {project_fee:.2f} ₽\n"
        + (f"Комиссия рефереру ({partner_percent:.0f}%): {partner_fee:.2f} ₽\n" if partner_percent else "")
        + f"К получению: {payout:.2f} ₽\n\nСредства списаны до проверки."
    )
    user = message.from_user
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Выплачено", callback_data=f"withdraw:approve:{wid}"), InlineKeyboardButton(text="❌ Отклонить и вернуть", callback_data=f"withdraw:reject:{wid}")]])
    await bot.send_message(ADMIN_ID, f"💸 ЗАЯВКА НА ВЫВОД №{wid}\n\nСписать с баланса: {amount:.2f} ₽\n"
        f"Комиссия проекта ({project_percent:.0f}%): {project_fee:.2f} ₽\n"
        + (f"Комиссия рефереру ({partner_percent:.0f}%): {partner_fee:.2f} ₽\n" if partner_percent else "")
        + f"К выплате: {payout:.2f} ₽\n"
        f"Телефон: {data['withdraw_phone']}\nБанк: {bank}\nПользователь: {user.full_name}\n"
        f"Username: @{user.username if user.username else 'не указан'}\nTelegram ID: {uid}", reply_markup=kb)

@dp.callback_query(F.data.startswith("withdraw:approve:"))
async def withdraw_approve(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True); return
    wid = int(callback.data.rsplit(":",1)[1])
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """UPDATE nobody_withdrawals SET status='approved',reviewed_at=NOW()
                   WHERE id=$1 AND status='pending'
                   RETURNING user_id,amount,payout_amount,referrer_id,referrer_commission""",
                wid
            )
            if row and row["referrer_id"] and Decimal(row["referrer_commission"]) > 0:
                await conn.execute(
                    """INSERT INTO nobody_partner_ledger
                       (partner_id,source_user_id,amount,kind,description,ref)
                       VALUES ($1,$2,$3,'withdrawal_commission',$4,$5)
                       ON CONFLICT (ref) DO NOTHING""",
                    row["referrer_id"], row["user_id"], row["referrer_commission"],
                    f"Комиссия за одобренный вывод приглашённого пользователя №{wid}",
                    f"withdraw-referral:{wid}"
                )
    if not row:
        await callback.answer("Заявка уже обработана.", show_alert=True); return
    await callback.answer("Отмечено как выплачено.")
    if callback.message: await callback.message.edit_reply_markup(reply_markup=None)
    await callback.bot.send_message(row["user_id"], f"✅ Вывод №{wid} одобрен. На карту отправлено: {Decimal(row['payout_amount']):.2f} ₽.")
    if row["referrer_id"] and Decimal(row["referrer_commission"]) > 0:
        try:
            await callback.bot.send_message(
                row["referrer_id"],
                f"💰 Вам начислено {Decimal(row['referrer_commission']):.2f} ₽ — 5% с одобренного вывода приглашённого пользователя."
            )
        except Exception:
            log.exception("Could not notify referrer for withdrawal %s", wid)

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
        minimum = await get_price("min_topup_amount")
        if amount < minimum or amount > Decimal("1000000"):
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        minimum = await get_price("min_topup_amount")
        await send_flow_message(message.bot, message.chat.id, state, f"Минимальная сумма пополнения — {minimum:.2f} ₽. Введи корректную сумму.")
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
        await replace_callback_message(callback, "❌ Пополнение отменено.")

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
        await replace_callback_message(callback, "✅ Заявка на пополнение отправлена. Баланс будет зачислен после проверки оплаты.")
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
        await replace_callback_message(callback, f"✅ Заявка №{topup_id} подтверждена. Начислено {Decimal(row['amount']):.2f} ₽.")
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
        await replace_callback_message(callback, f"❌ Заявка №{topup_id} отклонена.")
    try:
        await bot.send_message(row["user_id"], f"❌ Заявка на пополнение №{topup_id} отклонена. Если считаешь это ошибкой, обратись в поддержку.")
    except Exception:
        log.exception("Could not notify user about rejected top-up %s", topup_id)

@dp.message(F.text == "👤 Профиль")
async def profile(message: Message, state: FSMContext):
    user = message.from_user
    await send_ui_message(message.bot, message.chat.id, state, f"👤 Профиль VIP NOBODY\n\nИмя: {user.first_name or 'не указано'}\nUsername: @{user.username if user.username else 'не указан'}\nTelegram ID: {user.id}")

@dp.message(F.text == "ℹ️ Помощь")
async def help_message(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆘 Обратиться в поддержку", callback_data="support:start")]
    ])
    card_price = await get_price("card_price")
    reveal_price = await get_price("reveal_price")
    await send_ui_message(
        message.bot, message.chat.id, state,
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
            await replace_callback_message(callback, "Обращения в поддержку для твоего аккаунта временно недоступны.")
        return
    await state.clear()
    await state.update_data(support_ticket_id=ticket_id)
    if ticket:
        await state.set_state(SupportFlow.user_reply)
        if callback.message:
            await replace_callback_message(callback, 
                "У тебя уже есть открытый диалог с поддержкой. Если поддержка ответит, здесь появится кнопка для ответа."
            )
    else:
        await state.set_state(SupportFlow.new_text)
        if callback.message:
            await replace_callback_message(callback, "Опиши проблему или вопрос одним сообщением. Текст будет передан администрации.")

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
        await send_flow_message(message.bot, message.chat.id, state, "Опиши проблему текстом до 3000 символов.")
        return
    ticket_id = (await state.get_data()).get("support_ticket_id")
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO nobody_support_messages (ticket_id,sender_id,sender_role,body) VALUES ($1,$2,'user',$3)",
            ticket_id, message.from_user.id, body
        )
        await conn.execute("UPDATE nobody_support_tickets SET updated_at=NOW() WHERE id=$1", ticket_id)
    await state.set_state(SupportFlow.user_reply)
    await send_flow_message(message.bot, message.chat.id, state, 
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
        await replace_callback_message(callback, "Напиши сообщение для администрации:")

@dp.message(SupportFlow.user_reply)
async def support_user_reply(message: Message, state: FSMContext, bot: Bot):
    body = (message.text or "").strip()
    if not body or len(body) > 3000:
        await send_flow_message(message.bot, message.chat.id, state, "Напиши текст до 3000 символов.")
        return
    ticket_id = (await state.get_data()).get("support_ticket_id")
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            ticket = await conn.fetchrow("SELECT user_id,status FROM nobody_support_tickets WHERE id=$1 FOR UPDATE", ticket_id)
            blocked = await conn.fetchval("SELECT 1 FROM nobody_support_blocks WHERE user_id=$1", message.from_user.id)
            if not ticket or ticket["user_id"] != message.from_user.id or ticket["status"] != "open" or blocked:
                await state.clear()
                await send_flow_message(message.bot, message.chat.id, state, "Диалог закрыт или обращения в поддержку недоступны.")
                return
            await conn.execute(
                "INSERT INTO nobody_support_messages (ticket_id,sender_id,sender_role,body) VALUES ($1,$2,'user',$3)",
                ticket_id, message.from_user.id, body
            )
            await conn.execute("UPDATE nobody_support_tickets SET updated_at=NOW() WHERE id=$1", ticket_id)
    await send_flow_message(message.bot, message.chat.id, state, "✅ Сообщение отправлено администрации. Ожидай ответа поддержки.")
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
        await replace_callback_message(callback, "Введи ответ пользователю:")

@dp.message(SupportFlow.admin_reply)
async def support_admin_reply(message: Message, state: FSMContext, bot: Bot):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    body = (message.text or "").strip()
    if not body or len(body) > 3000:
        await send_flow_message(message.bot, message.chat.id, state, "Введи текст ответа до 3000 символов.")
        return
    ticket_id = (await state.get_data()).get("support_ticket_id")
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            ticket = await conn.fetchrow("SELECT user_id,status,user_can_close FROM nobody_support_tickets WHERE id=$1 FOR UPDATE", ticket_id)
            if not ticket or ticket["status"] != "open":
                await state.clear()
                await send_flow_message(message.bot, message.chat.id, state, "Диалог уже закрыт.")
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
        await send_flow_message(message.bot, message.chat.id, state, "✅ Ответ отправлен.")
    except Exception:
        log.exception("Could not deliver support response for ticket %s", ticket_id)
        await send_flow_message(message.bot, message.chat.id, state, "Не удалось доставить ответ пользователю.")
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
        await replace_callback_message(callback, "🔒 Ты завершил диалог с поддержкой.")
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
        await replace_callback_message(callback, "Введи Telegram ID или @username пользователя. Он должен ранее запустить бота.")

@dp.message(SupportFlow.direct_target)
async def support_direct_target(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    user = await resolve_support_user(message.text or "")
    if not user:
        await send_flow_message(message.bot, message.chat.id, state, "Не нашёл пользователя. Проверь ID или @username — пользователь должен запускать бота.")
        return
    await state.update_data(direct_user_id=user["telegram_id"])
    await state.set_state(SupportFlow.direct_text)
    await send_flow_message(message.bot, message.chat.id, state, f"Получатель найден: {user['first_name']} (ID {user['telegram_id']}). Теперь введи сообщение.")

@dp.message(SupportFlow.direct_text)
async def support_direct_text(message: Message, state: FSMContext, bot: Bot):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    body = (message.text or "").strip()
    if not body or len(body) > 3000:
        await send_flow_message(message.bot, message.chat.id, state, "Введи текст до 3000 символов.")
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
        await send_flow_message(message.bot, message.chat.id, state, "✅ Сообщение отправлено. Пользователь сможет ответить, а завершить диалог сможешь только ты.")
    except Exception:
        log.exception("Could not send direct support message to %s", user_id)
        await send_flow_message(message.bot, message.chat.id, state, "Не удалось отправить сообщение. Возможно, пользователь заблокировал бота.")
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
        await replace_callback_message(callback, 
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
        await send_flow_message(message.bot, message.chat.id, state, "Пользователь не найден. Проверь ID или @username.")
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
        await send_flow_message(message.bot, message.chat.id, state, f"✅ Запрет обращений снят для ID {target['telegram_id']}.")
    else:
        await send_flow_message(message.bot, message.chat.id, state, f"🚫 Обращения в поддержку заблокированы для ID {target['telegram_id']}.")

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
        [InlineKeyboardButton(text="👥 Рефералы", callback_data="admin:referrals")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="🏆 Топ 10 активных", callback_data="admin:top_active")],
        [InlineKeyboardButton(text="👁 Просмотр", callback_data="admin:view_messages")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin:broadcast")],
    ])
    await send_ui_message(message.bot, message.chat.id, state, "🛠 АДМИН-ПАНЕЛЬ VIP NOBODY", reply_markup=kb)

@dp.message(Command("setref"))
async def admin_set_referrer(message: Message):
    if not await is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /setref TELEGRAM_ID")
        return
    invited_id = int(parts[1])
    if invited_id == ADMIN_ID:
        await message.answer("Нельзя назначить пользователя реферером самого себя.")
        return
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            invited_exists = await conn.fetchval(
                "SELECT 1 FROM nobody_users WHERE telegram_id=$1", invited_id
            )
            admin_exists = await conn.fetchval(
                "SELECT 1 FROM nobody_users WHERE telegram_id=$1", ADMIN_ID
            )
            if not invited_exists or not admin_exists:
                await message.answer("Не найден пользователь или аккаунт администратора в базе.")
                return
            await conn.execute(
                """UPDATE nobody_users
                   SET referred_by=$2, referral_locked=TRUE
                   WHERE telegram_id=$1""",
                invited_id, ADMIN_ID
            )
    await message.answer(
        f"✅ Реферер назначен. Пользователь {invited_id} теперь приглашён администратором."
    )


@dp.callback_query(F.data.startswith("admin:"))
async def admin_actions(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True); return
    action = callback.data.split(":")[1]
    await callback.answer()
    if action in ("credit", "debit"):
        await state.update_data(balance_action=action)
        await state.set_state(AdminFlow.balance_user)
        if callback.message: await replace_callback_message(callback, "Отправь Telegram ID пользователя.")
    elif action == "prices":
        async with db_pool.acquire() as conn:
            card_value = await conn.fetchval("SELECT value FROM nobody_settings WHERE key='card_price'")
            reveal_value = await conn.fetchval("SELECT value FROM nobody_settings WHERE key='reveal_price'")
            min_topup_value = await conn.fetchval("SELECT value FROM nobody_settings WHERE key='min_topup_amount'")
            min_withdraw_value = await conn.fetchval("SELECT value FROM nobody_settings WHERE key='min_withdraw_amount'")
        card_label = f"{Decimal(card_value):.2f} ₽" if card_value is not None else "не задана"
        reveal_label = f"{Decimal(reveal_value):.2f} ₽" if reveal_value is not None else "не задана"
        min_topup_label = f"{Decimal(min_topup_value or '1'):.2f} ₽"
        min_withdraw_label = f"{Decimal(min_withdraw_value or '100'):.2f} ₽"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"❤️ Цена открытки: {card_label}", callback_data="price:card")],
            [InlineKeyboardButton(text=f"🔎 Цена раскрытия: {reveal_label}", callback_data="price:reveal")],
            [InlineKeyboardButton(text=f"➕ Минимальное пополнение: {min_topup_label}", callback_data="price:min_topup")],
            [InlineKeyboardButton(text=f"💸 Минимальный вывод: {min_withdraw_label}", callback_data="price:min_withdraw")],
        ])
        if callback.message:
            await replace_callback_message(callback, "💵 НАСТРОЙКА ЦЕН\n\nВыбери, какую цену изменить:", reply_markup=kb)
    elif action == "stats":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Люди", callback_data="stats:people")],
            [InlineKeyboardButton(text="💰 Заработок", callback_data="stats:earnings")],
        ])
        if callback.message:
            await replace_callback_message(callback, "📊 СТАТИСТИКА VIP NOBODY\n\nВыбери раздел:", reply_markup=kb)

    elif action == "referrals":
        async with db_pool.acquire() as conn:
            enabled_value = await conn.fetchval(
                "SELECT value FROM nobody_settings WHERE key='referral_bonus_enabled'"
            )
            amount_value = await conn.fetchval(
                "SELECT value FROM nobody_settings WHERE key='referral_bonus_amount'"
            )
            referrals_total = await conn.fetchval(
                "SELECT COUNT(*) FROM nobody_users WHERE referred_by IS NOT NULL"
            )
            bonus_total = await conn.fetchval(
                "SELECT COALESCE(SUM(amount),0) FROM nobody_partner_ledger WHERE kind='referral_bonus'"
            )
        enabled = str(enabled_value or "true").lower() == "true"
        amount = Decimal(amount_value or "1")
        status = "🟢 ВКЛЮЧЕН" if enabled else "🔴 ВЫКЛЮЧЕН"
        toggle_label = "🔴 Выключить бонус" if enabled else "🟢 Включить бонус"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=toggle_label, callback_data="referrals:toggle")],
            [InlineKeyboardButton(text="💰 Изменить сумму бонуса", callback_data="referrals:amount")]
        ])
        if callback.message:
            await replace_callback_message(callback, 
                "👥 УПРАВЛЕНИЕ РЕФЕРАЛАМИ\n\n"
                f"Статус бонуса {amount:g} ₽: {status}\n"
                f"💰 Сумма за нового реферала: {amount:g} ₽\n\n"
                f"👥 Всего рефералов: {int(referrals_total or 0)}\n"
                f"💰 Всего выплачено бонусов: {Decimal(bonus_total or 0):.2f} ₽\n\n"
                f"При отключении бонус {amount:g} ₽ новым рефералам начисляться не будет.\n"
                "Комиссии 10% с покупок и 5% с одобренных выводов продолжат работать.",
                reply_markup=kb
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
                await replace_callback_message(callback, "Пока нет активных пользователей.")
            else:
                lines = ["🏆 ТОП 10 АКТИВНЫХ ПОЛЬЗОВАТЕЛЕЙ\n"]
                for index, user in enumerate(users, 1):
                    username = f"@{user['username']}" if user["username"] else "username не указан"
                    name = user["first_name"] or "Без имени"
                    active_at = user["last_active_at"].strftime("%d.%m.%Y %H:%M")
                    lines.append(f"{index}. {username} — {name}\n   Действий: {user['activity_count']} · Последняя активность: {active_at}")
                await replace_callback_message(callback, "\n".join(lines))
    elif action == "view_messages":
        await state.clear()
        await state.set_state(AdminFlow.message_view_user)
        if callback.message:
            await replace_callback_message(callback, "Введи @username пользователя, чтобы посмотреть последние 10 сообщений, которые ему отправляли.")
    elif action == "broadcast":
        await state.clear()
        await state.set_state(AdminFlow.broadcast_text)
        if callback.message:
            await replace_callback_message(callback, 
                "📢 РАССЫЛКА\n\nОтправь текст, который нужно разослать всем пользователям бота.\n"
                "Можно использовать эмодзи. Для отмены открой админ-панель."
            )


@dp.callback_query(F.data == "referrals:amount")
async def admin_referrals_amount_start(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminFlow.referral_bonus_amount)
    await callback.answer()
    if callback.message:
        await replace_callback_message(callback, 
            "💰 Введи новую сумму бонуса за одного нового реферала в рублях.\n"
            "Например: 2 или 2.50"
        )


@dp.message(AdminFlow.referral_bonus_amount)
async def admin_referrals_amount_save(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip().replace(",", ".")
    try:
        amount = Decimal(raw)
        if not amount.is_finite() or amount <= 0 or amount > Decimal("100000"):
            raise InvalidOperation
        amount = amount.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        await send_flow_message(message.bot, message.chat.id, state, "Введи сумму больше 0 и не больше 100 000 ₽. Например: 2 или 2.50")
        return
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO nobody_settings (key,value) VALUES ('referral_bonus_amount',$1)
               ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value""",
            str(amount)
        )
        enabled_value = await conn.fetchval(
            "SELECT value FROM nobody_settings WHERE key='referral_bonus_enabled'"
        )
        referrals_total = await conn.fetchval(
            "SELECT COUNT(*) FROM nobody_users WHERE referred_by IS NOT NULL"
        )
        bonus_total = await conn.fetchval(
            "SELECT COALESCE(SUM(amount),0) FROM nobody_partner_ledger WHERE kind='referral_bonus'"
        )
    await state.clear()
    enabled = str(enabled_value or "true").lower() == "true"
    status = "🟢 ВКЛЮЧЕН" if enabled else "🔴 ВЫКЛЮЧЕН"
    toggle_label = "🔴 Выключить бонус" if enabled else "🟢 Включить бонус"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_label, callback_data="referrals:toggle")],
        [InlineKeyboardButton(text="💰 Изменить сумму бонуса", callback_data="referrals:amount")]
    ])
    await send_flow_message(message.bot, message.chat.id, state, 
        "✅ Сумма бонуса обновлена!\n\n"
        f"Статус бонуса {amount:g} ₽: {status}\n"
        f"💰 Сумма за нового реферала: {amount:g} ₽\n\n"
        f"👥 Всего рефералов: {int(referrals_total or 0)}\n"
        f"💰 Всего выплачено бонусов: {Decimal(bonus_total or 0):.2f} ₽\n\n"
        "Новая сумма применяется только к новым рефералам.",
        reply_markup=kb
    )


@dp.callback_query(F.data == "referrals:toggle")
async def admin_referrals_toggle(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchval(
                "SELECT value FROM nobody_settings WHERE key='referral_bonus_enabled' FOR UPDATE"
            )
            new_value = "false" if str(current or "true").lower() == "true" else "true"
            await conn.execute(
                """INSERT INTO nobody_settings (key,value) VALUES ('referral_bonus_enabled',$1)
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value""",
                new_value
            )
            amount_value = await conn.fetchval(
                "SELECT value FROM nobody_settings WHERE key='referral_bonus_amount'"
            )
            referrals_total = await conn.fetchval(
                "SELECT COUNT(*) FROM nobody_users WHERE referred_by IS NOT NULL"
            )
            bonus_total = await conn.fetchval(
                "SELECT COALESCE(SUM(amount),0) FROM nobody_partner_ledger WHERE kind='referral_bonus'"
            )
    enabled = new_value == "true"
    amount = Decimal(amount_value or "1")
    status = "🟢 ВКЛЮЧЕН" if enabled else "🔴 ВЫКЛЮЧЕН"
    toggle_label = "🔴 Выключить бонус" if enabled else "🟢 Включить бонус"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_label, callback_data="referrals:toggle")],
        [InlineKeyboardButton(text="💰 Изменить сумму бонуса", callback_data="referrals:amount")]
    ])
    await callback.answer("Бонус включён." if enabled else "Бонус выключен.")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass
        await replace_callback_message(callback, 
            "👥 УПРАВЛЕНИЕ РЕФЕРАЛАМИ\n\n"
            f"Статус бонуса {amount:g} ₽: {status}\n"
            f"💰 Сумма за нового реферала: {amount:g} ₽\n\n"
            f"👥 Всего рефералов: {int(referrals_total or 0)}\n"
            f"💰 Всего выплачено бонусов: {Decimal(bonus_total or 0):.2f} ₽\n\n"
            f"При отключении бонус {amount:g} ₽ новым рефералам начисляться не будет.\n"
            "Комиссии 10% с покупок и 5% с одобренных выводов продолжат работать.",
            reply_markup=kb
        )


@dp.message(AdminFlow.broadcast_text)
async def admin_broadcast_text(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    body = (message.text or "").strip()
    if not body:
        await send_flow_message(message.bot, message.chat.id, state, "Отправь текстовое сообщение для рассылки.")
        return
    if len(body) > 4096:
        await send_flow_message(message.bot, message.chat.id, state, "Сообщение слишком длинное. Максимум — 4096 символов.")
        return
    await state.update_data(broadcast_body=body)
    await state.set_state(AdminFlow.broadcast_confirm)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Отправить всем", callback_data="broadcast:send")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast:cancel")],
    ])
    await send_flow_message(message.bot, message.chat.id, state, 
        "👀 ПРЕДПРОСМОТР РАССЫЛКИ\n\n"
        + body
        + "\n\nОтправить это сообщение всем пользователям?",
        reply_markup=kb
    )


@dp.callback_query(F.data.in_({"broadcast:send", "broadcast:cancel"}))
async def admin_broadcast_action(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    if callback.data == "broadcast:cancel":
        await state.clear()
        await callback.answer("Рассылка отменена.")
        if callback.message:
            await replace_callback_message(callback, "❌ Рассылка отменена.")
        return

    if await state.get_state() != AdminFlow.broadcast_confirm.state:
        await callback.answer("Нет подготовленного сообщения. Начни заново.", show_alert=True)
        return
    data = await state.get_data()
    body = data.get("broadcast_body")
    if not body:
        await state.clear()
        await callback.answer("Текст рассылки не найден.", show_alert=True)
        return

    await state.clear()
    await callback.answer("Рассылка запущена.")
    status_message = None
    if callback.message:
        status_message = await replace_callback_message(callback, "⏳ Рассылка выполняется. Это может занять некоторое время...")

    async with db_pool.acquire() as conn:
        user_ids = await conn.fetch("SELECT telegram_id FROM nobody_users ORDER BY telegram_id")

    sent_count = 0
    failed_count = 0
    for row in user_ids:
        try:
            await bot.send_message(chat_id=row["telegram_id"], text=body)
            sent_count += 1
        except Exception as exc:
            failed_count += 1
            log.warning("Broadcast failed for user %s: %s", row["telegram_id"], exc)
        await asyncio.sleep(0.05)

    total = len(user_ids)
    report = (
        "✅ РАССЫЛКА ЗАВЕРШЕНА\n\n"
        f"👥 Всего пользователей: {total}\n"
        f"📨 Успешно получили: {sent_count}\n"
        f"⚠️ Не удалось доставить: {failed_count}"
    )
    if status_message:
        try:
            await status_message.edit_text(report)
        except Exception:
            await replace_callback_message(callback, report)


@dp.message(AdminFlow.message_view_user)
async def admin_view_user_messages(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    username = (message.text or "").strip().lstrip("@").lower()
    if not username or len(username) > 64 or any(ch.isspace() for ch in username):
        await send_flow_message(message.bot, message.chat.id, state, "Введи корректный @username пользователя.")
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
        await send_flow_message(message.bot, message.chat.id, state, "Пользователь с таким username не найден. Проверь написание.")
        return
    await state.clear()
    if not rows:
        await send_flow_message(message.bot, message.chat.id, state, f"У @{user['username']} пока нет полученных анонимных сообщений.")
        return
    lines = [f"👁 ПОСЛЕДНИЕ 10 СООБЩЕНИЙ ДЛЯ @{user['username']}\n"]
    for index, row in enumerate(rows, 1):
        category = CATEGORIES.get(row["category"], "💬 Другое")
        created = row["created_at"].strftime("%d.%m.%Y %H:%M")
        body = row["body"].strip()
        if len(body) > 300:
            body = body[:300].rstrip() + "…"
        lines.append(f"{index}. {category} · {created}\n{body}")
    await send_flow_message(message.bot, message.chat.id, state, "\n\n".join(lines))



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
        await replace_callback_message(callback, 
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
        eligible_purchases = await conn.fetchval(
            """SELECT COALESCE(-SUM(amount),0) FROM nobody_transactions
               WHERE amount<0 AND (description='Покупка открытки-сердечка' OR description LIKE 'Раскрытие автора%')"""
        )
        purchase_partner_commissions = await conn.fetchval(
            "SELECT COALESCE(SUM(amount),0) FROM nobody_partner_ledger WHERE kind='commission'"
        )
        approved_withdrawal_fees = await conn.fetchval(
            """SELECT COALESCE(SUM(amount*commission_percent/100),0)
               FROM nobody_withdrawals WHERE status='approved'"""
        )
        project_earnings = (
            Decimal(eligible_purchases or 0)
            - Decimal(purchase_partner_commissions or 0)
            + Decimal(approved_withdrawal_fees or 0)
        )
    await callback.answer()
    if callback.message:
        await replace_callback_message(callback, 
            f"💰 ЗАРАБОТОК\n\n"
            f"Общий баланс людей: {Decimal(balance):.2f} ₽\n"
            f"Всего потрачено: {Decimal(spent):.2f} ₽\n"
            f"Доход проекта с открыток, раскрытий и выводов: {project_earnings:.2f} ₽\n\n"
            f"Пополнения за сегодня: {Decimal(topups_today):.2f} ₽\n"
            f"Траты за сегодня: {Decimal(spent_today):.2f} ₽"
        )

@dp.callback_query(F.data.in_({"price:card", "price:reveal", "price:min_topup", "price:min_withdraw"}))
async def admin_price_start(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    kind = {
        "price:card": "card_price",
        "price:reveal": "reveal_price",
        "price:min_topup": "min_topup_amount",
        "price:min_withdraw": "min_withdraw_amount",
    }[callback.data]
    title = {
        "card_price": "открытки",
        "reveal_price": "раскрытия личности",
        "min_topup_amount": "минимальную сумму пополнения",
        "min_withdraw_amount": "минимальную сумму вывода",
    }[kind]
    await state.clear()
    await state.update_data(price_kind=kind)
    await state.set_state(AdminFlow.price_value)
    await callback.answer()
    if callback.message:
        await replace_callback_message(callback, f"Введи новую цену {title} в рублях:")

@dp.message(AdminFlow.price_value)
async def admin_price_save(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        value = Decimal((message.text or "").replace(",", ".").strip()).quantize(Decimal("0.01"))
        limit = Decimal("1000000")
        data_for_limit = await state.get_data()
        minimum_key = data_for_limit.get("price_kind") in ("min_topup_amount", "min_withdraw_amount")
        if value < 0 or (minimum_key and value <= 0) or value > limit:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        await send_flow_message(message.bot, message.chat.id, state, "Введи корректную цену от 0 до 1 000 000 ₽.")
        return
    data = await state.get_data()
    key = data.get("price_kind")
    if key not in ("card_price", "reveal_price", "min_topup_amount", "min_withdraw_amount"):
        await state.clear()
        await send_flow_message(message.bot, message.chat.id, state, "Не удалось определить тип цены. Открой настройки заново.")
        return
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO nobody_settings (key,value) VALUES ($1,$2) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            key, str(value)
        )
    await state.clear()
    label = {
        "card_price": "цена открытки",
        "reveal_price": "цена раскрытия личности",
        "min_topup_amount": "минимальное пополнение",
        "min_withdraw_amount": "минимальный вывод",
    }[key]
    await send_flow_message(message.bot, message.chat.id, state, f"✅ Новое значение ({label}): {value:.2f} ₽")

@dp.message(AdminFlow.balance_user)
async def admin_balance_user(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    try: target = int((message.text or "").strip())
    except ValueError:
        await send_flow_message(message.bot, message.chat.id, state, "Нужен числовой Telegram ID."); return
    async with db_pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM nobody_users WHERE telegram_id=$1", target)
    if not exists:
        await send_flow_message(message.bot, message.chat.id, state, "Пользователь ещё не запускал бота. Сначала он должен нажать /start."); return
    await state.update_data(target_id=target)
    await state.set_state(AdminFlow.balance_amount)
    await send_flow_message(message.bot, message.chat.id, state, "Введи сумму в рублях. Для списания напиши положительное число — действие будет выбрано по предыдущему шагу.")

@dp.message(AdminFlow.balance_amount)
async def admin_balance_amount(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    try:
        amount = Decimal((message.text or "").replace(",", ".").strip()).quantize(Decimal("0.01"))
        if amount <= 0 or amount > Decimal("1000000"): raise InvalidOperation
    except (InvalidOperation, ValueError):
        await send_flow_message(message.bot, message.chat.id, state, "Введи корректную сумму больше 0."); return
    data = await state.get_data()
    target = data["target_id"]
    action = data["balance_action"]
    delta = amount if action == "credit" else -amount
    ref = f"admin:{message.from_user.id}:{message.message_id}"
    try:
        await change_balance(target, delta, "Ручное начисление администратором" if delta > 0 else "Ручное списание администратором", ref)
    except ValueError:
        await send_flow_message(message.bot, message.chat.id, state, "У пользователя недостаточно средств."); return
    await state.clear()
    await send_flow_message(message.bot, message.chat.id, state, f"✅ Операция выполнена. Новый баланс: {await get_balance(target):.2f} ₽")

async def retry_telegram_request(make_request, bot, method):
    # Temporary Telegram network failures should not immediately break user replies.
    # Database operations are not retried here; only outgoing Telegram API calls.
    if getattr(method, "__api_method__", "") == "getUpdates":
        return await make_request(bot, method)

    for attempt in range(3):
        try:
            return await make_request(bot, method)
        except (TelegramNetworkError, TelegramServerError):
            if attempt == 2:
                raise
            delay = attempt + 1
            log.warning(
                "Telegram API request %s failed; retrying in %s sec (attempt %s/3)",
                getattr(method, "__api_method__", type(method).__name__),
                delay,
                attempt + 1,
            )
            await asyncio.sleep(delay)


async def main():
    if not BOT_TOKEN: raise RuntimeError("Не задан BOT_TOKEN в переменных Railway.")
    await init_database()
    bot = Bot(token=BOT_TOKEN)
    bot.session.middleware(retry_telegram_request)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        if db_pool is not None: await db_pool.close()

if __name__ == "__main__":
    asyncio.run(main())