import os
import json
import time
import asyncio
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import db
from db import init_db, upsert_user, count_available_codes, take_code_for_user, add_codes, export_remaining_codes, get_setting, set_setting

load_dotenv()

# ---------- Config ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()  # username (начинается с @) или числовой chat_id (-100...)
GIFTS_JSON = os.getenv("GIFTS_JSON", "").strip()  # JSON список объектов gift [{'id':'emoji_gift_...','name':'...'},...]
GIFT_NAME = os.getenv("GIFT_NAME", "🎁 Подарок")
GIFT_PHOTO_URL = os.getenv("GIFT_PHOTO_URL", "https://i.imgur.com/3iY9F6P.png")  # 512x512 рекомендуется
ONLY_ONCE = os.getenv("ONLY_ONCE", "1") == "1"  # выдавать только один раз на пользователя
REQUIRE_CHANNEL = os.getenv("REQUIRE_CHANNEL", CHANNEL_ID)  # можно продублировать, но обычно CHANNEL_ID
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/tg/webhook").strip()
PORT = int(os.getenv("PORT", "8080"))
REDIS_URL = os.getenv("REDIS_URL", "").strip()

# Если нет токена — выходим
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is required")

# Парсим список подарков Star Gifts
GIFTS: List[Dict[str, str]] = []
if GIFTS_JSON:
    try:
        GIFTS = json.loads(GIFTS_JSON)
        if not isinstance(GIFTS, list) or not all('id' in g and 'name' in g for g in GIFTS):
            raise ValueError
    except Exception:
        raise SystemExit("GIFTS_JSON must be a JSON list of objects with 'id' and 'name'")

DEFAULT_GIFT_ID = GIFTS[0]["id"] if GIFTS else None

router = Router()
storage = MemoryStorage() if not REDIS_URL else None  # при Redis лучше переключить на RedisStorage
dp = Dispatcher(storage=storage, router=router)

# ---------- Bot helpers ----------
def to_channel_id(channel: str) -> str:
    # Если username с @ — вернем как есть, иначе оставим как есть (может быть -100...)
    if channel.startswith("@"):
        return channel
    return channel

