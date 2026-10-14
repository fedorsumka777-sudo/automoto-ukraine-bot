import os
import logging
import html
import re
import json
import asyncio
import urllib.request
from contextlib import asynccontextmanager

import asyncpg
from catalog import REGIONS, OTHER, keyboard, makes, models, matches, settlements, PLACES
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, Update, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, InputMediaPhoto, InputMediaDocument, BotCommand, BotCommandScopeChat
from fastapi import FastAPI, Request, HTTPException

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["BOT_TOKEN"]
DB_URL = os.environ["DATABASE_URL"]
PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")
SECRET = os.environ["WEBHOOK_SECRET"]
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")
CHANNEL_AUTOMOTO = os.getenv("CHANNEL_AUTOMOTO", "").strip()
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
    await bot.pin_chat_message(chat_id, notice.message_id, disable_notification=True)


async def track_update(update):
    msg = update.message
    cb = update.callback_query
    user = msg.from_user if msg else (cb.from_user if cb else None)
    if not user or user.is_bot:
        return
    chat = msg.chat if msg else (cb.message.chat if cb and cb.message else None)
    if not chat or chat.type not in ("private", "group", "supergroup"):
        return
    if chat.type != "private" and chat.username and ("@" + chat.username).lower() not in [g.lower() for g in GROUPS.values()]:
        return
    event = "message"
    if msg and chat.type == "private":
        label = msg.text or ""
        event = {"🚘 Створити оголошення":"create","🔎 Пошук":"search",
                 "📋 Мої оголошення":"my_ads"}.get(label,
                 "start" if label.startswith("/start") else "photo" if msg.photo else "message")
    elif cb:
        data = cb.data or ""
        event = ("edit" if data.startswith(("editad:", "editfield:")) else
                 "view" if data.startswith(("myad:", "found:")) else "button")
    async with db.acquire() as con:
        if chat.type == "private":
            await con.execute("""INSERT INTO bot_users(user_id,username) VALUES($1,$2)
              ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,last_seen=now()""",
              user.id,user.username)
        await con.execute("INSERT INTO bot_activity(user_id,chat_id,scope,event) VALUES($1,$2,$3,$4)",
                          user.id,chat.id,chat.type,event)


@dp.message(F.text == "/admin")
async def show_admin(m: Message):
    if m.chat.type != "private" or not ADMIN_USER_ID or m.from_user.id != ADMIN_USER_ID:
        return
    async with db.acquire() as con:
        total = await con.fetchval("SELECT count(*) FROM bot_users")
        fresh = await con.fetchval("SELECT count(*) FROM bot_users WHERE first_seen>=now()-interval '7 days'")
        active = await con.fetchval("SELECT count(DISTINCT user_id) FROM bot_activity WHERE scope='private' AND created_at>=now()-interval '7 days'")
        actions = await con.fetch("""SELECT event,count(*) n FROM bot_activity WHERE scope='private'
               AND created_at>=now()-interval '7 days' GROUP BY event ORDER BY n DESC LIMIT 6""")
        ads = await con.fetchval("SELECT count(*) FROM ads")
    labels = {"create":"Створювали оголошення","search":"Шукали","my_ads":"Мої оголошення",
              "view":"Переглядали","edit":"Редагували","photo":"Надсилали фото",
              "start":"Запускали","message":"Відповідали","button":"Натискали кнопки"}
    lines = ["🛡 АДМІНКА | АВТО МОТО УКРАЇНА", "",
             f"👤 Користувачі бота: {total}", f"🆕 Нові за 7 днів: {fresh}",
             f"⚡ Активні за 7 днів: {active}",f"📋 Оголошень у базі: {ads}",
             "", "📊 Дії за 7 днів:"]
    lines.extend(f"• {labels.get(r['event'],r['event'])}: {r['n']}" for r in actions)
    lines += ["", "👥 Групи:"]
    for name,group in GROUPS.items():
        try:
            count = await bot.get_chat_member_count(group)
            chat = await bot.get_chat(group)
            async with db.acquire() as con:
                row = await con.fetchrow("""SELECT count(*) messages,count(DISTINCT user_id) users
                   FROM bot_activity WHERE chat_id=$1 AND scope<>'private'
                   AND created_at>=now()-interval '7 days'""",chat.id)
            lines.append(f"{name}: {count} учасників, {row['users']} активних, {row['messages']} повідомлень/7 днів")
        except Exception:
            lines.append(f"{name}: немає доступу до статистики")
    lines.append("\nℹ️ Дані активності збираються з моменту оновлення. Для підрахунку всіх повідомлень потрібен вимкнений Privacy Mode.")
    await m.answer("\n".join(lines),reply_markup=menu)


