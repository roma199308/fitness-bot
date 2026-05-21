import os
import asyncpg
import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.client.default import DefaultBotProperties

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_PUBLIC_URL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_PUBLIC_URL не найден")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher()

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📝 Внести отчет за день")],
        [KeyboardButton(text="📊 Мини-дашборд")],
        [KeyboardButton(text="📈 Прогноз цели")],
        [KeyboardButton(text="⚙️ Настройки")]
    ],
    resize_keyboard=True
)

pool = None

async def create_tables():
    async with pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            weight FLOAT,
            goal_weight FLOAT
        )
        """)

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "🔥 Fitness Bot PostgreSQL работает!",
        reply_markup=main_keyboard
    )

async def main():
    global pool

    pool = await asyncpg.create_pool(DATABASE_URL)

    await create_tables()

    print("Fitness bot PostgreSQL started")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
