import os
import sqlite3
import asyncio
import calendar
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "fitness_bot.db")
TZ = ZoneInfo(os.getenv("BOT_TZ", "Europe/Kyiv"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден. Добавь его в .env или в Render Environment Variables.")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
user_states = {}


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now():
    return datetime.now(TZ)


def today_iso():
    return now().date().isoformat()


def now_iso():
    return now().isoformat()


def month_key(d=None):
    d = d or now().date()
    return f"{d.year:04d}-{d.month:02d}"


def month_bounds(d=None):
    d = d or now().date()
    start = d.replace(day=1)
    end = d.replace(day=calendar.monthrange(d.year, d.month)[1])
    return start, end


def previous_month_bounds(d=None):
    d = d or now().date()
    prev_last = d.replace(day=1) - timedelta(days=1)
    return month_bounds(prev_last)


def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            start_weight REAL,
            current_weight REAL,
            target_weight REAL,
            height_cm REAL,
            age INTEGER,
            gender TEXT,
            activity_level TEXT,
            daily_calorie_goal INTEGER,
            daily_reminder_time TEXT DEFAULT '20:00',
            is_registered INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            report_date TEXT NOT NULL,
            food_calories INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(telegram_id, report_date)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS workouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            workout_date TEXT NOT NULL,
            workout_type TEXT NOT NULL,
            calories_burned INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS weight_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            weight_date TEXT NOT NULL,
            weight REAL NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS body_measurements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            measurement_date TEXT NOT NULL,
            neck_cm REAL,
            chest_cm REAL,
            belly_cm REAL,
            hips_cm REAL,
            thigh_cm REAL,
            calves_cm REAL,
            forearms_cm REAL,
            created_at TEXT NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS weekly_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            week_start TEXT NOT NULL,
            week_end TEXT NOT NULL,
            total_food_calories INTEGER NOT NULL,
            total_workout_calories INTEGER NOT NULL,
            net_calories INTEGER NOT NULL,
            summary_text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(telegram_id, week_start, week_end)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS monthly_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            report_month TEXT NOT NULL,
            total_food_calories INTEGER NOT NULL,
            avg_food_calories INTEGER NOT NULL,
            total_workout_calories INTEGER NOT NULL,
            avg_workout_calories INTEGER NOT NULL,
            net_calories INTEGER NOT NULL,
            avg_net_calories INTEGER NOT NULL,
            total_workouts INTEGER NOT NULL,
            double_training_days INTEGER NOT NULL,
            start_weight REAL,
            end_weight REAL,
            weight_delta REAL,
            goal_progress_percent REAL,
            report_text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(telegram_id, report_month)
        )
        """)


def ensure_user(tg_id):
    with db() as conn:
        row = conn.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (tg_id,)).fetchone()
        if not row:
            conn.execute("INSERT INTO users (telegram_id, created_at) VALUES (?, ?)", (tg_id, now_iso()))


def get_user(tg_id):
    ensure_user(tg_id)
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE telegram_id=?", (tg_id,)).fetchone()


def parse_float(text):
    return float(text.replace(",", ".").strip())


def parse_int(text):
    return int(round(parse_float(text)))


def bar(percent, size=10):
    percent = max(0, percent)
    filled = round(min(percent, 100) / 100 * size)
    return "█" * filled + "░" * (size - filled) + f" {percent:.0f}%"


def food_status(food, goal):
    p = food / goal * 100 if goal else 0
    if p <= 100:
        return "🟢 В пределах плана"
    if p <= 110:
        return "🟡 Небольшое превышение"
    return "🔴 Существенный перебор"


def workout_status(burned, target):
    p = burned / target * 100 if target else 0
    if p >= 100:
        return "🟢 Цель выполнена"
    if p >= 80:
        return "🟡 Почти выполнено"
    return "🔴 Ниже плана"


def goal_progress(user):
    start = user["start_weight"] or 0
    current = user["current_weight"] or start
    target = user["target_weight"] or 0
    total = start - target
    lost = start - current
    left = current - target
    percent = max(0, min(100, lost / total * 100)) if total > 0 else 0
    return total, lost, left, percent


def training_targets(user):
    weight = user["current_weight"] or user["start_weight"] or 100
    if weight >= 95:
        return 350, 500, 600, 900, 800, 1200
    if weight >= 85:
        return 300, 450, 500, 800, 700, 1000
    return 250, 400, 450, 700, 600, 900


def main_menu():
    kb = InlineKeyboardBuilder()
    items = [
        ("🔥 План на сегодня", "plan"),
        ("🍽 Внести калории", "food"),
        ("🏋️ Внести тренировку", "workout"),
        ("📊 Отчет за сегодня", "today"),
        ("🗂 История недель", "weeks"),
        ("📅 Отчет за месяц", "month"),
        ("🗂 История месяцев", "months"),
        ("⚖️ Внести вес", "weight"),
        ("📏 Замеры тела", "measure"),
        ("🎯 Моя цель", "goal"),
        ("😵 Хочу сорваться", "panic"),
        ("ℹ️ Помощь", "help"),
    ]
    for text, data in items:
        kb.button(text=text, callback_data=data)
    kb.adjust(2)
    return kb.as_markup()


def cancel_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="cancel")
    return kb.as_markup()


def gender_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="Мужской", callback_data="gender:male")
    kb.button(text="Женский", callback_data="gender:female")
    kb.adjust(2)
    return kb.as_markup()


def activity_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="Малоподвижный", callback_data="activity:sedentary")
    kb.button(text="Средний", callback_data="activity:medium")
    kb.button(text="Активный", callback_data="activity:active")
    kb.adjust(1)
    return kb.as_markup()


def workout_menu():
    kb = InlineKeyboardBuilder()
    items = ["Силовая", "Велотренажер", "Футбол", "Бег", "Ходьба", "Плавание", "Бокс", "Йога/растяжка", "Другое"]
    icons = ["🏋️", "🚴", "⚽", "🏃", "🚶", "🏊", "🥊", "🧘", "🔥"]
    for icon, item in zip(icons, items):
        kb.button(text=f"{icon} {item}", callback_data=f"workout_type:{item}")
    kb.adjust(2)
    return kb.as_markup()


async def ask_reg(message, tg_id):
    step = user_states[tg_id]["step"]
    prompts = {
        "start_weight": "1/7 Введи текущий вес, например: 101.3",
        "target_weight": "2/7 Введи целевой вес, например: 80",
        "height": "3/7 Введи рост в см, например: 183",
        "age": "4/7 Введи возраст, например: 33",
        "calories": "6/7 Введи дневной лимит калорий, например: 1600",
        "reminder": "7/7 Введи время ежедневного напоминания, например: 20:00",
    }
    if step == "gender":
        await message.answer("5/7 Выбери пол:", reply_markup=gender_menu())
    elif step == "activity":
        await message.answer("Выбери активность без тренировок:", reply_markup=activity_menu())
    else:
        await message.answer(prompts[step], reply_markup=cancel_menu())


@dp.message(CommandStart())
async def start(message: Message):
    tg_id = message.from_user.id
    ensure_user(tg_id)
    user = get_user(tg_id)
    if user["is_registered"]:
        await message.answer("Главное меню:", reply_markup=main_menu())
        return
    user_states[tg_id] = {"flow": "reg", "step": "start_weight", "data": {}}
    await message.answer("Привет! Настроим цель, калории, тренировки, вес и замеры.")
    await ask_reg(message, tg_id)


@dp.message(Command("menu"))
async def menu(message: Message):
    await message.answer("Главное меню:", reply_markup=main_menu())


@dp.callback_query(F.data == "cancel")
async def cancel(call: CallbackQuery):
    user_states.pop(call.from_user.id, None)
    await call.message.answer("Отменил.", reply_markup=main_menu())
    await call.answer()


@dp.callback_query(F.data.startswith("gender:"))
async def gender(call: CallbackQuery):
    st = user_states.get(call.from_user.id)
    if st and st.get("flow") == "reg":
        st["data"]["gender"] = call.data.split(":", 1)[1]
        st["step"] = "activity"
        await call.message.answer("Выбери активность без тренировок:", reply_markup=activity_menu())
    await call.answer()


@dp.callback_query(F.data.startswith("activity:"))
async def activity(call: CallbackQuery):
    st = user_states.get(call.from_user.id)
    if st and st.get("flow") == "reg":
        st["data"]["activity"] = call.data.split(":", 1)[1]
        st["step"] = "calories"
        await call.message.answer("6/7 Введи дневной лимит калорий, например: 1600", reply_markup=cancel_menu())
    await call.answer()


@dp.callback_query(F.data == "plan")
async def plan(call: CallbackQuery):
    user = get_user(call.from_user.id)
    mn1, mn2, id1, id2, db1, db2 = training_targets(user)
    total, lost, left, pct = goal_progress(user)
    await call.message.answer(
        f"🔥 <b>План на сегодня</b>\n\n"
        f"🍽 Лимит питания: <b>{user['daily_calorie_goal']} ккал</b>\n"
        f"📌 Минимум расхода: <b>{mn1}–{mn2} ккал</b>\n"
        f"🏆 Идеально сжечь: <b>{id1}–{id2} ккал</b>\n"
        f"🔥 2 тренировки: <b>{db1}–{db2} ккал</b>\n\n"
        f"🎯 Прогресс цели:\n{bar(pct)}\n"
        f"Сброшено: {lost:.1f} кг из {total:.1f} кг\n"
        f"Осталось: {left:.1f} кг"
    )
    await call.answer()


@dp.callback_query(F.data == "food")
async def food(call: CallbackQuery):
    user_states[call.from_user.id] = {"flow": "food"}
    await call.message.answer("🍽 Сколько калорий съел сегодня? Например: 1580", reply_markup=cancel_menu())
    await call.answer()


@dp.callback_query(F.data == "workout")
async def workout(call: CallbackQuery):
    user_states[call.from_user.id] = {"flow": "workout_type"}
    await call.message.answer("Выбери тип тренировки:", reply_markup=workout_menu())
    await call.answer()


@dp.callback_query(F.data.startswith("workout_type:"))
async def workout_type(call: CallbackQuery):
    wt = call.data.split(":", 1)[1]
    user_states[call.from_user.id] = {"flow": "workout_calories", "type": wt}
    await call.message.answer(f"🔥 Сколько калорий сжег на тренировке «{wt}»?", reply_markup=cancel_menu())
    await call.answer()


@dp.callback_query(F.data == "weight")
async def weight(call: CallbackQuery):
    user_states[call.from_user.id] = {"flow": "weight"}
    await call.message.answer("⚖️ Введи текущий вес, например: 98.4", reply_markup=cancel_menu())
    await call.answer()


@dp.callback_query(F.data == "measure")
async def measure(call: CallbackQuery):
    user_states[call.from_user.id] = {"flow": "measure", "step": "neck", "data": {}}
    await call.message.answer("📏 Введи обхват шеи в см:", reply_markup=cancel_menu())
    await call.answer()


@dp.callback_query(F.data == "goal")
async def goal(call: CallbackQuery):
    user = get_user(call.from_user.id)
    total, lost, left, pct = goal_progress(user)
    await call.message.answer(
        f"🎯 <b>Моя цель</b>\n\n"
        f"Старт: {user['start_weight']} кг\nСейчас: {user['current_weight']} кг\nЦель: {user['target_weight']} кг\n\n"
        f"{bar(pct)}\nСброшено: {lost:.1f} кг\nОсталось: {left:.1f} кг"
    )
    await call.answer()


@dp.callback_query(F.data == "panic")
async def panic(call: CallbackQuery):
    await call.message.answer("😵 Стоп. Выпей воды, подожди 10 минут и не ломай день полностью. Один сложный момент не отменяет прогресс.")
    await call.answer()


@dp.callback_query(F.data == "help")
async def help_cb(call: CallbackQuery):
    await call.message.answer("/start — регистрация\n/menu — меню\nКаждый день вноси калории и тренировки. В конце месяца внеси вес и замеры, затем сформируй отчет.")
    await call.answer()


@dp.callback_query(F.data == "today")
async def today(call: CallbackQuery):
    await send_today(call.from_user.id, call.message)
    await call.answer()


@dp.callback_query(F.data == "weeks")
async def weeks(call: CallbackQuery):
    with db() as conn:
        rows = conn.execute("SELECT summary_text FROM weekly_summaries WHERE telegram_id=? ORDER BY week_start DESC LIMIT 5", (call.from_user.id,)).fetchall()
    if not rows:
        await call.message.answer("Истории недель пока нет.")
    else:
        await call.message.answer("🗂 <b>История недель</b>\n\n" + "\n\n".join(r["summary_text"] for r in rows))
    await call.answer()


@dp.callback_query(F.data == "months")
async def months(call: CallbackQuery):
    with db() as conn:
        rows = conn.execute("SELECT report_text FROM monthly_reports WHERE telegram_id=? ORDER BY report_month DESC LIMIT 3", (call.from_user.id,)).fetchall()
    if not rows:
        await call.message.answer("Истории месяцев пока нет.")
    else:
        for r in rows:
            await call.message.answer(r["report_text"])
    await call.answer()


@dp.callback_query(F.data == "month")
async def month(call: CallbackQuery):
    await generate_month_report(call.from_user.id, call.message)
    await call.answer()


@dp.message()
async def text_handler(message: Message):
    tg_id = message.from_user.id
    ensure_user(tg_id)
    st = user_states.get(tg_id)
    if not st:
        await message.answer("Выбери действие в меню:", reply_markup=main_menu())
        return
    try:
        flow = st["flow"]
        text = message.text or ""
        if flow == "reg":
            await handle_reg(message, st, text)
        elif flow == "food":
            await handle_food(message, text)
        elif flow == "workout_calories":
            await handle_workout(message, st, text)
        elif flow == "weight":
            await handle_weight(message, text)
        elif flow == "measure":
            await handle_measure(message, st, text)
    except ValueError:
        await message.answer("Введи число. Например: 1600 или 98.4")


async def handle_reg(message, st, text):
    tg_id = message.from_user.id
    data = st["data"]
    step = st["step"]
    if step == "start_weight":
        data["start_weight"] = parse_float(text); st["step"] = "target_weight"
    elif step == "target_weight":
        data["target_weight"] = parse_float(text); st["step"] = "height"
    elif step == "height":
        data["height"] = parse_float(text); st["step"] = "age"
    elif step == "age":
        data["age"] = parse_int(text); st["step"] = "gender"; await ask_reg(message, tg_id); return
    elif step == "calories":
        data["calories"] = parse_int(text); st["step"] = "reminder"
    elif step == "reminder":
        data["reminder"] = text if ":" in text else "20:00"
        with db() as conn:
            conn.execute("""
            UPDATE users SET start_weight=?, current_weight=?, target_weight=?, height_cm=?, age=?, gender=?, activity_level=?, daily_calorie_goal=?, daily_reminder_time=?, is_registered=1 WHERE telegram_id=?
            """, (data["start_weight"], data["start_weight"], data["target_weight"], data["height"], data["age"], data["gender"], data["activity"], data["calories"], data["reminder"], tg_id))
            conn.execute("INSERT INTO weight_logs (telegram_id, weight_date, weight, created_at) VALUES (?, ?, ?, ?)", (tg_id, today_iso(), data["start_weight"], now_iso()))
        user_states.pop(tg_id, None)
        await message.answer("✅ Настройка завершена.", reply_markup=main_menu())
        return
    await ask_reg(message, tg_id)


async def handle_food(message, text):
    tg_id = message.from_user.id
    food = parse_int(text)
    with db() as conn:
        conn.execute("""
        INSERT INTO daily_reports (telegram_id, report_date, food_calories, created_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(telegram_id, report_date) DO UPDATE SET food_calories=excluded.food_calories
        """, (tg_id, today_iso(), food, now_iso()))
    user_states.pop(tg_id, None)
    user = get_user(tg_id)
    pct = food / user["daily_calorie_goal"] * 100
    await message.answer(f"✅ Калории сохранены.\n\n🍽 {food}/{user['daily_calorie_goal']} ккал\n{bar(pct)}\n{food_status(food, user['daily_calorie_goal'])}", reply_markup=main_menu())


async def handle_workout(message, st, text):
    tg_id = message.from_user.id
    cal = parse_int(text)
    wt = st["type"]
    with db() as conn:
        conn.execute("INSERT INTO workouts (telegram_id, workout_date, workout_type, calories_burned, created_at) VALUES (?, ?, ?, ?, ?)", (tg_id, today_iso(), wt, cal, now_iso()))
    user_states.pop(tg_id, None)
    user = get_user(tg_id)
    _, _, ideal, _, _, _ = training_targets(user)
    await message.answer(f"✅ Тренировка сохранена.\n\n🔥 {wt}: {cal} ккал\n{bar(cal / ideal * 100)}\n{workout_status(cal, ideal)}", reply_markup=main_menu())


async def handle_weight(message, text):
    tg_id = message.from_user.id
    weight = parse_float(text)
    with db() as conn:
        conn.execute("UPDATE users SET current_weight=? WHERE telegram_id=?", (weight, tg_id))
        conn.execute("INSERT INTO weight_logs (telegram_id, weight_date, weight, created_at) VALUES (?, ?, ?, ?)", (tg_id, today_iso(), weight, now_iso()))
    user_states.pop(tg_id, None)
    user = get_user(tg_id)
    total, lost, left, pct = goal_progress(user)
    await message.answer(f"✅ Вес сохранен: {weight:g} кг\n\n🎯 {bar(pct)}\nСброшено: {lost:.1f} кг\nОсталось: {left:.1f} кг", reply_markup=main_menu())


async def handle_measure(message, st, text):
    tg_id = message.from_user.id
    value = parse_float(text)
    steps = [("neck", "шеи"), ("chest", "груди"), ("belly", "живота"), ("hips", "таза"), ("thigh", "бедра"), ("calves", "икр"), ("forearms", "предплечья")]
    st["data"][st["step"]] = value
    idx = [x[0] for x in steps].index(st["step"])
    if idx < len(steps) - 1:
        st["step"] = steps[idx + 1][0]
        await message.answer(f"📏 Введи обхват {steps[idx+1][1]} в см:", reply_markup=cancel_menu())
        return
    d = st["data"]
    with db() as conn:
        conn.execute("""
        INSERT INTO body_measurements (telegram_id, measurement_date, neck_cm, chest_cm, belly_cm, hips_cm, thigh_cm, calves_cm, forearms_cm, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (tg_id, today_iso(), d["neck"], d["chest"], d["belly"], d["hips"], d["thigh"], d["calves"], d["forearms"], now_iso()))
    user_states.pop(tg_id, None)
    await message.answer("✅ Замеры сохранены.", reply_markup=main_menu())


async def send_today(tg_id, msg):
    user = get_user(tg_id)
    _, _, ideal, _, _, _ = training_targets(user)
    with db() as conn:
        r = conn.execute("SELECT * FROM daily_reports WHERE telegram_id=? AND report_date=?", (tg_id, today_iso())).fetchone()
        ws = conn.execute("SELECT * FROM workouts WHERE telegram_id=? AND workout_date=?", (tg_id, today_iso())).fetchall()
    food = r["food_calories"] if r else 0
    burned = sum(w["calories_burned"] for w in ws)
    net = food - burned
    lines = "\n".join(f"— {w['workout_type']}: {w['calories_burned']} ккал" for w in ws) or "— нет данных"
    await msg.answer(
        f"📊 <b>Отчет за сегодня</b>\n\n"
        f"🍽 {food}/{user['daily_calorie_goal']} ккал\n{bar(food / user['daily_calorie_goal'] * 100)}\n{food_status(food, user['daily_calorie_goal'])}\n\n"
        f"🔥 {burned}/{ideal} ккал\n{bar(burned / ideal * 100)}\n{workout_status(burned, ideal)}\n\n"
        f"⚡ Чистые калории: {net} ккал\n\n🏋️ Тренировки:\n{lines}"
    )


def week_summary_text(tg_id, start, end):
    with db() as conn:
        rs = conn.execute("SELECT food_calories FROM daily_reports WHERE telegram_id=? AND report_date BETWEEN ? AND ?", (tg_id, start.isoformat(), end.isoformat())).fetchall()
        ws = conn.execute("SELECT calories_burned FROM workouts WHERE telegram_id=? AND workout_date BETWEEN ? AND ?", (tg_id, start.isoformat(), end.isoformat())).fetchall()
    food = sum(r["food_calories"] for r in rs)
    burned = sum(w["calories_burned"] for w in ws)
    net = food - burned
    text = f"📌 <b>Итог недели</b>\n{start.isoformat()} — {end.isoformat()}\n\n🍽 Всего съедено: {food} ккал\n🔥 Всего сожжено: {burned} ккал\n⚡ Чистые калории: {net} ккал"
    with db() as conn:
        conn.execute("""
        INSERT INTO weekly_summaries (telegram_id, week_start, week_end, total_food_calories, total_workout_calories, net_calories, summary_text, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id, week_start, week_end) DO UPDATE SET total_food_calories=excluded.total_food_calories, total_workout_calories=excluded.total_workout_calories, net_calories=excluded.net_calories, summary_text=excluded.summary_text
        """, (tg_id, start.isoformat(), end.isoformat(), food, burned, net, text, now_iso()))
    return text


def month_stats(tg_id, start, end):
    with db() as conn:
        rs = conn.execute("SELECT * FROM daily_reports WHERE telegram_id=? AND report_date BETWEEN ? AND ? ORDER BY report_date", (tg_id, start.isoformat(), end.isoformat())).fetchall()
        ws = conn.execute("SELECT * FROM workouts WHERE telegram_id=? AND workout_date BETWEEN ? AND ? ORDER BY workout_date", (tg_id, start.isoformat(), end.isoformat())).fetchall()
        weights = conn.execute("SELECT * FROM weight_logs WHERE telegram_id=? AND weight_date BETWEEN ? AND ? ORDER BY weight_date", (tg_id, start.isoformat(), end.isoformat())).fetchall()
        ms = conn.execute("SELECT * FROM body_measurements WHERE telegram_id=? AND measurement_date BETWEEN ? AND ? ORDER BY measurement_date", (tg_id, start.isoformat(), end.isoformat())).fetchall()
    food = sum(r["food_calories"] for r in rs)
    burned = sum(w["calories_burned"] for w in ws)
    by_day = {}
    for w in ws:
        by_day[w["workout_date"]] = by_day.get(w["workout_date"], 0) + 1
    days = len(rs)
    return {"rs": rs, "ws": ws, "weights": weights, "ms": ms, "food": food, "burned": burned, "net": food - burned, "days": days, "avg_food": round(food / days) if days else 0, "avg_burned": round(burned / days) if days else 0, "avg_net": round((food-burned)/days) if days else 0, "double": sum(1 for v in by_day.values() if v >= 2)}


def cmp_text(label, cur, prev, lower_good=True, unit=""):
    if prev is None:
        return f"{label}: {cur}{unit}"
    diff = cur - prev
    good = diff < 0 if lower_good else diff > 0
    icon = "🟢" if good else "🔴" if diff else "➖"
    sign = "+" if diff > 0 else ""
    return f"{label}: {cur}{unit} | было: {prev}{unit} | {icon} {sign}{diff}{unit}"


def meas_line(name, key, first, last, pfirst=None, plast=None):
    a, b = first[key], last[key]
    delta = b - a
    icon = "✅" if delta < 0 else "➖" if delta == 0 else "⚠️"
    text = f"{name}: {a:g} → {b:g} см / {delta:+.1f} см {icon}"
    if pfirst and plast:
        pd = plast[key] - pfirst[key]
        text += f" | прошлый месяц: {pd:+.1f} см"
    return text


async def generate_month_report(tg_id, msg):
    user = get_user(tg_id)
    start, end = month_bounds()
    ps, pe = previous_month_bounds()
    s = month_stats(tg_id, start, end)
    p = month_stats(tg_id, ps, pe)
    if not s["weights"]:
        await msg.answer("⚠️ За этот месяц нет веса. Сначала нажми «⚖️ Внести вес».")
        return
    if not s["ms"]:
        await msg.answer("⚠️ За этот месяц нет замеров. Сначала нажми «📏 Замеры тела».")
        return
    _, _, ideal, _, _, _ = training_targets(user)
    total, lost, left, gp = goal_progress(user)
    start_w = s["weights"][0]["weight"]
    end_w = s["weights"][-1]["weight"]
    wd = end_w - start_w
    food_goal = user["daily_calorie_goal"] * max(s["days"], 1)
    workout_goal = ideal * max(s["days"], 1)
    prev_food = p["food"] if p["days"] else None
    prev_burned = p["burned"] if p["days"] else None
    prev_net = p["net"] if p["days"] else None
    m1, m2 = s["ms"][0], s["ms"][-1]
    pm1 = p["ms"][0] if p["ms"] else None
    pm2 = p["ms"][-1] if p["ms"] else None
    meas = "\n".join([
        meas_line("Шея", "neck_cm", m1, m2, pm1, pm2),
        meas_line("Грудь", "chest_cm", m1, m2, pm1, pm2),
        meas_line("Живот", "belly_cm", m1, m2, pm1, pm2),
        meas_line("Таз", "hips_cm", m1, m2, pm1, pm2),
        meas_line("Бедро", "thigh_cm", m1, m2, pm1, pm2),
        meas_line("Икры", "calves_cm", m1, m2, pm1, pm2),
        meas_line("Предплечья", "forearms_cm", m1, m2, pm1, pm2),
    ])
    report = (
        f"📅 <b>Отчет за месяц {month_key()}</b>\n\n"
        f"🍽 <b>Питание</b>\nСъедено: {s['food']} ккал\nСреднее: {s['avg_food']} ккал/день\n{bar(s['food']/food_goal*100)}\n{food_status(s['avg_food'], user['daily_calorie_goal'])}\n{cmp_text('Сравнение еды', s['food'], prev_food, True, ' ккал')}\n\n"
        f"🔥 <b>Тренировки</b>\nСожжено: {s['burned']} ккал\nТренировок: {len(s['ws'])}\nДней с 2 тренировками: {s['double']}\n{bar(s['burned']/workout_goal*100)}\n{workout_status(s['avg_burned'], ideal)}\n{cmp_text('Сравнение активности', s['burned'], prev_burned, False, ' ккал')}\n\n"
        f"⚡ <b>Баланс</b>\nЧистые калории: {s['net']} ккал\nСредние чистые: {s['avg_net']} ккал/день\n{cmp_text('Сравнение чистых калорий', s['net'], prev_net, True, ' ккал')}\n\n"
        f"⚖️ <b>Вес</b>\n{start_w:g} → {end_w:g} кг\nИзменение: {wd:+.1f} кг\n\n"
        f"🎯 <b>Цель</b>\n{bar(gp)}\nСброшено всего: {lost:.1f} кг\nОсталось: {left:.1f} кг\n\n"
        f"📏 <b>Замеры</b>\n{meas}"
    )
    with db() as conn:
        conn.execute("""
        INSERT INTO monthly_reports (telegram_id, report_month, total_food_calories, avg_food_calories, total_workout_calories, avg_workout_calories, net_calories, avg_net_calories, total_workouts, double_training_days, start_weight, end_weight, weight_delta, goal_progress_percent, report_text, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id, report_month) DO UPDATE SET report_text=excluded.report_text, total_food_calories=excluded.total_food_calories, total_workout_calories=excluded.total_workout_calories, net_calories=excluded.net_calories
        """, (tg_id, month_key(), s["food"], s["avg_food"], s["burned"], s["avg_burned"], s["net"], s["avg_net"], len(s["ws"]), s["double"], start_w, end_w, wd, gp, report, now_iso()))
    await msg.answer(report)


async def daily_reminder():
    with db() as conn:
        users = conn.execute("SELECT telegram_id FROM users WHERE is_registered=1").fetchall()
    for u in users:
        try:
            await bot.send_message(u["telegram_id"], "⏰ Время внести калории и тренировки за день.", reply_markup=main_menu())
        except Exception:
            pass


async def weekly_summary():
    end = now().date()
    start = end - timedelta(days=6)
    with db() as conn:
        users = conn.execute("SELECT telegram_id FROM users WHERE is_registered=1").fetchall()
    for u in users:
        try:
            text = week_summary_text(u["telegram_id"], start, end)
            await bot.send_message(u["telegram_id"], text)
        except Exception:
            pass


async def monthly_reminder():
    with db() as conn:
        users = conn.execute("SELECT telegram_id FROM users WHERE is_registered=1").fetchall()
    for u in users:
        try:
            await bot.send_message(u["telegram_id"], "📅 Новый месяц. Внеси вес и замеры, затем сформируй месячный отчет.", reply_markup=main_menu())
        except Exception:
            pass


async def main():
    init_db()
    scheduler = AsyncIOScheduler(timezone=str(TZ))
    scheduler.add_job(daily_reminder, "cron", hour=20, minute=0)
    scheduler.add_job(weekly_summary, "cron", day_of_week="sun", hour=20, minute=30)
    scheduler.add_job(monthly_reminder, "cron", day=1, hour=9, minute=0)
    scheduler.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