@dp.message(F.text.startswith("/pinbot"))
async def pin_bot_command(m: Message):
    if m.chat.type not in ("group", "supergroup") or not m.from_user:
        return
    member = await bot.get_chat_member(m.chat.id, m.from_user.id)
    if str(member.status) not in ("administrator", "creator"):
        return
    try:
        await pin_bot_entry(m.chat.id)
        await m.answer("✅ Повідомлення бота опубліковано. Перевір закріплені повідомлення.")
    except Exception:
        logging.exception("Unable to publish bot entry in group %s", m.chat.id)
        await m.answer("⚠️ Не вдалося створити повідомлення. Перевір права бота й журнал Render.")


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
        previous = s["step"]
        s["step"] = "custom_make" if previous == "make" else "custom_model"
        try:
            await c.message.edit_text("✅ Обрано ручне введення марки" if previous == "make" else "✅ Обрано ручне введення моделі")
        except Exception:
            pass
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
        await c.message.edit_text("✅ Обрано: " + chosen)
    except Exception:
        pass


WELCOME_TEXT = (
    "🇺🇦 ВІТАЄМО В АВТО МОТО УКРАЇНА! 🚘\n\n"
    "Твій безкоштовний торговельний майданчик для автомобілів, "
    "мотоциклів, сільгосптехніки та запчастин!\n\n"
    "🔥 Продавай без посередників.\n"
    "🤝 Знаходь покупців з усієї України.\n"
    "🔎 Шукай вигідні пропозиції.\n"
    "📸 Створюй оголошення з фотографіями за кілька хвилин.\n\n"
    "💚 Жодної плати за розміщення!\n\n"
    "📢 НАШІ ГРУПИ:\n"
    "🚗 Авторинок — https://t.me/AutoMotoAvtorunok\n"
    "🚜 Агромаркет — https://t.me/AutoMotoAgromarket\n"
    "⚙️ Запчастини — https://t.me/AutoMotoZapchastunu\n\n"
    "👇 Обирай потрібний розділ і починай!"
)


async def send_welcome(m):
    await m.answer(WELCOME_TEXT, reply_markup=menu)


@dp.message(CommandStart())
async def start(m: Message):
    if not m.from_user:
        return
    if m.chat.type != "private":
        return
    sessions.pop(m.from_user.id, None)
    await send_welcome(m)


@dp.message(F.text == "/help")
async def help_command(m: Message):
    if m.chat.type == "private":
        await send_welcome(m)


@dp.message(F.text == "❌ Скасувати")
async def cancel(m: Message):
    if m.from_user:
        sessions.pop(m.from_user.id, None)
    await m.answer("Скасовано.", reply_markup=menu)


@dp.message(F.text == "🚘 Створити оголошення")
async def new_ad(m: Message):
    if not m.from_user:
        return
    sessions[m.from_user.id] = {"step": "create_mode", "data": {}}
    await m.answer("🚘 Як створимо оголошення?\n\n📝 Покроково — відповідай на запитання.\n🤖 Штучний інтелект — введи довільний опис одним повідомленням.",
                   reply_markup=CREATE_MODE)



def ad_buttons(rows, prefix):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"№{r['id']} — {r['title'][:32]} | {r['price']}",
                              callback_data=f"{prefix}:{r['id']}")] for r in rows
    ])


@dp.message(F.text == "🔎 Пошук")
async def search(m: Message):
    if not m.from_user or m.chat.type != "private":
        return
    sessions[m.from_user.id] = {"step": "search_filter", "data": {}}
    options = [[InlineKeyboardButton(text="🌐 Усі категорії", callback_data="filter:all")]]
    options.extend([[InlineKeyboardButton(text=kind, callback_data=f"filter:{i}")]
                    for i, kind in enumerate(GROUPS)])
    await m.answer("🔎 Де шукатимемо оголошення? Обери категорію:",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=options))


