import os
import logging
import html
import re
from contextlib import asynccontextmanager

import asyncpg
from catalog import REGIONS, OTHER, keyboard, makes, models, matches, settlements, PLACES
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, Update, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, InputMediaPhoto, InputMediaDocument
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
], resize_keyboard=True, is_persistent=True)
categories = ReplyKeyboardMarkup(keyboard=[
    *[[KeyboardButton(text=x)] for x in GROUPS],
    [KeyboardButton(text="❌ Скасувати")],
], resize_keyboard=True)




async def pin_bot_entry(chat_id):
    me = await bot.get_me()
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 Створити оголошення", url=f"https://t.me/{me.username}?start=group")
    ]])
    notice = await bot.send_message(
        chat_id,
        "🇺🇦 АВТО МОТО УКРАЇНА\n"
        "🚘 Безкоштовні оголошення про транспорт, сільгосптехніку та запчастини.\n"
        "✍️ Можна публікувати самостійно або оформити оголошення через бота 👇",
        reply_markup=markup,
    )
    try:
        await bot.pin_chat_message(chat_id, notice.message_id, disable_notification=True)
    except Exception:
        logging.warning("Cannot pin bot entry in group %s; check admin pin permissions", chat_id)


@dp.message(F.text.startswith("/pinbot"))
async def pin_bot_command(m: Message):
    if m.chat.type not in ("group", "supergroup") or not m.from_user:
        return
    member = await bot.get_chat_member(m.chat.id, m.from_user.id)
    if member.status not in ("administrator", "creator"):
        return
    await pin_bot_entry(m.chat.id)


@dp.message(F.new_chat_members)
async def welcome_member(m: Message):
    if m.chat.type not in ("group", "supergroup"):
        return
    me = await bot.get_me()
    if any(u.id == me.id for u in m.new_chat_members):
        await pin_bot_entry(m.chat.id)
        return
    newcomers = [u for u in m.new_chat_members if not u.is_bot]
    if not newcomers:
        return
    buttons = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 Створити оголошення через бота",
                             url=f"https://t.me/{me.username}?start=group")
    ]])
    await m.answer(
        "🇺🇦 Вітаємо в АВТО МОТО УКРАЇНА!\n"
        "🚘 Тут безкоштовно продають та купують транспорт, техніку й запчастини.\n"
        "✍️ Публікуй оголошення самостійно або через наш бот 👇",
        reply_markup=buttons,
    )


def region_places(code):
    return sorted([p for p in PLACES if p[0] == code], key=lambda p: (p[1].casefold(), p[3]))


def selections(s):
    step, d = s["step"], s["data"]
    if step == "make":
        return makes(d["category"])
    if step == "model":
        return models(d["category"], d["make"])
    if step == "region":
        return sorted(REGIONS)
    return []


def selection_keyboard(s, page=0):
    values = selections(s)
    max_page = max(0, (len(values) - 1) // 12)
    page = max(0, min(max_page, page))
    rows = []
    for j in range(page * 12, min((page + 1) * 12, len(values)), 2):
        rows.append([InlineKeyboardButton(text=values[i][:60], callback_data=f"select:v:{i}")
                     for i in range(j, min(j+2, len(values)))])
    controls = []
    if page:
        controls.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"select:p:{page-1}"))
    if page < max_page:
        controls.append(InlineKeyboardButton(text="Далі ➡️", callback_data=f"select:p:{page+1}"))
    if controls:
        rows.append(controls)
    if s["step"] == "make":
        rows.append([InlineKeyboardButton(text="✍️ Інша марка", callback_data="select:custom:0")])
    if s["step"] == "model":
        rows.append([InlineKeyboardButton(text="✍️ Інша модель", callback_data="select:custom:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_selection(m, s):
    prompts = {"make": "🚘 Обери марку:", "model": "🚗 Обери модель:",
               "region": "📍 Обери область:"}
    await m.answer(prompts[s["step"]], reply_markup=selection_keyboard(s))


@dp.callback_query(F.data.startswith("select:"))
async def select_callback(c: CallbackQuery):
    s = sessions.get(c.from_user.id)
    if not s or s["step"] not in ("make", "model", "region"):
        await c.answer("Це меню вже неактуальне.", show_alert=True)
        return
    try:
        _, action, raw = c.data.split(":")
        index = int(raw)
    except (ValueError, AttributeError):
        await c.answer("Помилка вибору")
        return
    if action == "p":
        await c.message.edit_reply_markup(reply_markup=selection_keyboard(s, index))
        await c.answer()
        return
    if action == "custom" and s["step"] in ("make", "model"):
        s["step"] = "custom_make" if s["step"] == "make" else "custom_model"
        await c.message.answer("✍️ Введи назву марки:" if s["step"] == "custom_make" else "✍️ Введи модель:", reply_markup=menu)
        await c.answer()
        return
    options = selections(s)
    if action != "v" or index < 0 or index >= len(options):
        await c.answer("Некоректний вибір")
        return
    chosen = options[index]
    d, step = s["data"], s["step"]
    if step == "make":
        d["make"] = chosen
        s["step"] = "model"
        await show_selection(c.message, s)
    elif step == "model":
        d["model"] = chosen
        d["title"] = d["make"] + " " + chosen
        await ask_next(c.message, s, c.from_user)
    elif step == "region":
        d["region"] = chosen
        d["region_code"] = REGIONS[chosen]
        if chosen in ("Київ", "Севастополь"):
            d["city"] = chosen
        await ask_next(c.message, s, c.from_user)
    await c.answer()
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


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
PHOTO_DONE = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="✅ Фото готові")], [KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True)
CONFIRM = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="✅ Опублікувати"), KeyboardButton(text="✏️ Почати заново")],
    [KeyboardButton(text="❌ Скасувати")],
], resize_keyboard=True)


