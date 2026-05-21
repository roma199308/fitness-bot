
import os
import asyncio
from datetime import date, datetime, timedelta

import asyncpg
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.client.default import DefaultBotProperties

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL не найден")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
pool = None
states = {}

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📝 Внести отчет за день")],
        [KeyboardButton(text="📊 Мини-дашборд"), KeyboardButton(text="📈 Прогноз цели")],
        [KeyboardButton(text="⚖️ Внести вес"), KeyboardButton(text="📏 Замеры месяца")],
        [KeyboardButton(text="📅 Отчет за месяц"), KeyboardButton(text="🗂 История месяцев")],
        [KeyboardButton(text="🗂 История недель"), KeyboardButton(text="⚙️ Настройки")],
    ],
    resize_keyboard=True
)

workout_count_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="0 тренировок")],
        [KeyboardButton(text="1 тренировка")],
        [KeyboardButton(text="2 тренировки")],
        [KeyboardButton(text="Отмена")]
    ],
    resize_keyboard=True
)

workout_type_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🏋️ Силовая"), KeyboardButton(text="🚴 Велотренажер")],
        [KeyboardButton(text="⚽ Футбол"), KeyboardButton(text="🏃 Бег")],
        [KeyboardButton(text="🚶 Ходьба"), KeyboardButton(text="🔥 Другое")],
        [KeyboardButton(text="Отмена")]
    ],
    resize_keyboard=True
)

settings_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎯 Изменить целевой вес")],
        [KeyboardButton(text="🍽 Изменить лимит калорий")],
        [KeyboardButton(text="Назад")]
    ],
    resize_keyboard=True
)


async def execute(query, *args):
    async with pool.acquire() as conn:
        return await conn.execute(query, *args)


async def fetchrow(query, *args):
    async with pool.acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetch(query, *args):
    async with pool.acquire() as conn:
        return await conn.fetch(query, *args)