@dp.callback_query(F.data.startswith("filter:"))
async def choose_search_category(c: CallbackQuery):
    s = sessions.get(c.from_user.id)
    if not s or s["step"] != "search_filter":
        await c.answer("Пошук уже завершено.")
        return
    index = c.data.split(":", 1)[1]
    if index == "all":
        s["data"]["category"] = None
        selected = "усіх категоріях"
    elif index.isdigit() and int(index) < len(GROUPS):
        selected = list(GROUPS)[int(index)]
        s["data"]["category"] = selected
    else:
        await c.answer("Неправильна категорія")
        return
    s["step"] = "search"
    await c.message.edit_text(f"✅ Пошук у {selected}.")
    await c.message.answer("🔎 Напиши будь-яке слово або кілька слів для пошуку.", reply_markup=menu)
    await c.answer()


def stored_ad_caption(r):
    return (f"<b>{html.escape(r['category'].upper())}</b>\n"
            f"<b>{html.escape(r['title'])}</b>\n"
            f"📝 {html.escape(r['description'])}\n"
            f"💰 <b>{html.escape(r['price'])}</b>\n"
            f"📍 {html.escape(r['city'])}\n"
            f"☎️ {html.escape(r['contact'])}\n🔖 №{r['id']}")


@dp.callback_query(F.data.startswith("found:"))
async def found_ad(c: CallbackQuery):
    try:
        ad_id = int(c.data.split(":")[1])
    except (ValueError, IndexError):
        await c.answer("Помилка")
        return
    async with db.acquire() as con:
        row = await con.fetchrow("SELECT * FROM ads WHERE id=$1 AND status='active'", ad_id)
    if not row:
        await c.answer("Не знайдено", show_alert=True)
        return
    await c.answer()
    await show_ad_details(c.message, row)


@dp.message(F.text == "📋 Мої оголошення")
async def mine(m: Message):
    if not m.from_user or m.chat.type != "private":
        return
    async with db.acquire() as con:
        rows = await con.fetch(
            "SELECT id,title,price,city FROM ads WHERE user_id=$1 ORDER BY id DESC LIMIT 30",
            m.from_user.id)
    if not rows:
        await m.answer("Поки немає оголошень.", reply_markup=menu)
    else:
        await m.answer("📋 Натисни на своє оголошення, щоб переглянути або відредагувати:",
                       reply_markup=ad_buttons(rows, "myad"))


async def show_ad_details(m, row, mine=False):
    title = html.escape(row["title"])
    category = html.escape(row["category"])
    desc = html.escape(row["description"])
    price = html.escape(row["price"])
    city = html.escape(row["city"])
    contact = html.escape(row["contact"])
    content = f"<b>{category}</b>\n<b>{title}</b>\n\n📝 {desc}\n💰 {price}\n📍 {city}\n☎️ {contact}\n🔖 №{row['id']}"
    controls = []
    if mine:
        controls.append([InlineKeyboardButton(text="✏️ Редагувати", callback_data=f"editad:{row['id']}")])
    if row["group_chat_id"] and row["group_message_id"]:
        gid = str(row["group_chat_id"])
        if gid.startswith("-100"):
            controls.append([InlineKeyboardButton(text="🔗 Відкрити в групі",
                url=f"https://t.me/c/{gid[4:]}/{row['group_message_id']}")])
    await m.answer(content, parse_mode="HTML",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=controls) if controls else menu)


@dp.callback_query(F.data.startswith("myad:"))
async def open_my_ad(c: CallbackQuery):
    try:
        ad_id = int(c.data.split(":")[1])
    except (ValueError, IndexError):
        await c.answer("Некоректний номер")
        return
    async with db.acquire() as con:
        row = await con.fetchrow("SELECT * FROM ads WHERE id=$1 AND user_id=$2", ad_id, c.from_user.id)
    if not row:
        await c.answer("Оголошення не знайдено", show_alert=True)
        return
    await c.answer()
    await show_ad_details(c.message, row, mine=True)


@dp.callback_query(F.data.startswith("editad:"))
async def edit_ad(c: CallbackQuery):
    try:
        ad_id = int(c.data.split(":")[1])
    except (ValueError, IndexError):
        await c.answer("Некоректний номер")
        return
    async with db.acquire() as con:
        row = await con.fetchrow("SELECT id FROM ads WHERE id=$1 AND user_id=$2", ad_id, c.from_user.id)
    if not row:
        await c.answer("Немає доступу до цього оголошення", show_alert=True)
        return
    sessions[c.from_user.id] = {"step": "edit_choose", "data": {"ad_id": ad_id}}
    fields = [("🏷️ Назва", "title"), ("📝 Опис", "description"),
              ("💰 Ціна", "price"), ("📍 Місто", "city"), ("☎️ Контакт", "contact")]
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"editfield:{field}")] for label, field in fields
    ])
    await c.message.answer("✏️ Що потрібно змінити?", reply_markup=markup)
    await c.answer()


