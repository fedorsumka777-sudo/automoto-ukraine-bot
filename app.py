import os
import logging
from contextlib import asynccontextmanager

import aiosqlite
from pathlib import Path
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
    "🚗 Авторинок": os.getenv("GROUP_AUTORINOK"),
    "🚜 Агромаркет": os.getenv("GROUP_AGRO"),
    "🔧 Запчастини": os.getenv("GROUP_PARTS"),
    "🚛 Вантажівки": os.getenv("GROUP_TRUCKS"),
    "🏍 Мототехніка": os.getenv("GROUP_MOTO"),
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
    await m.answer("Обери категорію:", reply_markup=categories)


@dp.message(F.text == "🔎 Пошук")
async def search(m: Message):
    if not m.from_user:
        return
    sessions[m.from_user.id] = {"step": "search", "data": {}}
    await m.answer("Введи назву, марку або модель:", reply_markup=menu)


@dp.message(F.text == "📋 Мої оголошення")
async def mine(m: Message):
    if not m.from_user:
        return
    async with db.execute("SELECT id,title,price,city FROM ads WHERE user_id=? ORDER BY id DESC LIMIT 10", (m.from_user.id,)) as cursor:
        rows = await cursor.fetchall()
    await m.answer("\n\n".join(f"№{r['id']} — {r['title']}, {r['price']}, {r['city']}" for r in rows) or "Оголошень немає.", reply_markup=menu)


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
    if step == "search":
        if len(value) < 2 or len(value) > 100:
            await m.answer("Введи від 2 до 100 символів.")
            return
        async with db.execute(
                "SELECT id,category,title,price,city FROM ads WHERE status='active' AND (LOWER(title) LIKE ? OR LOWER(description) LIKE ?) ORDER BY id DESC LIMIT 10",
                ("%" + value.lower() + "%", "%" + value.lower() + "%")
            ) as cursor:
            rows = await cursor.fetchall()
        sessions.pop(m.from_user.id, None)
        await m.answer("\n\n".join(f"№{r['id']} {r['category']}\n{r['title']}\n💰 {r['price']} 📍 {r['city']}" for r in rows) or "Нічого не знайдено.", reply_markup=menu)
        return
    if step == "category":
        if value not in GROUPS:
            await m.answer("Обери категорію кнопкою.", reply_markup=categories)
            return
        d["category"] = value
        s["step"] = "title"
        await m.answer("Напиши марку, модель або назву товару:", reply_markup=menu)
        return
    prompts = {
        "title": ("description", "Опиши характеристики та стан:"),
        "description": ("price", "Вкажи ціну й валюту (наприклад 8500 USD):"),
        "price": ("city", "Вкажи місто або область:"),
        "city": ("contact", "Вкажи телефон або Telegram для зв'язку:"),
        "contact": ("photo", "Надішли одне фото або напиши «Пропустити»:"),
    }
    if step in prompts:
        if not value or len(value) > (100 if step in ("title","price","city","contact") else 900):
            await m.answer("Введи коректний текст (для опису до 900 символів, для інших полів до 100).")
            return
        d[step] = value
        s["step"], question = prompts[step]
        await m.answer(question)
        return
    if step == "photo":
        if m.photo:
            d["photo_id"] = m.photo[-1].file_id
        elif value.lower() != "пропустити":
            await m.answer("Надішли фотографію або напиши «Пропустити».")
            return
        async with db.execute(
                """INSERT INTO ads (user_id,username,category,title,description,price,city,contact,photo_id)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (m.from_user.id, m.from_user.username, d["category"], d["title"],
                d["description"], d["price"], d["city"], d["contact"], d.get("photo_id"))
            ) as cursor:
            ad_id = cursor.lastrowid
        await db.commit()
        content = (f"🚘 {d['title']}\n📂 {d['category']}\n📝 {d['description']}\n"
                   f"💰 {d['price']}\n📍 {d['city']}\n☎️ {d['contact']}\n🔖 №{ad_id}")
        published = False
        target = GROUPS[d["category"]]
        if target:
            try:
                if d.get("photo_id"):
                    await bot.send_photo(int(target), d["photo_id"], caption=content[:1024])
                else:
                    await bot.send_message(int(target), content)
                published = True
            except Exception:
                logging.exception("Cannot publish ad %s", ad_id)
        sessions.pop(m.from_user.id, None)
        await m.answer(f"✅ Оголошення №{ad_id} збережено!" + (" Опубліковано в групі." if published else " Публікація в групі поки недоступна."), reply_markup=menu)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db_path = Path(os.getenv("SQLITE_PATH", "/var/data/automoto.db"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
    await db.execute("""CREATE TABLE IF NOT EXISTS ads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, username TEXT, category TEXT NOT NULL,
        title TEXT NOT NULL, description TEXT NOT NULL, price TEXT NOT NULL,
        city TEXT NOT NULL, contact TEXT NOT NULL, photo_id TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    await db.execute("CREATE INDEX IF NOT EXISTS ads_user_idx ON ads(user_id)")
    await db.commit()
    await bot.set_webhook(PUBLIC_URL + "/telegram", secret_token=SECRET)
    yield
    await bot.delete_webhook()
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