def next_question(d):
    fields = [
        *(([("make", "🚘 Обери марку зі списку:")]) if d["category"] != "⚙️ Запчастини" else [("title", "🏷️ Назва запчастини?")]),
        *EXTRAS[d["category"]],
        ("description", "📝 Коротко опиши стан і особливості товару:"),
        ("price", "💰 Вкажи ціну тільки цифрами (наприклад 8500):"),
        ("currency", "💵 Обери валюту:"),
        ("region", "📍 Обери область України:"),
        ("city", "🏘️ Напиши назву міста, селища або села:"),
        ("contact", "Телефон або Telegram для зв'язку?"),
        ("photo", "📸 Надішли щонайменше 5 фотографій (можна альбомом), максимум 10."),
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
        f"<b>{html.escape(category.upper())}</b>",
        f"<b>{html.escape(d['title'])}</b>",
        f"🔹 {html.escape(extra[0][1].split('?')[0])}: {html.escape(d[extra[0][0]])}",
        f"🔹 {html.escape(extra[1][1].split('?')[0])}: {html.escape(d[extra[1][0]])}",
        f"📝 {html.escape(d['description'])}",
        f"💰 <b>{html.escape(d['price'])} {html.escape(d['currency'])}</b>",
        f"📍 {html.escape(d.get('region', ''))}, {html.escape(d['city'])}",
        f"☎️ {html.escape(d['contact'])}",
        f"👤 Продавець: {who}",
    ]
    if ad_id is not None:
        lines.append(f"🔖 Оголошення №{ad_id}")
    return "\n".join(lines)


async def ask_next(m, s, user=None):
    user = user or m.from_user
    step, prompt = next_question(s["data"])
    s["step"] = step
    if step == "preview":
        await m.answer("📸 Додано фотографій: " + str(len(s["data"]["photos"])) + ". Перевір текст оголошення:")
        await m.answer(render_ad(s["data"], user.id, user.username, user.first_name),
                       parse_mode="HTML", reply_markup=CONFIRM)
        await m.answer("Перевір оголошення. Опублікувати чи почати заново?", reply_markup=CONFIRM)
    else:
        if step in ("make", "region"):
            await show_selection(m, s)
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
    if not s or s["step"] != "preview" or len(s["data"].get("photos", [])) < 5:
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
                description, f"{d['price']} {d['currency']}", d["city"], d["contact"], d["photos"][0][0]
            )
        target = GROUPS[d["category"]]
        try:
            post_kwargs = {
                "caption": render_ad(d, m.from_user.id, m.from_user.username, m.from_user.first_name, ad_id),
                "parse_mode": "HTML",
            }
            media = [InputMediaDocument(media=fid) if kind == "document" else InputMediaPhoto(media=fid)
                     for fid, kind in d["photos"]]
            media[0].caption = post_kwargs["caption"]
            media[0].parse_mode = "HTML"
            await bot.send_media_group(target, media=media)
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
    if step in ("make", "model", "region"):
        await m.answer("👇 Обери варіант кнопкою в повідомленні вище.", reply_markup=menu)
        return
    if step == "custom_make":
        if not value or len(value) > 70:
            await m.answer("✍️ Введи назву марки (до 70 символів).")
            return
        d["make"] = value
        s["step"] = "custom_model"
        await m.answer("✍️ Введи назву моделі:")
        return
    if step == "custom_model":
        if not value or len(value) > 80:
            await m.answer("✍️ Введи модель (до 80 символів).")
            return
        d["model"] = value
        d["title"] = d["make"] + " " + value
        await ask_next(m, s)
        return
    if step == "photo":
        if m.photo:
            photo = (m.photo[-1].file_id, "photo")
        elif m.document and (m.document.mime_type or "").startswith("image/"):
            photo = (m.document.file_id, "document")
        else:
            await m.answer("📸 Надішли фото (не менше 5).")
            return
        photos = d.setdefault("photos", [])
        if photos and photo[1] != photos[0][1]:
            await m.answer("📸 Надішли всі фотографії одним способом: як фото або як файли. Не змішуй формати.")
            return
        if len(photos) < 10 and photo[0] not in {p[0] for p in photos}:
            photos.append(photo)
        if len(photos) < 5:
            await m.answer(f"📸 Отримано {len(photos)} із мінімум 5 фото. Додай ще {5-len(photos)}.")
            return
        # If an album has more than five images, defer confirmation until the user
        # finishes sending them, or until a separate confirmation message.
        if m.media_group_id:
            s["step"] = "photo_more"
            await m.answer(f"📸 Отримано {len(photos)} фото. Можеш додати до 10. Натисни «✅ Фото готові», коли завершиш.",
                           reply_markup=PHOTO_DONE)
            return
        d["photo"] = True
        await ask_next(m, s)
        return
    if step == "photo_more":
        if m.photo or (m.document and (m.document.mime_type or "").startswith("image/")):
            photo = (m.photo[-1].file_id, "photo") if m.photo else (m.document.file_id, "document")
            photos = d.setdefault("photos", [])
            if photos and photo[1] != photos[0][1]:
                await m.answer("📸 Не змішуй звичайні фото та файли в одному оголошенні.")
                return
            if len(photos) < 10 and photo[0] not in {p[0] for p in photos}:
                photos.append(photo)
            if len(photos) == 10:
                d["photo"] = True
                await ask_next(m, s)
            return
        if value == "✅ Фото готові":
            if len(d.get("photos", [])) < 5:
                await m.answer("📸 Потрібно хоча б 5 фотографій.")
                return
            d["photo"] = True
            await ask_next(m, s)
            return
        await m.answer("📸 Додай фото або натисни «✅ Фото готові».", reply_markup=PHOTO_DONE)
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