@dp.callback_query(F.data.startswith("editfield:"))
async def choose_edit_field(c: CallbackQuery):
    s = sessions.get(c.from_user.id)
    field = c.data.split(":", 1)[1]
    allowed = ("title", "description", "price", "city", "contact")
    if not s or s["step"] != "edit_choose" or field not in allowed:
        await c.answer("Редагування неактуальне")
        return
    s["step"] = "edit_value"
    s["data"]["field"] = field
    await c.message.answer("✍️ Надішли нове значення для поля «" + field + "»:", reply_markup=menu)
    await c.answer()



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
CREATE_MODE = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="📝 Покрокове створення")],
    [KeyboardButton(text="🤖 Створити за допомогою штучного інтелекту")],
    [KeyboardButton(text="❌ Скасувати")],
], resize_keyboard=True)
AI_CHOICE = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="✨ Покращити опис з Луною")],
    [KeyboardButton(text="➡️ Залишити мій опис")],
    [KeyboardButton(text="❌ Скасувати")],
], resize_keyboard=True)
AI_CONFIRM = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="✅ Використати опис Луни")],
    [KeyboardButton(text="🔄 Спробувати ще раз"), KeyboardButton(text="➡️ Залишити мій опис")],
    [KeyboardButton(text="❌ Скасувати")],
], resize_keyboard=True)
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




def parse_luna_listing(raw):
    """Extract only information explicitly supplied by the seller."""
    schema = {
        "model": AI_MODEL, "temperature": 0.1, "max_tokens": 650,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role":"system","content":
                "Витягни дані з неструктурованого тексту продавця українською. "
                "Поверни ТІЛЬКИ JSON з полями category,title,make,model,year,spec,description,"
                "price,currency,region,city,contact. Категорія точно одна з "
                "'🚗 Авторинок', '🚜 Агромаркет', '⚙️ Запчастини' або порожня. "
                "year, spec - дослівні характеристики (для авторинку spec = пробіг у км; "
                "для агро spec = тип і стан; для запчастин year = сумісність, spec = стан). "
                "price — тільки число без символу валюти, currency тільки 'грн' або 'USD'. "
                "region — лише коли прямо названа область; city — лише коли прямо названий населений пункт. "
                "description — короткий природний опис лише за відомими фактами, до 280 символів. "
                "НЕ вигадуй відсутніх даних, не виводь припущень про стан, пробіг, документи тощо. "
                "Невідомі поля — порожній рядок."},
            {"role":"user","content":raw[:2200]},
        ]
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(schema,ensure_ascii=False).encode("utf-8"),
        headers={"Authorization":"Bearer "+OPENAI_API_KEY,"Content-Type":"application/json"},
        method="POST")
    with urllib.request.urlopen(req,timeout=35) as response:
        parsed = json.load(response)
    obj = json.loads(parsed["choices"][0]["message"]["content"])
    if not isinstance(obj,dict):
        raise ValueError("Invalid listing")
    out = {k:str(obj.get(k) or "").strip() for k in (
        "category","title","make","model","year","spec","description",
        "price","currency","region","city","contact")}
    if out["category"] not in GROUPS:
        out["category"] = ""
    if out["region"] not in REGIONS:
        normalized = out["region"].casefold().replace(" область", "").replace(" обл.", "").strip()
        out["region"] = next((r for r in REGIONS if r.casefold().replace(" область","").strip() == normalized), "")
    if out["currency"] not in ("грн","USD"):
        out["currency"] = ""
    if not re.fullmatch(r"\\d{1,10}(?:[.,]\\d{1,2})?",out["price"]):
        out["price"] = ""
    return out