def build_claim_keyboard(pending: bool = False) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text=GIFT_NAME, callback_data="claim:gift")],
    ]
    if pending:
        kb.append([InlineKeyboardButton(text="🔔 Я подписался(ась)", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

async def is_subscribed(bot: Bot, user_id: int, channel: str) -> bool:
    ch = to_channel_id(channel)
    try:
        member = await bot.get_chat_member(chat_id=ch, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        # Если бот не админ, get_chat_member вернет ошибку
        return False

async def try_send_star_gift(bot: Bot, user_id: int, gift_id: str, text: Optional[str] = None) -> bool:
    # Проверяем доступность метода sendGift
    method = bot.session.session.api_object.api_request
    if not hasattr(method, "__self__"):
        # aiogram 3.x не раскрывает напрямую метод Bot API, используем низкоуровневый вызов
        pass

    # Формируем запрос вручную через Bot API
    payload = {
        "user_id": user_id,
        "gift_id": gift_id,
    }
    if text:
        payload["text"] = text
    # photo можно передать как Upload или URL
    # payload["photo"] = ...  # опционально

    try:
        # используем низкоуровневый вызов
        from aiogram.methods import SendGift
        req = SendGift(**payload)
        resp = await bot(req)
        # aiogram вернет объект Response с полем ok и result
        return getattr(resp, "ok", True)  # допускаем, что ok=True
    except Exception as e:
        # логируем и возвращаем False
        print(f"sendGift failed: {e}")
        return False

async def safe_send_text(bot: Bot, chat_id: int, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None):
    try:
        await bot.send_message(chat_id, text, reply_markup=reply_markup)
    except Exception as e:
        print(f"send_message failed: {e}")

async def try_send_promo(bot: Bot, user_id: int, code: str) -> bool:
    try:
        await bot.send_message(user_id, f"🎉 Ваш промокод: <code>{code}</code>\nИспользуйте его в боте/на сайте.", parse_mode="HTML")
        return True
    except Exception as e:
        print(f"send promo failed: {e}")
        return False

# ---------- States ----------
class GiftState(StatesGroup):
    waiting_claim = State()

# ---------- Filters ----------
class IsAdmin:
    async def __call__(self, message: Message) -> bool:
        # проверяем, что юзер — админ из настроек или список через запятую
        admins_raw = (await get_setting("admins")) or os.getenv("ADMINS", "")
        if not admins_raw:
            return False
        ids = [int(x.strip()) for x in admins_raw.split(",") if x.strip().isdigit()]
        return message.from_user.id in ids

# ---------- Handlers ----------
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user = message.from_user
    if not user:
        return
    await upsert_user(user.id, user.username)
    await state.clear()
    # Проверяем подписку
    sub = await is_subscribed(message.bot, user.id, REQUIRE_CHANNEL)

    if sub:
        await safe_send_text(
            message.bot, user.id,
            "Спасибо! Вы подписаны. Можно получить подарок.",
            reply_markup=build_claim_keyboard(pending=False)
        )
    else:
        text = (
            "👋 Привет! Для получения подарка нужно подписаться на канал.\n"
            f"Канал: {REQUIRE_CHANNEL}\n\n"
            "После подписки нажмите «Я подписался(ась)»."
        )
        await safe_send_text(
            message.bot, user.id, text,
            reply_markup=build_claim_keyboard(pending=True)
        )

@router.message(Command("gift"))
async def cmd_gift(message: Message, state: FSMContext):
    user = message.from_user
    if not user:
        return
    await upsert_user(user.id, user.username)
    await state.set_state(GiftState.waiting_claim)
    await cmd_start(message, state)

@router.message(Command("balance"), IsAdmin())
async def cmd_balance(message: Message):
    left = await count_available_codes()
    await message.answer(f"Промокодов осталось: {left}")

@router.message(Command("export"), IsAdmin())
async def cmd_export(message: Message):
    limit = None
    parts = message.get_args().strip().split()
    if parts and parts[0].isdigit():
        limit = int(parts[0])
    codes = await export_remaining_codes(limit)
    if not codes:
        await message.answer("Нет доступных кодов.")
        return
    # Отправляем отдельным файлом
    txt = "\n".join(codes)
    await message.answer_document(types.BufferedInputFile(txt.encode("utf-8"), filename="promo_codes.txt"))

@router.message(Command("add"), IsAdmin())
async def cmd_add(message: Message):
    # добавить коды из аргументов (через пробел/запятую/перенос)
    text = message.get_args().strip()
    if not text:
        await message.answer("Использование: /add CODE1 CODE2 CODE3 ...")
        return
    raw = text.replace(",", " ").replace("\n", " ")
    codes = [c.strip() for c in raw.split() if c.strip()]
    await add_codes(codes)
    await message.answer(f"Добавлено кодов: {len(codes)}")

@router.message(IsAdmin(), F.chat.type == "private", Command("promo"))
async def promo_from_admin(message: Message):
    # Отправьте боту сообщение формата: /promo
    # 123456789: SOMECODE
    # ...
    # где 123456789 — user_id
    await message.answer("Отправьте список вида:\nuser_id: CODE\nчтобы выдать промокод вручную. (Пока не реализовано парсинг).")

@router.callback_query(F.data == "check_sub")
async def cb_check_sub(callback: types.CallbackQuery, state: FSMContext):
    user = callback.from_user
    await callback.answer()
    if not user:
        return
    sub = await is_subscribed(callback.bot, user.id, REQUIRE_CHANNEL)
    if sub:
        await callback.message.edit_text(
            "Отлично! Подписка подтверждена.\nМожно получить подарок.",
            reply_markup=build_claim_keyboard(pending=False)
        )
    else:
        await callback.message.edit_text(
            "Подписка не найдена. Подпишитесь на канал и повторите попытку.",
            reply_markup=build_claim_keyboard(pending=True)
        )

@router.callback_query(F.data == "claim:gift")
async def cb_claim(callback: types.CallbackQuery, state: FSMContext):
    user = callback.from_user
    await callback.answer()
    if not user:
        return
    await upsert_user(user.id, user.username)

    # Доп. проверка подписки перед выдачей
    sub = await is_subscribed(callback.bot, user.id, REQUIRE_CHANNEL)
    if not sub:
        await callback.message.edit_text(
            "Нужно подписаться на канал для получения подарка.",
            reply_markup=build_claim_keyboard(pending=True)
        )
        return

    # Если запрещено выдавать повторно — проверим, что ранее не получали (по наличию в настройках)
    if ONLY_ONCE:
        already = await get_setting(f"gift_received_{user.id}")
        if already:
            await callback.message.edit_text("Вы уже получали подарок. Спасибо!")
            return

    # Стратегия выдачи:
    # 1) Если есть Star Gifts и бот поддерживает sendGift — отправляем Star Gift
    # 2) Иначе — выдаем промокод из БД
    sent = False
    if DEFAULT_GIFT_ID:
        sent = await try_send_star_gift(callback.bot, user.id, DEFAULT_GIFT_ID, text="Спасибо за подписку!")

    if not sent:
        # пробуем промокоды
        code_row = await take_code_for_user(user.id)
        if not code_row:
            await callback.message.edit_text("К сожалению, подарки закончились. Попробуйте позже.")
            return
        code = code_row[0]
        ok = await try_send_promo(callback.bot, user.id, code)
        if not ok:
            await callback.message.edit_text("Не удалось отправить подарок. Напишите @support.")
            return

    # Отмечаем, что выдано (для ONLY_ONCE)
    if ONLY_ONCE:
        await set_setting(f"gift_received_{user.id}", str(int(time.time())))

    await callback.message.edit_text(
        "✅ Подарок отправлен! Если это был промокод — проверьте личные сообщения."
    )

# ---------- Webhook route ----------
from fastapi import FastAPI, Request, Response
import uvicorn

app = FastAPI()
webhook_ready = False

@app.on_event("startup")
async def startup():
    await init_db()
    # Устанавливаем команды в меню
    bot = Bot(token=BOT_TOKEN)
    await bot.set_my_commands([
        BotCommand(command="start", description="Старт / Проверка подписки"),
        BotCommand(command="gift", description="Получить подарок"),
    ])
    # Если указан WEBHOOK_URL — ставим webhook
    global webhook_ready
    if WEBHOOK_URL:
        url = WEBHOOK_URL.rstrip("/") + WEBHOOK_PATH
        try:
            await bot.set_webhook(url, drop_pending_updates=True)
            webhook_ready = True
            print(f"Webhook set: {url}")
        except Exception as e:
            print(f"Failed to set webhook: {e}")
    else:
        webhook_ready = False
        print("Using long polling")

@app.post(WEBHOOK_PATH)
async def tg_webhook(request: Request):
    if not webhook_ready:
        return Response(status_code=200)
    bot = Bot(token=BOT_TOKEN)
    update = await request.json()
    # aiogram 3 поддерживает updates из Bot API напрямую
    tg_update = types.Update(**update)
    await dp.feed_update(bot=bot, update=tg_update)
    return Response(status_code=200)

# ---------- Main ----------
async def main():
    await init_db()
    bot = Bot(token=BOT_TOKEN)

    if WEBHOOK_URL:
        # Сервер на FastAPI уже поднят через uvicorn.run ниже
        pass
    else:
        # Long polling
        try:
            await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
        finally:
            await bot.session.close()

if __name__ == "__main__":
    if WEBHOOK_URL:
        uvicorn.run("main:app", host="0.0.0.0", port=PORT)
    else:
        asyncio.run(main())
