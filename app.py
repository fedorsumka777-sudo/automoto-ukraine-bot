import os
import logging
import html
import re
from contextlib import asynccontextmanager

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, Update
from fastapi import FastAPI, Request, HTTPException

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["BOT_TOKEN"]
DB_URL = os.environ["DATABASE_URL"]
PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")
SECRET = os.environ["WEBHOOK_SECRET"]
GROUPS = {
    "🚗 Авторинок": os.getenv("GROUP_AUTORINOK", "@AutoMotoAvtorunok"),
    "🚜 Агромаркет": os.getenv("GROUP_AGRO", "@AutoMotoAgromarket"),
    "⚙️ Запчастини": os.getenv("GROUP_PARTS", "@AutoMotoZapchastunu"),
}
bot = Bot(TOKEN)
dp = Dispatcher()
db = None
sessions = {}
menu = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="🚘 Створити оголошення"), KeyboardButton(text="🔎 Пошук")],
    [KeyboardButton(text="📋 Мої оголошення"), KeyboardButton(text="❌ Скасувати")],
], resize_keyboard=True)
categories = ReplyKeyboardMarkup(keyboard=[
    *[[KeyboardButton(text=x)] for x in GROUPS],
    [KeyboardButton(text="❌ Скасувати")],
], resize_keyboard=True)


@dp.message(CommandStart())
async def start(m: Message):
    if not m.from_user:
        return
    sessions.pop(m.from_user.id, None)
    await m.answer("🇺🇦 АВТО МОТО УКРАЇНА\nБезкоштовні оголошення. Обери дію 👇", reply_markup=menu)


@dp.message(F.text == "❌ Скасувати")
async def cancel(m: Message):
    if m.from_user:
        sessions.pop(m.from_user.id, None)
    await m.answer("Скасовано.", reply_markup=menu)


@dp.message(F.text == "🚘 Створити оголошення")
async def new_ad(m: Message):
    if not m.from_user:
        return
    sessions[m.from_user.id] = {"step": "category", "data": {}}
    await m.answer("🗂️ Обери категорію:", reply_markup=categories)


@dp.message(F.text == "🔎 Пошук")
async def search(m: Message):
    if not m.from_user:
        return
    sessions[m.from_user.id] = {"step": "search", "data": {}}
    await m.answer("🔎 Введи назву, марку або модель:", reply_markup=menu)


@dp.message(F.text == "📋 Мої оголошення")
async def mine(m: Message):
    if not m.from_user:
        return
    async with db.acquire() as con:
        rows = await con.fetch("SELECT id,title,price,city FROM ads WHERE user_id=$1 ORDER BY id DESC LIMIT 10", m.from_user.id)
    await m.answer("\n\n".join(f"№{r['id']} — {r['title']}, {r['price']}, {r['city']}" for r in rows) or "Оголошень немає.", reply_markup=menu)


EXTRAS = {
    "🚗 Авторинок": [
        ("year", "📅 Рік випуску? (наприклад 2012)"),
        ("spec", "🛣️ Пробіг у км? (наприклад 185000)"),
    ],
    "🚜 Агромаркет": [
        ("year", "📅 Рік випуску техніки?"),
        ("spec", "🚜 Тип і стан техніки? (наприклад трактор, справний)"),
    ],
    "⚙️ Запчастини": [
        ("year", "🔧 Для якої техніки / моделі підходить?"),
        ("spec", "📦 Стан: нова чи б/в?"),
    ],
}
CURRENCY = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="🇺🇦 UAH (грн)"), KeyboardButton(text="🇺🇸 USD ($)")],
    [KeyboardButton(text="❌ Скасувати")],
], resize_keyboard=True)
CONFIRM = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="✅ Опублікувати"), KeyboardButton(text="✏️ Почати заново")],
    [KeyboardButton(text="❌ Скасувати")],
], resize_keyboard=True)


