import os
import asyncio
from datetime import date, timedelta

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
        [KeyboardButton(text="⚖️ Внести вес"), KeyboardButton(text="⚙️ Настройки")],
        [KeyboardButton(text="💾 Backup")]
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
        age INT,
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


async def ensure_user(user_id):
    await execute("INSERT INTO users(user_id) VALUES($1) ON CONFLICT (user_id) DO NOTHING", user_id)


async def get_user(user_id):
    await ensure_user(user_id)
    return await fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)


def to_float(text):
    return float(text.replace(",", ".").strip())


def to_int(text):
    return int(round(to_float(text)))


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
    burned = sum(w["calories_burned"] for w in workout_rows)
    return report, workout_rows, calories, burned, counted


async def forecast_text(user_id, user):
    total, lost, left, percent = calc_progress(user)
    if left <= 0 and user["target_weight"]:
        return "🎉 Цель уже достигнута"

    weight_rows = await fetch(
        "SELECT weight_date, weight FROM weight_logs WHERE user_id=$1 ORDER BY weight_date ASC",
        user_id
    )

    if len(weight_rows) < 2:
        return "Пока мало данных для прогноза. Внеси вес минимум 2 раза."

    first = weight_rows[0]
    last = weight_rows[-1]
    days = max(1, (last["weight_date"] - first["weight_date"]).days)
    kg_lost = first["weight"] - last["weight"]

    if kg_lost <= 0:
        return "Темп пока не считается: вес еще не снижался."

    kg_per_day = kg_lost / days
    target_date = date.today() + timedelta(days=round(left / kg_per_day))

    return (
        f"📌 Осталось: {left:.1f} кг\n"
        f"⚡ Темп: {kg_per_day * 30:.1f} кг/месяц\n"
        f"🎯 Прогноз цели: {target_date.strftime('%d.%m.%Y')}"
    )


@dp.message(CommandStart())
async def start(message: Message):
    user_id = message.from_user.id
    user = await get_user(user_id)

    if user and user.get("is_registered"):
        await message.answer("Главное меню:", reply_markup=main_keyboard)
        return

    states[user_id] = {
        "flow": "registration",
        "step": "start_weight",
        "data": {}
    }

    await message.answer(
        "Привет! Настроим цель.\n\n1/5 Введи текущий вес, например: 101.3"
    )


@dp.message(Command("menu"))
async def menu(message: Message):
    await message.answer("Главное меню:", reply_markup=main_keyboard)


@dp.message(F.text == "📊 Мини-дашборд")
async def dashboard(message: Message):
    user_id = message.from_user.id
    user = await get_user(user_id)

    if not user.get("is_registered"):
        await message.answer("Сначала нажми /start и заполни данные.")
        return

    _, workouts, calories, burned, counted = await today_stats(user_id)
    total, lost, left, percent = calc_progress(user)
    score, label = calc_day_index(user, calories, burned)
    cal_text = "не считал" if not counted else f"{calories or 0} ккал"

    await message.answer(
        "📊 <b>Мини-дашборд</b>\n\n"
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

    if not user.get("is_registered"):
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


@dp.message(F.text == "💾 Backup")
async def backup(message: Message):
    await message.answer("💾 Данные хранятся в PostgreSQL и не теряются после деплоя.")


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
        state["step"] = "age"
        await message.answer("4/5 Введи возраст, например: 33")
    elif step == "age":
        data["age"] = to_int(text)
        state["step"] = "calories"
        await message.answer("5/5 Введи дневной лимит калорий, например: 1600")
    elif step == "calories":
        data["calories"] = to_int(text)
        await execute(
            """
            UPDATE users SET
                start_weight=$1,
                current_weight=$1,
                target_weight=$2,
                height_cm=$3,
                age=$4,
                daily_calorie_goal=$5,
                is_registered=TRUE
            WHERE user_id=$6
            """,
            data["start_weight"], data["target_weight"], data["height"],
            data["age"], data["calories"], user_id
        )
        await execute(
            "INSERT INTO weight_logs(user_id, weight_date, weight) VALUES($1, CURRENT_DATE, $2)",
            user_id, data["start_weight"]
        )
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
    await execute(
        """
        INSERT INTO daily_reports(user_id, report_date, calories_in, calories_counted, no_workout)
        VALUES($1, CURRENT_DATE, $2, $3, $4)
        ON CONFLICT(user_id, report_date) DO UPDATE SET
            calories_in=EXCLUDED.calories_in,
            calories_counted=EXCLUDED.calories_counted,
            no_workout=EXCLUDED.no_workout
        """,
        user_id, data.get("calories_in"), data.get("calories_counted", True), data.get("no_workout", False)
    )
    await execute("DELETE FROM workouts WHERE user_id=$1 AND workout_date=CURRENT_DATE", user_id)
    for w in data.get("workouts", []):
        await execute(
            "INSERT INTO workouts(user_id, workout_date, workout_type, calories_burned) VALUES($1, CURRENT_DATE, $2, $3)",
            user_id, w["type"], w["calories"]
        )
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
    step = state["step"]

    if step == "target_weight":
        value = to_float(text)
        await execute("UPDATE users SET target_weight=$1 WHERE user_id=$2", value, user_id)
        await message.answer(f"✅ Цель обновлена: {value} кг", reply_markup=main_keyboard)
    elif step == "calories":
        value = to_int(text)
        await execute("UPDATE users SET daily_calorie_goal=$1 WHERE user_id=$2", value, user_id)
        await message.answer(f"✅ Лимит обновлен: {value} ккал", reply_markup=main_keyboard)

    states.pop(user_id, None)


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
    print("Fitness bot PostgreSQL button fix started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