async def fill_from_luna(m,s):
    if not OPENAI_API_KEY:
        await m.answer("⚠️ AI-ключ не налаштований. Перейдемо до покрокового створення.",reply_markup=menu)
        s["step"]="category"
        await m.answer("🗂️ Обери категорію:",reply_markup=categories)
        return
    await m.answer("🤖 Луна аналізує твій опис...")
    try:
        extracted = await asyncio.wait_for(asyncio.to_thread(parse_luna_listing,s["raw"]),timeout=40)
    except Exception:
        logging.exception("Luna extraction failed")
        await m.answer("⚠️ Луна зараз недоступна. Обери категорію і продовжимо вручну.")
        s["step"]="category"
        await m.answer("🗂️ Обери категорію:",reply_markup=categories)
        return
    d=s["data"]
    d.update({k:v for k,v in extracted.items() if v})
    if d.get("make") and d.get("model"):
        d["title"] = d["make"] + " " + d["model"]
    elif d.get("category") != "⚙️ Запчастини":
        # Ask for full vehicle choice instead of producing an incomplete title.
        d.pop("make", None)
        d.pop("model", None)
        d.pop("title", None)
    if d.get("region"):
        d["region_code"]=REGIONS[d["region"]]
    # Do not put a synthesized title in place of a missing vehicle make/model.
    if d.get("category") != "⚙️ Запчастини" and (not d.get("make") or not d.get("model")):
        d.pop("title",None)
    if d.get("category") == "⚙️ Запчастини" and not d.get("title") and d.get("make"):
        d["title"] = (d["make"] + " " + d.get("model", "")).strip()
    preview = "\n".join(f"• {k}: {v}" for k,v in extracted.items() if v and k not in ("make","model"))
    await m.answer("✅ Луна розібрала текст:\n"+(preview or "Потрібні уточнення.")+
                   "\n\nЗараз запитаю лише те, чого бракує.",reply_markup=menu)
    if not d.get("category"):
        s["step"]="category"
        await m.answer("🗂️ Обери категорію:",reply_markup=categories)
    else:
        await ask_next(m,s)


def request_luna(data):
    """Synchronous call executed in a worker thread; no API key or prompt logging."""
    context = {
        "category": data.get("category"), "make": data.get("make"),
        "model": data.get("model"), "title": data.get("title"),
        "year": data.get("year"), "spec": data.get("spec"),
        "seller_description": data.get("description"),
    }
    payload = {
        "model": AI_MODEL,
        "max_tokens": 260,
        "temperature": 0.45,
        "messages": [
            {"role": "system", "content":
                "Ти Луна, українська помічниця для приватних оголошень про транспорт і техніку. "
                "Створи лише один лаконічний, живий, грамотний опис українською (до 400 символів). "
                "Спирайся ЛИШЕ на факти, які надав продавець. "
                "Не вигадуй технічний стан, комплектацію, пробіг, документи, ремонт, переваги, гарантії, ДТП або історію. "
                "Не додавай ціну, контакти, населений пункт, заголовок, хештеги чи зайвих емодзі. "
                "Якщо мало інформації, просто лаконічно перефразуй її."},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)}
        ],
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + OPENAI_API_KEY,
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        body = json.load(response)
    return body["choices"][0]["message"]["content"].strip()[:400]