async def create_tables():
    await execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        start_weight DOUBLE PRECISION,
        current_weight DOUBLE PRECISION,
        target_weight DOUBLE PRECISION,
        height_cm DOUBLE PRECISION,
        birth_date DATE,
        daily_calorie_goal INT DEFAULT 1600,
        is_registered BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT NOW()
    )
    """)
    await execute("""
    CREATE TABLE IF NOT EXISTS daily_reports (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        report_date DATE NOT NULL DEFAULT CURRENT_DATE,
        calories_in INT,
        calories_counted BOOLEAN DEFAULT TRUE,
        no_workout BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT NOW(),
        UNIQUE(user_id, report_date)
    )
    """)
    await execute("""
    CREATE TABLE IF NOT EXISTS workouts (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        workout_date DATE NOT NULL DEFAULT CURRENT_DATE,
        workout_type TEXT NOT NULL,
        calories_burned INT NOT NULL,
        created_at TIMESTAMP DEFAULT NOW()
    )
    """)
    await execute("""
    CREATE TABLE IF NOT EXISTS weight_logs (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        weight_date DATE NOT NULL DEFAULT CURRENT_DATE,
        weight DOUBLE PRECISION NOT NULL,
        created_at TIMESTAMP DEFAULT NOW()
    )
    """)
    await execute("""
    CREATE TABLE IF NOT EXISTS body_measurements (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        measure_month TEXT NOT NULL,
        measure_date DATE NOT NULL DEFAULT CURRENT_DATE,
        chest_cm DOUBLE PRECISION,
        biceps_cm DOUBLE PRECISION,
        forearm_cm DOUBLE PRECISION,
        belly_cm DOUBLE PRECISION,
        hips_cm DOUBLE PRECISION,
        thigh_cm DOUBLE PRECISION,
        calf_cm DOUBLE PRECISION,
        neck_cm DOUBLE PRECISION,
        created_at TIMESTAMP DEFAULT NOW(),
        UNIQUE(user_id, measure_month)
    )
    """)
    await execute("""
    CREATE TABLE IF NOT EXISTS weekly_summaries (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        week_start DATE NOT NULL,
        week_end DATE NOT NULL,
        total_calories_in INT,
        total_calories_out INT,
        not_counted_days INT,
        no_workout_days INT,
        summary_text TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT NOW(),
        UNIQUE(user_id, week_start, week_end)
    )
    """)
    await execute("""
    CREATE TABLE IF NOT EXISTS monthly_reports (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        report_month TEXT NOT NULL,
        report_text TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT NOW(),
        UNIQUE(user_id, report_month)
    )
    """)


async def ensure_user(user_id):
    await execute("INSERT INTO users(user_id) VALUES($1) ON CONFLICT (user_id) DO NOTHING", user_id)


async def get_user(user_id):
    await ensure_user(user_id)
    return await fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)


def to_float(text):
    return float(text.replace(",", ".").strip())


def to_int(text):
    return int(round(to_float(text)))


def parse_birth_date(text):
    return datetime.strptime(text.strip(), "%d.%m.%Y").date()


def age_from_birth(birth_date):
    if not birth_date:
        return None
    today = date.today()
    return today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))


def month_key(d=None):
    d = d or date.today()
    return f"{d.year:04d}-{d.month:02d}"


def previous_month_key():
    first = date.today().replace(day=1)
    prev = first - timedelta(days=1)
    return month_key(prev)


def month_bounds(key=None):
    if key:
        y, m = map(int, key.split("-"))
        start = date(y, m, 1)
    else:
        start = date.today().replace(day=1)
    if start.month == 12:
        end = date(start.year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(start.year, start.month + 1, 1) - timedelta(days=1)
    return start, end


def progress_bar(percent, size=10):
    percent = max(0, percent)
    filled = round(min(percent, 100) / 100 * size)
    return "█" * filled + "░" * (size - filled) + f" {percent:.0f}%"


def training_target(weight):
    if weight and weight < 85:
        return 450
    if weight and weight < 95:
        return 500
    return 600


def calc_progress(user):
    start = user["start_weight"] or 0
    current = user["current_weight"] or start
    target = user["target_weight"] or 0
    total = start - target
    lost = start - current
    left = current - target
    percent = max(0, min(100, lost / total * 100)) if total > 0 else 0
    return total, lost, left, percent


def calc_day_index(user, calories, burned):
    goal = user["daily_calorie_goal"] or 1600
    workout_goal = training_target(user["current_weight"])
    if calories is None:
        food_score = 20
    elif calories <= goal:
        food_score = 55
    elif calories <= goal * 1.1:
        food_score = 40
    else:
        food_score = 20
    workout_score = min(35, round(35 * min((burned or 0) / workout_goal, 1.2) / 1.2))
    balance_score = 10 if calories is not None and calories - burned <= goal else 5
    score = max(0, min(100, food_score + workout_score + balance_score))
    label = "🟢 Отличный день" if score >= 85 else "🟡 Нормально" if score >= 65 else "🔴 Ниже плана"
    return score, label


async def today_stats(user_id):
    today = date.today()
    report = await fetchrow("SELECT * FROM daily_reports WHERE user_id=$1 AND report_date=$2", user_id, today)
    workout_rows = await fetch("SELECT * FROM workouts WHERE user_id=$1 AND workout_date=$2 ORDER BY id ASC", user_id, today)
    calories = report["calories_in"] if report else None
    counted = report["calories_counted"] if report else False
    no_workout = report["no_workout"] if report else False
    burned = sum(w["calories_burned"] for w in workout_rows)
    return report, workout_rows, calories, burned, counted, no_workout


async def forecast_text(user_id, user):
    total, lost, left, percent = calc_progress(user)
    if left <= 0 and user["target_weight"]:
        return "🎉 Цель уже достигнута"
    ws = await fetch("SELECT weight_date, weight FROM weight_logs WHERE user_id=$1 ORDER BY weight_date ASC", user_id)
    if len(ws) < 2:
        return "Пока мало данных для прогноза. Внеси вес минимум 2 раза."
    first, last = ws[0], ws[-1]
    days = max(1, (last["weight_date"] - first["weight_date"]).days)
    kg_lost = first["weight"] - last["weight"]
    if kg_lost <= 0:
        return "Темп пока не считается: вес еще не снижался."
    kg_per_day = kg_lost / days
    target_date = date.today() + timedelta(days=round(left / kg_per_day))
    return f"📌 Осталось: {left:.1f} кг\n⚡ Темп: {kg_per_day * 30:.1f} кг/месяц\n🎯 Прогноз цели: {target_date.strftime('%d.%m.%Y')}"


@dp.message(CommandStart())
async def start(message: Message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    if user["is_registered"]:
        await message.answer("Главное меню:", reply_markup=main_keyboard)
        return
    states[user_id] = {"flow": "registration", "step": "start_weight", "data": {}}
    await message.answer("Привет! Настроим цель.\n\n1/5 Введи текущий вес, например: 101.3")


@dp.message(Command("menu"))
async def menu(message: Message):
    await message.answer("Главное меню:", reply_markup=main_keyboard)


@dp.message(F.text == "📊 Мини-дашборд")
async def dashboard(message: Message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    if not user["is_registered"]:
        await message.answer("Сначала нажми /start и заполни данные.")
        return
    _, workouts, calories, burned, counted, no_workout = await today_stats(user_id)
    total, lost, left, percent = calc_progress(user)
    score, label = calc_day_index(user, calories, burned)
    cal_text = "не считал" if not counted else f"{calories or 0} ккал"
    age = age_from_birth(user["birth_date"])
    await message.answer(
        "📊 <b>Мини-дашборд</b>\n\n"
        f"🎂 Возраст: {age if age else '—'}\n"
        f"⚖️ Вес: {user['current_weight']} кг\n"
        f"🎯 Цель: {user['target_weight']} кг\n"
        f"📈 Прогресс:\n{progress_bar(percent)}\n"
        f"✅ Сброшено: {lost:.1f} кг\n"
        f"📌 Осталось: {left:.1f} кг\n\n"
        f"🍽 Сегодня: {cal_text}\n"
        f"🔥 Сожжено: {burned} ккал\n"
        f"🏋️ Тренировок: {len(workouts)}\n"
        f"❤️ Индекс: {score}/100 {label}"
    )


@dp.message(F.text == "📈 Прогноз цели")
async def forecast(message: Message):
    user = await get_user(message.from_user.id)
    if not user["is_registered"]:
        await message.answer("Сначала нажми /start и заполни данные.")
        return
    await message.answer("📈 <b>Прогноз цели</b>\n\n" + await forecast_text(message.from_user.id, user))


@dp.message(F.text == "📝 Внести отчет за день")
async def daily_start(message: Message):
    states[message.from_user.id] = {"flow": "daily", "step": "calories", "data": {}}
    await message.answer("Сколько калорий съел сегодня?\nВведи число или напиши: не считал")


@dp.message(F.text == "⚖️ Внести вес")
async def add_weight(message: Message):
    states[message.from_user.id] = {"flow": "weight", "step": "weight", "data": {}}
    await message.answer("Введи текущий вес, например: 98.4")


@dp.message(F.text == "📏 Замеры месяца")
async def measurements_start(message: Message):
    key = month_key()
    existing = await fetchrow("SELECT id FROM body_measurements WHERE user_id=$1 AND measure_month=$2", message.from_user.id, key)
    states[message.from_user.id] = {"flow": "measurements", "step": "chest_cm", "data": {"measure_month": key}}
    prefix = "За этот месяц замеры уже есть. Новые значения заменят старые.\n\n" if existing else ""
    await message.answer(prefix + "📏 Замеры месяца\n\n1/8 Грудь, см:")


@dp.message(F.text == "📅 Отчет за месяц")
async def month_report(message: Message):
    await send_month_report(message)


@dp.message(F.text == "🗂 История месяцев")
async def month_history(message: Message):
    rows = await fetch("SELECT report_month, report_text FROM monthly_reports WHERE user_id=$1 ORDER BY report_month DESC LIMIT 3", message.from_user.id)
    if not rows:
        await message.answer("Истории месяцев пока нет.")
        return
    for r in rows:
        await message.answer(r["report_text"])


@dp.message(F.text == "🗂 История недель")
async def week_history(message: Message):
    await save_current_week_summary(message.from_user.id)
    rows = await fetch("SELECT summary_text FROM weekly_summaries WHERE user_id=$1 ORDER BY week_start DESC LIMIT 5", message.from_user.id)
    if not rows:
        await message.answer("Истории недель пока нет.")
        return
    await message.answer("🗂 <b>История недель</b>\n\n" + "\n\n".join(r["summary_text"] for r in rows))


@dp.message(F.text == "⚙️ Настройки")
async def settings(message: Message):
    await message.answer("⚙️ Настройки:", reply_markup=settings_keyboard)


@dp.message(F.text == "🎯 Изменить целевой вес")
async def change_goal(message: Message):
    states[message.from_user.id] = {"flow": "settings", "step": "target_weight", "data": {}}
    await message.answer("Введи новый целевой вес:")


@dp.message(F.text == "🍽 Изменить лимит калорий")
async def change_calories(message: Message):
    states[message.from_user.id] = {"flow": "settings", "step": "calories", "data": {}}
    await message.answer("Введи новый лимит калорий:")


@dp.message(F.text == "Назад")
async def back(message: Message):
    await message.answer("Главное меню:", reply_markup=main_keyboard)


@dp.message()
async def handler(message: Message):
    user_id = message.from_user.id
    text = (message.text or "").strip()
    state = states.get(user_id)
    if text.lower() == "отмена":
        states.pop(user_id, None)
        await message.answer("Отменил.", reply_markup=main_keyboard)
        return
    if not state:
        await message.answer("Выбери действие в меню:", reply_markup=main_keyboard)
        return
    try:
        flow = state["flow"]
        if flow == "registration":
            await registration_flow(message, state, text)
        elif flow == "daily":
            await daily_flow(message, state, text)
        elif flow == "weight":
            await weight_flow(message, text)
        elif flow == "settings":
            await settings_flow(message, state, text)
        elif flow == "measurements":
            await measurements_flow(message, state, text)
    except ValueError:
        await message.answer("Введи число в правильном формате.")


async def registration_flow(message, state, text):
    user_id = message.from_user.id
    step = state["step"]
    data = state["data"]
    if step == "start_weight":
        data["start_weight"] = to_float(text)
        state["step"] = "target_weight"
        await message.answer("2/5 Введи целевой вес, например: 80")
    elif step == "target_weight":
        data["target_weight"] = to_float(text)
        state["step"] = "height"
        await message.answer("3/5 Введи рост в см, например: 183")
    elif step == "height":
        data["height"] = to_float(text)
        state["step"] = "birth_date"
        await message.answer("4/5 Введи дату рождения в формате ДД.ММ.ГГГГ, например: 15.08.1992")
    elif step == "birth_date":
        data["birth_date"] = parse_birth_date(text)
        state["step"] = "calories"
        await message.answer("5/5 Введи дневной лимит калорий, например: 1600")
    elif step == "calories":
        data["calories"] = to_int(text)
        await execute("""
            UPDATE users SET start_weight=$1, current_weight=$1, target_weight=$2,
            height_cm=$3, birth_date=$4, daily_calorie_goal=$5, is_registered=TRUE
            WHERE user_id=$6
        """, data["start_weight"], data["target_weight"], data["height"], data["birth_date"], data["calories"], user_id)
        await execute("INSERT INTO weight_logs(user_id, weight_date, weight) VALUES($1, CURRENT_DATE, $2)", user_id, data["start_weight"])
        states.pop(user_id, None)
        await message.answer("✅ Настройка завершена.", reply_markup=main_keyboard)


async def daily_flow(message, state, text):
    user_id = message.from_user.id
    step = state["step"]
    data = state["data"]
    if step == "calories":
        if text.lower() in ["не считал", "не считал калории", "не знаю"]:
            data["calories_in"] = None
            data["calories_counted"] = False
        else:
            data["calories_in"] = to_int(text)
            data["calories_counted"] = True
        state["step"] = "workout_count"
        await message.answer("Сколько тренировок было сегодня?", reply_markup=workout_count_keyboard)
    elif step == "workout_count":
        if text.startswith("0"):
            data["workouts_count"] = 0
            data["workouts"] = []
            data["no_workout"] = True
            await save_daily(message, data)
        elif text.startswith("1") or text.startswith("2"):
            data["workouts_count"] = int(text[0])
            data["workouts"] = []
            data["no_workout"] = False
            state["step"] = "workout_type"
            await message.answer("Выбери тип тренировки:", reply_markup=workout_type_keyboard)
        else:
            await message.answer("Выбери 0, 1 или 2 тренировки.", reply_markup=workout_count_keyboard)
    elif step == "workout_type":
        data["current_type"] = text
        state["step"] = "workout_calories"
        await message.answer(f"Сколько калорий сжег на «{text}»?")
    elif step == "workout_calories":
        data["workouts"].append({"type": data["current_type"], "calories": to_int(text)})
        if len(data["workouts"]) < data["workouts_count"]:
            state["step"] = "workout_type"
            await message.answer("Выбери тип следующей тренировки:", reply_markup=workout_type_keyboard)
        else:
            await save_daily(message, data)


async def save_daily(message, data):
    user_id = message.from_user.id
    await execute("""
        INSERT INTO daily_reports(user_id, report_date, calories_in, calories_counted, no_workout)
        VALUES($1, CURRENT_DATE, $2, $3, $4)
        ON CONFLICT(user_id, report_date) DO UPDATE SET
            calories_in=EXCLUDED.calories_in,
            calories_counted=EXCLUDED.calories_counted,
            no_workout=EXCLUDED.no_workout
    """, user_id, data.get("calories_in"), data.get("calories_counted", True), data.get("no_workout", False))
    await execute("DELETE FROM workouts WHERE user_id=$1 AND workout_date=CURRENT_DATE", user_id)
    for w in data.get("workouts", []):
        await execute("INSERT INTO workouts(user_id, workout_date, workout_type, calories_burned) VALUES($1, CURRENT_DATE, $2, $3)", user_id, w["type"], w["calories"])
    states.pop(user_id, None)
    await message.answer("✅ Отчет за день сохранен.", reply_markup=main_keyboard)
    await dashboard(message)


async def weight_flow(message, text):
    user_id = message.from_user.id
    weight = to_float(text)
    await execute("UPDATE users SET current_weight=$1 WHERE user_id=$2", weight, user_id)
    await execute("INSERT INTO weight_logs(user_id, weight_date, weight) VALUES($1, CURRENT_DATE, $2)", user_id, weight)
    states.pop(user_id, None)
    await message.answer("✅ Вес сохранен.", reply_markup=main_keyboard)
    await dashboard(message)


async def settings_flow(message, state, text):
    user_id = message.from_user.id
    if state["step"] == "target_weight":
        value = to_float(text)
        await execute("UPDATE users SET target_weight=$1 WHERE user_id=$2", value, user_id)
        await message.answer(f"✅ Цель обновлена: {value} кг", reply_markup=main_keyboard)
    elif state["step"] == "calories":
        value = to_int(text)
        await execute("UPDATE users SET daily_calorie_goal=$1 WHERE user_id=$2", value, user_id)
        await message.answer(f"✅ Лимит обновлен: {value} ккал", reply_markup=main_keyboard)
    states.pop(user_id, None)


async def measurements_flow(message, state, text):
    user_id = message.from_user.id
    step = state["step"]
    data = state["data"]
    data[step] = to_float(text)
    order = ["chest_cm", "biceps_cm", "forearm_cm", "belly_cm", "hips_cm", "thigh_cm", "calf_cm", "neck_cm"]
    prompts = {
        "biceps_cm": "2/8 Бицепс, см:",
        "forearm_cm": "3/8 Предплечье, см:",
        "belly_cm": "4/8 Живот, см:",
        "hips_cm": "5/8 Таз, см:",
        "thigh_cm": "6/8 Бедро, см:",
        "calf_cm": "7/8 Икра, см:",
        "neck_cm": "8/8 Шея, см:",
    }
    idx = order.index(step)
    if idx < len(order) - 1:
        next_step = order[idx + 1]
        state["step"] = next_step
        await message.answer(prompts[next_step])
        return
    await execute("""
        INSERT INTO body_measurements(
            user_id, measure_month, chest_cm, biceps_cm, forearm_cm, belly_cm,
            hips_cm, thigh_cm, calf_cm, neck_cm
        )
        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        ON CONFLICT(user_id, measure_month) DO UPDATE SET
            chest_cm=EXCLUDED.chest_cm,
            biceps_cm=EXCLUDED.biceps_cm,
            forearm_cm=EXCLUDED.forearm_cm,
            belly_cm=EXCLUDED.belly_cm,
            hips_cm=EXCLUDED.hips_cm,
            thigh_cm=EXCLUDED.thigh_cm,
            calf_cm=EXCLUDED.calf_cm,
            neck_cm=EXCLUDED.neck_cm,
            measure_date=CURRENT_DATE
    """, user_id, data["measure_month"], data.get("chest_cm"), data.get("biceps_cm"), data.get("forearm_cm"),
        data.get("belly_cm"), data.get("hips_cm"), data.get("thigh_cm"), data.get("calf_cm"), data.get("neck_cm"))
    states.pop(user_id, None)
    await message.answer("✅ Замеры месяца сохранены.", reply_markup=main_keyboard)


async def monthly_stats(user_id, key):
    start, end = month_bounds(key)
    reports = await fetch("SELECT * FROM daily_reports WHERE user_id=$1 AND report_date BETWEEN $2 AND $3", user_id, start, end)
    workouts = await fetch("SELECT * FROM workouts WHERE user_id=$1 AND workout_date BETWEEN $2 AND $3", user_id, start, end)
    weights = await fetch("SELECT * FROM weight_logs WHERE user_id=$1 AND weight_date BETWEEN $2 AND $3 ORDER BY weight_date", user_id, start, end)
    meas = await fetchrow("SELECT * FROM body_measurements WHERE user_id=$1 AND measure_month=$2", user_id, key)
    counted = [r for r in reports if r["calories_counted"]]
    total_in = sum(r["calories_in"] or 0 for r in counted)
    total_out = sum(w["calories_burned"] for w in workouts)
    return {
        "reports": reports, "workouts": workouts, "weights": weights, "meas": meas,
        "total_in": total_in, "total_out": total_out,
        "not_counted": sum(1 for r in reports if not r["calories_counted"]),
        "no_workout": sum(1 for r in reports if r["no_workout"]),
        "avg_in": round(total_in / len(counted)) if counted else 0
    }


def meas_line(label, key, cur, prev):
    if not cur or cur[key] is None:
        return f"{label}: нет данных"
    if prev and prev[key] is not None:
        diff = cur[key] - prev[key]
        return f"{label}: {cur[key]:g} см ({diff:+.1f} см к прошлому мес.)"
    return f"{label}: {cur[key]:g} см"


async def send_month_report(message):
    user_id = message.from_user.id
    key = month_key()
    prev_key = previous_month_key()
    s = await monthly_stats(user_id, key)
    p = await monthly_stats(user_id, prev_key)
    w_text = "нет данных"
    if s["weights"]:
        w_start = s["weights"][0]["weight"]
        w_end = s["weights"][-1]["weight"]
        w_text = f"{w_start:g} → {w_end:g} кг ({w_end - w_start:+.1f} кг)"
    lines = [
        f"📅 <b>Отчет за месяц {key}</b>",
        "",
        f"🍽 Съедено: {s['total_in']} ккал",
        f"🍽 Среднее по дням подсчета: {s['avg_in']} ккал/день",
        f"❔ Дней без подсчета калорий: {s['not_counted']}",
        "",
        f"🔥 Сожжено: {s['total_out']} ккал",
        f"🏋️ Тренировок: {len(s['workouts'])}",
        f"🚫 Дней без тренировки: {s['no_workout']}",
        "",
        f"⚖️ Вес: {w_text}",
        "",
        "📏 <b>Замеры</b>",
        meas_line("Грудь", "chest_cm", s["meas"], p["meas"]),
        meas_line("Бицепс", "biceps_cm", s["meas"], p["meas"]),
        meas_line("Предплечье", "forearm_cm", s["meas"], p["meas"]),
        meas_line("Живот", "belly_cm", s["meas"], p["meas"]),
        meas_line("Таз", "hips_cm", s["meas"], p["meas"]),
        meas_line("Бедро", "thigh_cm", s["meas"], p["meas"]),
        meas_line("Икра", "calf_cm", s["meas"], p["meas"]),
        meas_line("Шея", "neck_cm", s["meas"], p["meas"]),
    ]
    text = "\n".join(lines)
    await execute("""
        INSERT INTO monthly_reports(user_id, report_month, report_text)
        VALUES($1,$2,$3)
        ON CONFLICT(user_id, report_month) DO UPDATE SET report_text=EXCLUDED.report_text, created_at=NOW()
    """, user_id, key, text)
    await message.answer(text)


async def save_current_week_summary(user_id):
    today = date.today()
    start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=6)
    reports = await fetch("SELECT * FROM daily_reports WHERE user_id=$1 AND report_date BETWEEN $2 AND $3", user_id, start, end)
    workouts = await fetch("SELECT * FROM workouts WHERE user_id=$1 AND workout_date BETWEEN $2 AND $3", user_id, start, end)
    counted = [r for r in reports if r["calories_counted"]]
    total_in = sum(r["calories_in"] or 0 for r in counted)
    total_out = sum(w["calories_burned"] for w in workouts)
    not_counted = sum(1 for r in reports if not r["calories_counted"])
    no_workout = sum(1 for r in reports if r["no_workout"])
    text = (
        f"📌 <b>Неделя {start.strftime('%d.%m')}—{end.strftime('%d.%m')}</b>\n"
        f"🍽 Съедено: {total_in} ккал\n"
        f"🔥 Сожжено: {total_out} ккал\n"
        f"❔ Без подсчета: {not_counted} дней\n"
        f"🚫 Без тренировки: {no_workout} дней"
    )
    await execute("""
        INSERT INTO weekly_summaries(user_id, week_start, week_end, total_calories_in, total_calories_out, not_counted_days, no_workout_days, summary_text)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8)
        ON CONFLICT(user_id, week_start, week_end) DO UPDATE SET
            total_calories_in=EXCLUDED.total_calories_in,
            total_calories_out=EXCLUDED.total_calories_out,
            not_counted_days=EXCLUDED.not_counted_days,
            no_workout_days=EXCLUDED.no_workout_days,
            summary_text=EXCLUDED.summary_text
    """, user_id, start, end, total_in, total_out, not_counted, no_workout, text)


async def main():
    global pool
    for i in range(10):
        try:
            pool = await asyncpg.create_pool(DATABASE_URL)
            print("PostgreSQL connected")
            break
        except Exception as e:
            print(f"DB retry {i + 1}/10:", e)
            await asyncio.sleep(5)
    if pool is None:
        raise RuntimeError("Не удалось подключиться к PostgreSQL")
    await create_tables()
    print("Fitness bot PostgreSQL full v2 started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