def next_question(d):
    fields = [
        ("title", "🏷️ Марка, модель або назва товару?"),
        *EXTRAS[d["category"]],
        ("description", "📝 Коротко опиши стан і особливості товару:"),
        ("price", "💰 Вкажи ціну тільки цифрами (наприклад 8500):"),
        ("currency", "💵 Обери валюту:"),
        ("city", "📍 Місто або область?"),
        ("contact", "Телефон або Telegram для зв'язку?"),
        ("photo", "📸 Надішли фотографію товару. Без фото оголошення не опублікується."),
    ]
    for field, prompt in fields:
        if field not in d:
            return field, prompt
    return "preview", ""


def render_ad(d, user_id, username, first_name, ad_id=None):
    category = d["category"]
    extra = EXTRAS[category]
    who = "@" + html.escape(username) if username else (
        f'<a href="tg://user?id={user_id}">{html.escape(first_name or "Продавець")}</a>'
    )
    lines = [
        f"<b>{html.escape(d['title'])}</b>",
        html.escape(category),
        f"🔹 {html.escape(extra[0][1].split('?')[0])}: {html.escape(d[extra[0][0]])}",
        f"🔹 {html.escape(extra[1][1].split('?')[0])}: {html.escape(d[extra[1][0]])}",
        f"📝 {html.escape(d['description'])}",
        f"💰 <b>{html.escape(d['price'])} {html.escape(d['currency'])}</b>",
        f"📍 {html.escape(d['city'])}",
        f"☎️ {html.escape(d['contact'])}",
        f"👤 Продавець: {who}",
    ]
    if ad_id is not None:
        lines.append(f"🔖 Оголошення №{ad_id}")
    return "\n".join(lines)


async def ask_next(m, s):
    step, prompt = next_question(s["data"])
    s["step"] = step
    if step == "preview":
        preview_kwargs = {
            "caption": render_ad(s["data"], m.from_user.id, m.from_user.username, m.from_user.first_name),
            "parse_mode": "HTML",
            "reply_markup": CONFIRM,
        }
        if s["data"].get("photo_kind") == "document":
            await m.answer_document(s["data"]["photo_id"], **preview_kwargs)
        else:
            await m.answer_photo(s["data"]["photo_id"], **preview_kwargs)
        await m.answer("Перевір оголошення. Опублікувати чи почати заново?", reply_markup=CONFIRM)
    else:
        await m.answer(prompt, reply_markup=CURRENCY if step == "currency" else menu)


@dp.message(F.text == "✏️ Почати заново")
async def restart(m: Message):
    if not m.from_user or m.chat.type != "private":
        return
    sessions[m.from_user.id] = {"step": "category", "data": {}}
    await m.answer("Обери категорію:", reply_markup=categories)


@dp.message(F.text == "✅ Опублікувати")
async def publish(m: Message):
    if not m.from_user or m.chat.type != "private":
        return
    s = sessions.get(m.from_user.id)
    if not s or s["step"] != "preview":
        await m.answer("Немає готового оголошення. Натисни /start.")
        return
    s["step"] = "publishing"
    d = s["data"]
    extras = EXTRAS[d["category"]]
    description = (f"{extras[0][1]} {d[extras[0][0]]}\n"
                   f"{extras[1][1]} {d[extras[1][0]]}\n{d['description']}")
    try:
        async with db.acquire() as con:
            ad_id = await con.fetchval(
                """INSERT INTO ads (user_id,username,category,title,description,price,city,contact,photo_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id""",
                m.from_user.id, m.from_user.username, d["category"], d["title"],
                description, f"{d['price']} {d['currency']}", d["city"], d["contact"], d["photo_id"]
            )
        target = GROUPS[d["category"]]
        try:
            post_kwargs = {
                "caption": render_ad(d, m.from_user.id, m.from_user.username, m.from_user.first_name, ad_id),
                "parse_mode": "HTML",
            }
            if d.get("photo_kind") == "document":
                await bot.send_document(target, d["photo_id"], **post_kwargs)
            else:
                await bot.send_photo(target, d["photo_id"], **post_kwargs)
            status = "✅ Оголошення опубліковано в групі!"
        except Exception:
            logging.exception("Failed to publish ad %s", ad_id)
            status = "⚠️ Оголошення збережено, але публікація в групі не вдалася. Повідом адміністратора."
        sessions.pop(m.from_user.id, None)
        await m.answer(f"{status}\nНомер №{ad_id}", reply_markup=menu)
    except Exception:
        s["step"] = "preview"
        logging.exception("Failed to save ad")
        await m.answer("Помилка збереження. Спробуй підтвердити ще раз.", reply_markup=CONFIRM)