async def propose_luna(m, s):
    if not OPENAI_API_KEY:
        await m.answer("⚠️ Луна ще не підключена до API. Залишимо твій опис.")
        s["data"]["ai_done"] = True
        await ask_next(m, s)
        return
    await m.answer("✨ Луна готує варіант опису на основі твоїх даних...")
    try:
        suggestion = await asyncio.wait_for(asyncio.to_thread(request_luna, dict(s["data"])), timeout=35)
        if not suggestion:
            raise ValueError("Empty AI description")
    except Exception:
        logging.exception("Luna request failed")
        await m.answer("⚠️ Зараз Луна недоступна. Твій опис збережено, продовжуємо без AI.")
        s["data"]["ai_done"] = True
        await ask_next(m, s)
        return
    s["data"]["ai_suggestion"] = suggestion
    s["step"] = "ai_review"
    await m.answer("✨ Луна пропонує:\n\n" + suggestion +
                   "\n\nПеревір факти перед публікацією.", reply_markup=AI_CONFIRM)


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
            reply = AI_CHOICE if step == "ai_done" else CURRENCY if step == "currency" else menu
            await m.answer(prompt, reply_markup=reply)


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
        caption = render_ad(d, m.from_user.id, m.from_user.username, m.from_user.first_name, ad_id)

        def make_album():
            # Telegram media objects cannot be reused across send requests.
            items = [InputMediaDocument(media=fid) if kind == "document"
                     else InputMediaPhoto(media=fid) for fid, kind in d["photos"]]
            items[0].caption = caption
            items[0].parse_mode = "HTML"
            return items

        results = []
        try:
            posted = await bot.send_media_group(target, media=make_album())
            async with db.acquire() as con:
                await con.execute(
                    "UPDATE ads SET group_chat_id=$1,group_message_id=$2 WHERE id=$3",
                    posted[0].chat.id, posted[0].message_id, ad_id)
            results.append("✅ У вибраній групі опубліковано.")
        except Exception:
            logging.exception("Group publishing failed for ad %s", ad_id)
            results.append("⚠️ Не вдалося опублікувати у групі.")

        if CHANNEL_AUTOMOTO:
            try:
                # Channel publishing is independent of the selected group.
                channel_posted = await bot.send_media_group(CHANNEL_AUTOMOTO, media=make_album())
                async with db.acquire() as con:
                    await con.execute(
                        "UPDATE ads SET channel_chat_id=$1,channel_message_id=$2 WHERE id=$3",
                        channel_posted[0].chat.id, channel_posted[0].message_id, ad_id)
                results.append("✅ У спільному каналі опубліковано.")
            except Exception:
                logging.exception("Channel publishing failed for ad %s", ad_id)
                results.append("⚠️ Не вдалося опублікувати в каналі. Перевір права бота.")
        else:
            results.append("⚠️ Канал ще не налаштований (CHANNEL_AUTOMOTO).")

        status = "\n".join(results)
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
        await send_welcome(m)
        return
    step, d = s["step"], s["data"]
    value = (m.text or "").strip()
    if step == "publishing":
        await m.answer("Зачекай, оголошення публікується.")
        return
    if step == "preview":
        await m.answer("Обери «✅ Опублікувати» або «✏️ Почати заново».", reply_markup=CONFIRM)
        return
    if step == "create_mode":
        if value == "📝 Покрокове створення":
            s["step"]="category"
            await m.answer("🗂️ Обери категорію:",reply_markup=categories)
        elif value == "🤖 Описати все Луні":
            s["step"]="luna_raw"
            await m.answer("🤖 Напиши все одним текстом: що продаєш, марка/модель, рік, "
                           "стан, пробіг, ціна, місто та контакт. Можна писати як зручно.",
                           reply_markup=menu)
        else:
            await m.answer("Обери спосіб створення кнопкою.",reply_markup=CREATE_MODE)
        return
    if step == "luna_raw":
        if len(value) < 15 or len(value)>2200:
            await m.answer("Опиши товар трохи докладніше (15–2200 символів).")
            return
        s["raw"]=value
        s["step"]="luna_processing"
        await fill_from_luna(m,s)
        return
    if step == "luna_processing":
        await m.answer("🤖 Луна ще опрацьовує опис.")
        return
    if step == "search_filter":
        await m.answer("👇 Обери категорію кнопкою вище.")
        return
    if step == "edit_choose":
        await m.answer("👇 Обери поле кнопкою вище.")
        return
    if step == "edit_value":
        field, ad_id = d["field"], d["ad_id"]
        if not value or len(value) > (500 if field == "description" else 120):
            await m.answer("Введи непорожнє значення (опис до 500 символів).")
            return
        async with db.acquire() as con:
            row = await con.fetchrow(
                "UPDATE ads SET " + field + "=$1 WHERE id=$2 AND user_id=$3 RETURNING *",
                value, ad_id, m.from_user.id)
        sessions.pop(m.from_user.id, None)
        if row:
            warning = ""
            if row["group_chat_id"] and row["group_message_id"]:
                try:
                    await bot.edit_message_caption(
                        chat_id=row["group_chat_id"], message_id=row["group_message_id"],
                        caption=stored_ad_caption(row), parse_mode="HTML")
                except Exception:
                    logging.exception("Could not synchronize edit to group")
                    warning = "\n⚠️ Зміна в базі збережена, але в групі оновити текст не вдалося."
            else:
                warning = "\nℹ️ Для старого оголошення немає збереженого ID повідомлення в групі. Відредагована картка доступна в боті."
            await m.answer("✅ Зміни збережені." + warning, reply_markup=menu)
            await show_ad_details(m, row, mine=True)
        else:
            await m.answer("Оголошення більше не знайдено.", reply_markup=menu)
        return
    if step == "search":
        if len(value) < 2 or len(value) > 100:
            await m.answer("Введи від 2 до 100 символів.")
            return
        pattern = "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        category = d.get("category")
        async with db.acquire() as con:
            rows = await con.fetch(
                """SELECT id,category,title,price,city FROM ads
                WHERE status='active' AND ($1::text IS NULL OR category=$1)
                AND (title ILIKE $2 ESCAPE '\\' OR description ILIKE $2 ESCAPE '\\'
                     OR city ILIKE $2 ESCAPE '\\')
                ORDER BY id DESC LIMIT 30""", category, pattern)
        sessions.pop(m.from_user.id, None)
        if rows:
            await m.answer(f"🔎 Знайдено {len(rows)} оголошень. Обери потрібне:",
                           reply_markup=ad_buttons(rows, "found"))
        else:
            await m.answer("За запитом нічого не знайдено.", reply_markup=menu)
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
    if step in ("photo", "photo_more"):
        if value == "✅ Фото готові":
            if len(d.get("photos", [])) < 5:
                await m.answer(f"📸 Додано {len(d.get('photos', []))} фото. Потрібно мінімум 5.", reply_markup=PHOTO_DONE)
                return
            d["photo"] = True
            await ask_next(m, s)
            return
        if m.photo:
            photo = (m.photo[-1].file_id, "photo")
        elif m.document and (m.document.mime_type or "").startswith("image/"):
            photo = (m.document.file_id, "document")
        else:
            await m.answer("📸 Надішли від 5 до 10 фото й натисни «✅ Фото готові».", reply_markup=PHOTO_DONE)
            return
        photos = d.setdefault("photos", [])
        if photos and photo[1] != photos[0][1]:
            await m.answer("📸 Не змішуй фото та документи. Надішли всі зображення одним способом.")
            return
        if len(photos) < 10 and photo[0] not in {p[0] for p in photos}:
            photos.append(photo)
        s["step"] = "photo_more"
        # Telegram albums arrive in separate updates: never finalize early.
        if not m.media_group_id or len(photos) == 10:
            await m.answer(f"📸 Додано {len(photos)} фото з 5–10. "
                           + ("✅ Можеш завершити." if len(photos)>=5 else f"Додай ще {5-len(photos)}."),
                           reply_markup=PHOTO_DONE)
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
        await con.execute("ALTER TABLE ads ADD COLUMN IF NOT EXISTS group_chat_id BIGINT")
        await con.execute("ALTER TABLE ads ADD COLUMN IF NOT EXISTS group_message_id BIGINT")
        await con.execute("ALTER TABLE ads ADD COLUMN IF NOT EXISTS channel_chat_id BIGINT")
        await con.execute("ALTER TABLE ads ADD COLUMN IF NOT EXISTS channel_message_id BIGINT")
        await con.execute("""CREATE TABLE IF NOT EXISTS bot_users(
            user_id BIGINT PRIMARY KEY, username TEXT, first_seen TIMESTAMPTZ DEFAULT now(),
            last_seen TIMESTAMPTZ DEFAULT now())""")
        await con.execute("""CREATE TABLE IF NOT EXISTS bot_activity(
            id BIGSERIAL PRIMARY KEY, user_id BIGINT, chat_id BIGINT, scope TEXT,
            event TEXT, created_at TIMESTAMPTZ DEFAULT now())""")
        await con.execute("CREATE INDEX IF NOT EXISTS bot_activity_date_idx ON bot_activity(created_at)")
        await con.execute("CREATE INDEX IF NOT EXISTS bot_activity_group_idx ON bot_activity(chat_id,created_at)")
    await bot.set_my_commands([BotCommand(command="start", description="🚀 Головне меню"),
                               BotCommand(command="help", description="ℹ️ Як користуватися ботом")])
    if ADMIN_USER_ID:
        await bot.set_my_commands([BotCommand(command="start",description="🚀 Головне меню"),
                                   BotCommand(command="admin",description="🛡 Статистика")],
                                  scope=BotCommandScopeChat(chat_id=ADMIN_USER_ID))
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
    try:
        await track_update(update)
    except Exception:
        logging.exception("Activity tracking error")
    await dp.feed_update(bot, update)
    return {"ok": True}