@dp.message()
async def wizard(m: Message):
    if not m.from_user or m.chat.type != "private":
        return
    s = sessions.get(m.from_user.id)
    if not s:
        await m.answer("Натисни /start для початку.", reply_markup=menu)
        return
    step, d = s["step"], s["data"]
    value = (m.text or "").strip()
    if step == "publishing":
        await m.answer("Зачекай, оголошення публікується.")
        return
    if step == "preview":
        await m.answer("Обери «✅ Опублікувати» або «✏️ Почати заново».", reply_markup=CONFIRM)
        return
    if step == "search":
        if len(value) < 2 or len(value) > 100:
            await m.answer("Введи від 2 до 100 символів.")
            return
        async with db.acquire() as con:
            rows = await con.fetch(
                "SELECT id,category,title,price,city FROM ads WHERE status='active' AND (title ILIKE $1 OR description ILIKE $1) ORDER BY id DESC LIMIT 10",
                "%" + value.replace("%", r"\%").replace("_", r"\_") + "%"
            )
        sessions.pop(m.from_user.id, None)
        await m.answer("\n\n".join(f"№{r['id']} {r['category']}\n{r['title']}\n💰 {r['price']} 📍 {r['city']}" for r in rows) or "Нічого не знайдено.", reply_markup=menu)
        return
    if step == "category":
        if value not in GROUPS:
            await m.answer("Обери категорію кнопкою.", reply_markup=categories)
            return
        d["category"] = value
        await ask_next(m, s)
        return
    if step == "photo":
        if not m.photo:
            await m.answer("📸 Фото обов'язкове. Надішли одне фото товару.")
            return
        d["photo_id"] = m.photo[-1].file_id
        await ask_next(m, s)
        return
    if step == "currency":
        if value not in ("🇺🇦 UAH (грн)", "🇺🇸 USD ($)"):
            await m.answer("Обери валюту кнопкою.", reply_markup=CURRENCY)
            return
        d["currency"] = "грн" if "UAH" in value else "USD"
    elif step == "price":
        if not re.fullmatch(r"\d{1,10}(?:[.,]\d{1,2})?", value):
            await m.answer("Введи суму цифрами, без валюти. Наприклад: 8500")
            return
        d[step] = value.replace(",", ".")
    else:
        if not value or len(value) > (280 if step == "description" else 90):
            await m.answer("Введи коротку відповідь (до 90 символів, опис — до 280).")
            return
        d[step] = value
    await ask_next(m, s)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = await asyncpg.create_pool(DB_URL, min_size=1, max_size=3)
    async with db.acquire() as con:
        await con.execute("""CREATE TABLE IF NOT EXISTS ads (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL, username TEXT, category TEXT NOT NULL,
            title TEXT NOT NULL, description TEXT NOT NULL, price TEXT NOT NULL,
            city TEXT NOT NULL, contact TEXT NOT NULL, photo_id TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""")
        await con.execute("CREATE INDEX IF NOT EXISTS ads_user_idx ON ads(user_id)")
    await bot.set_webhook(PUBLIC_URL + "/telegram", secret_token=SECRET)
    yield
    # Do not delete webhook on shutdown: Render replaces instances during deploys.
    await bot.session.close()
    await db.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def health():
    return {"status": "ok", "service": "automoto-ua"}


@app.post("/telegram")
async def telegram(request: Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        raise HTTPException(status_code=403)
    update = Update.model_validate(await request.json())
    await dp.feed_update(bot, update)
    return {"ok": True}
