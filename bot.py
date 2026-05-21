import os
import sqlite3
import asyncio
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "fitness_bot.db")
TZ = ZoneInfo(os.getenv("BOT_TZ", "Europe/Kyiv"))
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден. Добавь его в Railway Variables.")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
states = {}

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def now(): return datetime.now(TZ)
def today(): return now().date().isoformat()
def now_iso(): return now().isoformat()
def month_key(d=None):
    d = d or now().date()
    return f"{d.year:04d}-{d.month:02d}"

def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS users(
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
            created_at TEXT NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS daily_reports(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            report_date TEXT NOT NULL,
            food_calories INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(telegram_id, report_date))""")
        con.execute("""CREATE TABLE IF NOT EXISTS workouts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            workout_date TEXT NOT NULL,
            workout_type TEXT NOT NULL,
            calories_burned INTEGER NOT NULL,
            created_at TEXT NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS weight_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            weight_date TEXT NOT NULL,
            weight REAL NOT NULL,
            created_at TEXT NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS body_measurements(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            measurement_date TEXT NOT NULL,
            neck_cm REAL, chest_cm REAL, belly_cm REAL, hips_cm REAL,
            thigh_cm REAL, calves_cm REAL, forearms_cm REAL,
            created_at TEXT NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS weekly_summaries(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            week_start TEXT NOT NULL,
            week_end TEXT NOT NULL,
            total_food_calories INTEGER NOT NULL,
            total_workout_calories INTEGER NOT NULL,
            net_calories INTEGER NOT NULL,
            summary_text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(telegram_id, week_start, week_end))""")
        con.execute("""CREATE TABLE IF NOT EXISTS monthly_reports(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            report_month TEXT NOT NULL,
            report_text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(telegram_id, report_month))""")
        for sql in [
            'ALTER TABLE daily_reports ADD COLUMN food_tracked INTEGER DEFAULT 1',
            'ALTER TABLE daily_reports ADD COLUMN workout_tracked INTEGER DEFAULT 1'
        ]:
            try:
                con.execute(sql)
            except sqlite3.OperationalError:
                pass

def ensure_user(uid):
    with db() as con:
        row = con.execute('SELECT telegram_id FROM users WHERE telegram_id=?', (uid,)).fetchone()
        if not row:
            con.execute('INSERT INTO users(telegram_id, created_at) VALUES(?,?)', (uid, now_iso()))

def user(uid):
    ensure_user(uid)
    with db() as con:
        return con.execute('SELECT * FROM users WHERE telegram_id=?', (uid,)).fetchone()

def registered(uid): return bool(user(uid)['is_registered'])

def fnum(x): return float(str(x).replace(',', '.').strip())
def inum(x): return int(round(fnum(x)))

def bar(percent, size=10):
    percent = max(0, percent)
    filled = round(min(percent, 100) / 100 * size)
    return '█' * filled + '░' * (size-filled) + f' {percent:.0f}%'

def training_targets(u):
    w = u['current_weight'] or u['start_weight'] or 100
    if w >= 95: return dict(min_low=350, min_high=500, ideal_low=600, ideal_high=900, double_low=800, double_high=1200)
    if w >= 85: return dict(min_low=300, min_high=450, ideal_low=500, ideal_high=800, double_low=700, double_high=1000)
    return dict(min_low=250, min_high=400, ideal_low=450, ideal_high=700, double_low=600, double_high=900)

def goal_progress(u):
    start = u['start_weight'] or 0
    cur = u['current_weight'] or start
    target = u['target_weight'] or 0
    total = start - target
    lost = start - cur
    left = cur - target
    percent = max(0, min(100, lost / total * 100)) if total > 0 else 0
    return dict(total=total, lost=lost, left=left, percent=percent, remaining=max(0, 100-percent))

def day_data(uid, d=None):
    d = d or now().date()
    ds = d.isoformat()
    with db() as con:
        rep = con.execute('SELECT * FROM daily_reports WHERE telegram_id=? AND report_date=?', (uid, ds)).fetchone()
        wos = con.execute('SELECT * FROM workouts WHERE telegram_id=? AND workout_date=?', (uid, ds)).fetchall()
    food = rep['food_calories'] if rep else 0
    food_tracked = rep['food_tracked'] if rep and 'food_tracked' in rep.keys() else 1
    workout_tracked = rep['workout_tracked'] if rep and 'workout_tracked' in rep.keys() else 1
    burned = sum(w['calories_burned'] for w in wos)
    return dict(report=rep, workouts=wos, food=food, burned=burned, net=food-burned, food_tracked=food_tracked, workout_tracked=workout_tracked)

def food_status(food, goal):
    if not goal: return '⚪ Нет цели'
    p = food / goal * 100
    if p <= 100: return '🟢 В пределах плана'
    if p <= 110: return '🟡 Небольшое превышение'
    return '🔴 Существенный перебор'

def workout_status(burned, target):
    p = burned / target * 100 if target else 0
    if p >= 100: return '🟢 Цель выполнена'
    if p >= 80: return '🟡 Почти выполнено'
    return '🔴 Ниже плана'

def day_index(u, food, burned, food_tracked=True):
    goal = u['daily_calorie_goal'] or 1600
    target = training_targets(u)['ideal_low']
    food_score = 0
    if not food_tracked:
        food_score = 25
    elif food:
        if food <= goal: food_score = 55
        elif food <= goal*1.10: food_score = 40
        elif food <= goal*1.25: food_score = 25
        else: food_score = 10
    workout_score = min(35, round(35 * min((burned/target if target else 0), 1.2) / 1.2))
    balance_score = 10 if food_tracked and food and (food-burned) <= goal else 5 if food_tracked and food else 0
    score = max(0, min(100, food_score + workout_score + balance_score))
    label = '🟢 Отличный день' if score >= 85 else '🟡 Нормальный день' if score >= 65 else '🔴 День ниже плана'
    return score, label

def forecast(uid, u):
    gp = goal_progress(u)
    if gp['left'] <= 0: return '🎉 Цель уже достигнута'
    with db() as con:
        weights = con.execute('SELECT weight_date, weight FROM weight_logs WHERE telegram_id=? ORDER BY weight_date ASC', (uid,)).fetchall()
    if len(weights) < 2: return 'Пока мало данных для прогноза. Внеси вес минимум 2 раза.'
    d1, d2 = date.fromisoformat(weights[0]['weight_date']), date.fromisoformat(weights[-1]['weight_date'])
    days = max(1, (d2-d1).days)
    lost = weights[0]['weight'] - weights[-1]['weight']
    if lost <= 0: return 'Темп пока не считается: вес еще не снижался по данным бота.'
    kg_day = lost / days
    days_left = round(gp['left'] / kg_day)
    target_date = now().date() + timedelta(days=days_left)
    return f"Осталось: {gp['left']:.1f} кг\nСредний темп: {kg_day*30:.1f} кг/месяц\nПрогноз даты цели: {target_date.strftime('%d.%m.%Y')}"

def discipline(uid, u):
    lines = []
    for i in range(6, -1, -1):
        d = now().date() - timedelta(days=i)
        dd = day_data(uid, d)
        if not dd['report']:
            icon, status = '🔴', 'нет отчета'
        else:
            score, _ = day_index(u, dd['food'], dd['burned'], dd.get('food_tracked', 1))
            icon, status = ('🟢','отлично') if score >= 85 else ('🟡','норм') if score >= 65 else ('🔴','ниже плана')
        lines.append(f"{d.strftime('%d.%m')} {icon} {status}")
    return '\n'.join(lines)

def kb_main():
    kb = InlineKeyboardBuilder()
    items = [
        ('📝 Внести отчет за день','daily_report_start'), ('📊 Мини-дашборд','dashboard'),
        ('🔥 План на сегодня','plan_today'), ('📊 Отчет за сегодня','today_report'),
        ('📈 Прогноз цели','goal_forecast'), ('📅 Календарь дисциплины','discipline_calendar'),
        ('🍽 Внести только калории','add_food'), ('🏋️ Внести только тренировку','add_workout'),
        ('🗂 История недель','weekly_history'), ('📅 Отчет за месяц','month_report_start'),
        ('🗂 История месяцев','monthly_history'), ('⚖️ Внести вес','add_weight'),
        ('📏 Замеры тела','add_measurements'), ('🎯 Моя цель','goal'), ('😵 Хочу сорваться','panic'),
        ('⚙️ Настройки','settings'), ('💾 Скачать backup','backup'), ('ℹ️ Помощь','help')]
    for t,d in items: kb.button(text=t, callback_data=d)
    kb.adjust(1,2,2,2,2,2,2,2,2)
    return kb.as_markup()

def kb_cancel():
    kb=InlineKeyboardBuilder(); kb.button(text='❌ Отмена', callback_data='cancel'); return kb.as_markup()

def kb_gender():
    kb=InlineKeyboardBuilder(); kb.button(text='Мужской', callback_data='gender:male'); kb.button(text='Женский', callback_data='gender:female'); kb.adjust(2); return kb.as_markup()

def kb_activity():
    kb=InlineKeyboardBuilder()
    for t,d in [('Малоподвижный','activity:sedentary'),('Средний','activity:medium'),('Активный','activity:active')]: kb.button(text=t, callback_data=d)
    kb.adjust(1); return kb.as_markup()

def kb_count():
    kb=InlineKeyboardBuilder()
    kb.button(text='🚫 Не было тренировки', callback_data='daily_count:0')
    kb.button(text='1 тренировка', callback_data='daily_count:1')
    kb.button(text='2 тренировки', callback_data='daily_count:2')
    kb.adjust(1); return kb.as_markup()

def kb_food_choice():
    kb=InlineKeyboardBuilder()
    kb.button(text='✍️ Ввести калории', callback_data='daily_food_manual')
    kb.button(text='❔ Не считал калории', callback_data='daily_food_skip')
    kb.adjust(1); return kb.as_markup()

def kb_workout(prefix):
    kb=InlineKeyboardBuilder()
    for t,v in [('🏋️ Силовая','Силовая'),('🚴 Велотренажер','Велотренажер'),('⚽ Футбол','Футбол'),('🏃 Бег','Бег'),('🚶 Ходьба','Ходьба'),('🏊 Плавание','Плавание'),('🥊 Бокс','Бокс'),('🧘 Йога/растяжка','Йога/растяжка'),('🔥 Другое','Другое')]:
        kb.button(text=t, callback_data=f'{prefix}:{v}')
    kb.adjust(2); return kb.as_markup()

def kb_settings():
    kb=InlineKeyboardBuilder()
    for t,d in [('🎯 Изменить целевой вес','set_target'),('🍽 Изменить лимит калорий','set_calories'),('⏰ Изменить время напоминания','set_reminder'),('⬅️ Назад','back_menu')]: kb.button(text=t, callback_data=d)
    kb.adjust(1); return kb.as_markup()

async def ask_reg(message, uid):
    st = states[uid]; step = st['step']
    prompts = {'reg_start':'1/7 Введи текущий вес, например: 101.3','reg_target':'2/7 Введи целевой вес, например: 80','reg_height':'3/7 Введи рост в см, например: 183','reg_age':'4/7 Введи возраст, например: 33','reg_calories':'6/7 Введи дневной лимит калорий, например: 1600','reg_reminder':'7/7 Введи время ежедневного напоминания, например: 20:00'}
    if step in prompts: await message.answer(prompts[step], reply_markup=kb_cancel())
    elif step == 'reg_gender': await message.answer('5/7 Выбери пол:', reply_markup=kb_gender())
    elif step == 'reg_activity': await message.answer('Выбери активность без тренировок:', reply_markup=kb_activity())

@dp.message(CommandStart())
async def cmd_start(message: Message):
    uid=message.from_user.id; ensure_user(uid)
    if registered(uid):
        await message.answer('Меню бота:', reply_markup=kb_main()); return
    states[uid]={'flow':'registration','step':'reg_start','data':{}}
    await message.answer('Привет! Настроим цель и запустим фитнес-бота.')
    await ask_reg(message, uid)

@dp.message(Command('menu'))
async def cmd_menu(message: Message):
    await message.answer('Главное меню:', reply_markup=kb_main())

@dp.message(Command('backup'))
async def cmd_backup(message: Message):
    await send_backup(message)

@dp.callback_query(F.data == 'cancel')
async def cancel(c: CallbackQuery):
    states.pop(c.from_user.id, None); await c.message.answer('Ок, отменил.', reply_markup=kb_main()); await c.answer()

@dp.callback_query(F.data == 'back_menu')
async def back_menu(c: CallbackQuery):
    await c.message.answer('Главное меню:', reply_markup=kb_main()); await c.answer()

@dp.callback_query(F.data.startswith('gender:'))
async def set_gender(c: CallbackQuery):
    st=states.get(c.from_user.id)
    if st and st.get('flow')=='registration':
        st['data']['gender']=c.data.split(':',1)[1]; st['step']='reg_activity'; await c.message.answer('Выбери активность без тренировок:', reply_markup=kb_activity())
    await c.answer()

@dp.callback_query(F.data.startswith('activity:'))
async def set_activity(c: CallbackQuery):
    st=states.get(c.from_user.id)
    if st and st.get('flow')=='registration':
        st['data']['activity_level']=c.data.split(':',1)[1]; st['step']='reg_calories'; await c.message.answer('6/7 Введи дневной лимит калорий, например: 1600', reply_markup=kb_cancel())
    await c.answer()

@dp.callback_query(F.data == 'daily_report_start')
async def daily_start(c: CallbackQuery):
    states[c.from_user.id]={'flow':'daily','step':'food_choice','data':{}}
    await c.message.answer('📝 Отчет за день\n\nТы считал калории сегодня?', reply_markup=kb_food_choice()); await c.answer()

@dp.callback_query(F.data == 'daily_food_manual')
async def daily_food_manual(c: CallbackQuery):
    st=states.get(c.from_user.id)
    if not st or st.get('flow')!='daily': await c.answer(); return
    st['step']='food'; st['data']['food_tracked']=1
    await c.message.answer('Сколько калорий съел сегодня?', reply_markup=kb_cancel()); await c.answer()

@dp.callback_query(F.data == 'daily_food_skip')
async def daily_food_skip(c: CallbackQuery):
    st=states.get(c.from_user.id)
    if not st or st.get('flow')!='daily': await c.answer(); return
    st['data']['food']=0; st['data']['food_tracked']=0; st['step']='count'
    await c.message.answer('Ок, отметим день как «калории не считал».\n\nСколько тренировок было сегодня?', reply_markup=kb_count()); await c.answer()

@dp.callback_query(F.data.startswith('daily_count:'))
async def daily_count(c: CallbackQuery):
    st=states.get(c.from_user.id)
    if not st or st.get('flow')!='daily': await c.answer(); return
    count=int(c.data.split(':')[1]); st['data']['count']=count; st['data']['workouts']=[]; st['data']['idx']=1
    if count==0:
        await save_daily(c.from_user.id, c.message); await c.answer(); return
    st['step']='workout_type'; await c.message.answer(f'Выбери тип тренировки 1 из {count}:', reply_markup=kb_workout('daily_workout')); await c.answer()

@dp.callback_query(F.data.startswith('daily_workout:'))
async def daily_workout(c: CallbackQuery):
    st=states.get(c.from_user.id)
    if not st or st.get('flow')!='daily': await c.answer(); return
    st['data']['current_type']=c.data.split(':',1)[1]; st['step']='workout_calories'
    await c.message.answer(f"🔥 Тренировка {st['data']['idx']} из {st['data']['count']}: сколько калорий сжег?"); await c.answer()

@dp.callback_query(F.data == 'dashboard')
async def dashboard(c: CallbackQuery): await send_dashboard(c.from_user.id, c.message); await c.answer()
@dp.callback_query(F.data == 'goal_forecast')
async def goal_forecast(c: CallbackQuery): await c.message.answer('📈 <b>Прогноз цели</b>\n\n'+forecast(c.from_user.id, user(c.from_user.id))); await c.answer()
@dp.callback_query(F.data == 'discipline_calendar')
async def cal(c: CallbackQuery): await c.message.answer('📅 <b>Календарь дисциплины</b>\n\n'+discipline(c.from_user.id, user(c.from_user.id))); await c.answer()
@dp.callback_query(F.data == 'backup')
async def backup_cb(c: CallbackQuery): await send_backup(c.message); await c.answer()
@dp.callback_query(F.data == 'settings')
async def settings(c: CallbackQuery):
    u=user(c.from_user.id)
    await c.message.answer(f"⚙️ <b>Настройки</b>\n\n🎯 Целевой вес: {u['target_weight']} кг\n🍽 Лимит: {u['daily_calorie_goal']} ккал\n⏰ Напоминание: {u['daily_reminder_time']}", reply_markup=kb_settings()); await c.answer()
@dp.callback_query(F.data == 'set_target')
async def set_target(c): states[c.from_user.id]={'flow':'settings','step':'target'}; await c.message.answer('Введи новый целевой вес:', reply_markup=kb_cancel()); await c.answer()
@dp.callback_query(F.data == 'set_calories')
async def set_calories(c): states[c.from_user.id]={'flow':'settings','step':'calories'}; await c.message.answer('Введи новый лимит калорий:', reply_markup=kb_cancel()); await c.answer()
@dp.callback_query(F.data == 'set_reminder')
async def set_reminder(c): states[c.from_user.id]={'flow':'settings','step':'reminder'}; await c.message.answer('Введи время напоминания HH:MM:', reply_markup=kb_cancel()); await c.answer()

@dp.callback_query(F.data == 'plan_today')
async def plan(c: CallbackQuery):
    u=user(c.from_user.id); t=training_targets(u); gp=goal_progress(u)
    await c.message.answer(f"🔥 <b>План на сегодня</b>\n\n🍽 Лимит: <b>{u['daily_calorie_goal']} ккал</b>\n📌 Минимум: <b>{t['min_low']}–{t['min_high']} ккал</b>\n🏆 Идеально: <b>{t['ideal_low']}–{t['ideal_high']} ккал</b>\n🔥 2 тренировки: <b>{t['double_low']}–{t['double_high']} ккал</b>\n\n🎯 Прогресс:\n{bar(gp['percent'])}\nСброшено: {gp['lost']:.1f} кг\nОсталось: {gp['left']:.1f} кг"); await c.answer()
@dp.callback_query(F.data == 'today_report')
async def today_report(c): await send_today_report(c.from_user.id, c.message); await c.answer()
@dp.callback_query(F.data == 'add_food')
async def add_food(c): states[c.from_user.id]={'flow':'food'}; await c.message.answer('Сколько калорий съел сегодня?', reply_markup=kb_cancel()); await c.answer()
@dp.callback_query(F.data == 'add_workout')
async def add_workout(c): states[c.from_user.id]={'flow':'single_workout','step':'type'}; await c.message.answer('Выбери тип тренировки:', reply_markup=kb_workout('single_workout')); await c.answer()
@dp.callback_query(F.data.startswith('single_workout:'))
async def single_workout_type(c): states[c.from_user.id]={'flow':'single_workout','step':'calories','type':c.data.split(':',1)[1]}; await c.message.answer('Сколько калорий сжег?', reply_markup=kb_cancel()); await c.answer()
@dp.callback_query(F.data == 'add_weight')
async def add_weight(c): states[c.from_user.id]={'flow':'weight'}; await c.message.answer('Введи текущий вес:', reply_markup=kb_cancel()); await c.answer()
@dp.callback_query(F.data == 'add_measurements')
async def add_measurements(c): states[c.from_user.id]={'flow':'measure','step':'neck_cm','data':{}}; await c.message.answer('📏 Введи шею в см:', reply_markup=kb_cancel()); await c.answer()
@dp.callback_query(F.data == 'goal')
async def goal(c):
    u=user(c.from_user.id); gp=goal_progress(u)
    await c.message.answer(f"🎯 <b>Моя цель</b>\n\nСтарт: {u['start_weight']} кг\nСейчас: {u['current_weight']} кг\nЦель: {u['target_weight']} кг\n\n{bar(gp['percent'])}\nСброшено: {gp['lost']:.1f} кг\nОсталось: {gp['left']:.1f} кг"); await c.answer()
@dp.callback_query(F.data == 'panic')
async def panic(c):
    u=user(c.from_user.id); gp=goal_progress(u)
    await c.message.answer(f"😵 <b>Стоп. Не ломай день полностью.</b>\n\n🎯 Прогресс: {gp['percent']:.1f}%\n✅ Уже сброшено: {gp['lost']:.1f} кг\n📌 Осталось: {gp['left']:.1f} кг\n\n1. Выпей воды.\n2. Подожди 10 минут.\n3. Если голод реальный — выбери белковый перекус.\n4. Просто вернись в план."); await c.answer()
@dp.callback_query(F.data == 'weekly_history')
async def weekly(c):
    with db() as con: rows=con.execute('SELECT summary_text FROM weekly_summaries WHERE telegram_id=? ORDER BY week_start DESC LIMIT 5',(c.from_user.id,)).fetchall()
    await c.message.answer('Истории недель пока нет.' if not rows else '🗂 <b>История недель</b>\n\n'+'\n\n'.join(r['summary_text'] for r in rows)); await c.answer()
@dp.callback_query(F.data == 'monthly_history')
async def months(c):
    with db() as con: rows=con.execute('SELECT report_text FROM monthly_reports WHERE telegram_id=? ORDER BY report_month DESC LIMIT 3',(c.from_user.id,)).fetchall()
    if not rows: await c.message.answer('Истории месяцев пока нет.')
    else:
        for r in rows: await c.message.answer(r['report_text'])
    await c.answer()
@dp.callback_query(F.data == 'month_report_start')
async def month_report(c): await generate_month_report(c.from_user.id, c.message); await c.answer()
@dp.callback_query(F.data == 'help')
async def help_cb(c): await c.message.answer('ℹ️ /menu — меню\n/backup — скачать базу\n\nОсновной сценарий: 📝 Внести отчет за день.'); await c.answer()

@dp.message()
async def text_handler(message: Message):
    uid=message.from_user.id; ensure_user(uid); st=states.get(uid)
    if not st:
        await message.answer('Выбери действие:', reply_markup=kb_main()); return
    try:
        flow=st.get('flow'); txt=message.text or ''
        if flow=='registration': await handle_reg(message, st, txt)
        elif flow=='daily': await handle_daily(message, st, txt)
        elif flow=='food': await save_food(uid, inum(txt), 1, 1); states.pop(uid,None); await message.answer('✅ Калории сохранены.', reply_markup=kb_main())
        elif flow=='single_workout': await save_workout(uid, st['type'], inum(txt)); states.pop(uid,None); await message.answer('✅ Тренировка сохранена.', reply_markup=kb_main())
        elif flow=='weight': await save_weight(uid, fnum(txt)); states.pop(uid,None); await message.answer('✅ Вес сохранен.', reply_markup=kb_main())
        elif flow=='measure': await handle_measure(message, st, txt)
        elif flow=='settings': await handle_settings(message, st, txt)
    except ValueError:
        await message.answer('Введи число в правильном формате. Например: 1600 или 98.4')

async def handle_reg(message, st, txt):
    uid=message.from_user.id; data=st['data']; step=st['step']
    if step=='reg_start': data['start_weight']=fnum(txt); st['step']='reg_target'
    elif step=='reg_target': data['target_weight']=fnum(txt); st['step']='reg_height'
    elif step=='reg_height': data['height_cm']=fnum(txt); st['step']='reg_age'
    elif step=='reg_age': data['age']=inum(txt); st['step']='reg_gender'; await ask_reg(message, uid); return
    elif step=='reg_calories': data['daily_calorie_goal']=inum(txt); st['step']='reg_reminder'
    elif step=='reg_reminder':
        if ':' not in txt: await message.answer('Формат HH:MM, например 20:00'); return
        data['daily_reminder_time']=txt
        with db() as con:
            con.execute('''UPDATE users SET start_weight=?, current_weight=?, target_weight=?, height_cm=?, age=?, gender=?, activity_level=?, daily_calorie_goal=?, daily_reminder_time=?, is_registered=1 WHERE telegram_id=?''', (data['start_weight'],data['start_weight'],data['target_weight'],data['height_cm'],data['age'],data['gender'],data['activity_level'],data['daily_calorie_goal'],data['daily_reminder_time'],uid))
            con.execute('INSERT INTO weight_logs(telegram_id, weight_date, weight, created_at) VALUES(?,?,?,?)',(uid,today(),data['start_weight'],now_iso()))
        states.pop(uid,None); await message.answer('✅ Настройка завершена.', reply_markup=kb_main()); return
    await ask_reg(message, uid)

async def handle_daily(message, st, txt):
    uid=message.from_user.id; data=st['data']
    if st['step']=='food':
        data['food']=inum(txt); st['step']='count'; await message.answer('Сколько тренировок было сегодня?', reply_markup=kb_count()); return
    if st['step']=='workout_calories':
        data['workouts'].append((data['current_type'], inum(txt)))
        if data['idx'] < data['count']:
            data['idx'] += 1; st['step']='workout_type'; await message.answer(f"Выбери тип тренировки {data['idx']} из {data['count']}:", reply_markup=kb_workout('daily_workout')); return
        await save_daily(uid, message)

async def save_food(uid, food, food_tracked=1, workout_tracked=1):
    with db() as con:
        con.execute('''INSERT INTO daily_reports(telegram_id, report_date, food_calories, food_tracked, workout_tracked, created_at)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(telegram_id, report_date)
                       DO UPDATE SET food_calories=excluded.food_calories, food_tracked=excluded.food_tracked, workout_tracked=excluded.workout_tracked''',
                    (uid,today(),food,food_tracked,workout_tracked,now_iso()))
async def save_workout(uid, wtype, cal):
    with db() as con: con.execute('INSERT INTO workouts(telegram_id, workout_date, workout_type, calories_burned, created_at) VALUES(?,?,?,?,?)',(uid,today(),wtype,cal,now_iso()))
async def save_weight(uid, weight):
    with db() as con:
        con.execute('UPDATE users SET current_weight=? WHERE telegram_id=?',(weight,uid))
        con.execute('INSERT INTO weight_logs(telegram_id, weight_date, weight, created_at) VALUES(?,?,?,?)',(uid,today(),weight,now_iso()))

async def save_daily(uid, message):
    st=states[uid]; data=st['data']; await save_food(uid, data['food'], data.get('food_tracked',1), 1)
    with db() as con: con.execute('DELETE FROM workouts WHERE telegram_id=? AND workout_date=?',(uid,today()))
    for typ, cal in data.get('workouts',[]): await save_workout(uid, typ, cal)
    states.pop(uid,None); await message.answer('✅ Отчет за день сохранен.'); await send_today_report(uid, message)

async def handle_measure(message, st, txt):
    uid=message.from_user.id; data=st['data']; step=st['step']; data[step]=fnum(txt)
    seq=[('neck_cm','груди'),('chest_cm','живота'),('belly_cm','таза'),('hips_cm','бедра'),('thigh_cm','икр'),('calves_cm','предплечья'),('forearms_cm',None)]
    idx=[x[0] for x in seq].index(step)
    if seq[idx][1]:
        st['step']=seq[idx+1][0]; await message.answer(f"📏 Введи обхват {seq[idx][1]} в см:", reply_markup=kb_cancel()); return
    with db() as con:
        con.execute('''INSERT INTO body_measurements(telegram_id, measurement_date, neck_cm, chest_cm, belly_cm, hips_cm, thigh_cm, calves_cm, forearms_cm, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',(uid,today(),data.get('neck_cm'),data.get('chest_cm'),data.get('belly_cm'),data.get('hips_cm'),data.get('thigh_cm'),data.get('calves_cm'),data.get('forearms_cm'),now_iso()))
    states.pop(uid,None); await message.answer('✅ Замеры сохранены.', reply_markup=kb_main())

async def handle_settings(message, st, txt):
    uid=message.from_user.id; step=st['step']
    with db() as con:
        if step=='target': con.execute('UPDATE users SET target_weight=? WHERE telegram_id=?',(fnum(txt),uid)); ans='✅ Целевой вес обновлен.'
        elif step=='calories': con.execute('UPDATE users SET daily_calorie_goal=? WHERE telegram_id=?',(inum(txt),uid)); ans='✅ Лимит калорий обновлен.'
        else:
            if ':' not in txt: await message.answer('Формат HH:MM, например 20:00'); return
            con.execute('UPDATE users SET daily_reminder_time=? WHERE telegram_id=?',(txt,uid)); ans='✅ Время напоминания обновлено.'
    states.pop(uid,None); await message.answer(ans, reply_markup=kb_main())

async def send_today_report(uid, msg):
    u=user(uid); dd=day_data(uid); t=training_targets(u); score,label=day_index(u,dd['food'],dd['burned'],dd.get('food_tracked',1))
    if dd.get('food_tracked',1):
        food_p=dd['food']/(u['daily_calorie_goal'] or 1)*100
        food_block=f"🍽 Калории: {dd['food']} / {u['daily_calorie_goal']} ккал\n{bar(food_p)}\n{food_status(dd['food'],u['daily_calorie_goal'])}"
    else:
        food_block='🍽 Калории: не считал сегодня\n⚪ День учтен без калорий'
    work_p=dd['burned']/(t['ideal_low'] or 1)*100
    wlines='\n'.join(f"— {w['workout_type']}: {w['calories_burned']} ккал" for w in dd['workouts']) or '— не было тренировки'
    net_text = f"{dd['net']} ккал" if dd.get('food_tracked',1) else 'не считается без калорий'
    await msg.answer(f"📊 <b>Отчет за сегодня</b>\n\n❤️ Индекс дня: <b>{score}/100</b> {label}\n{bar(score)}\n\n{food_block}\n\n🔥 Тренировки: {dd['burned']} / {t['ideal_low']} ккал\n{bar(work_p)}\n{workout_status(dd['burned'],t['ideal_low'])}\n\n⚡ Чистые калории: {net_text}\n\n🏋️ Тренировки:\n{wlines}")

async def send_dashboard(uid, msg):
    u=user(uid); gp=goal_progress(u); dd=day_data(uid); score,label=day_index(u,dd['food'],dd['burned'],dd.get('food_tracked',1))
    food_text = f"{dd['food']} ккал" if dd.get('food_tracked',1) else 'не считал'
    net_text = f"{dd['net']} ккал" if dd.get('food_tracked',1) else 'не считается'
    await msg.answer(f"📊 <b>Мини-дашборд</b>\n\n⚖️ Вес: {u['current_weight']} кг\n🎯 Цель: {u['target_weight']} кг\n📈 Прогресс:\n{bar(gp['percent'])}\n✅ Сброшено: {gp['lost']:.1f} кг\n📌 Осталось: {gp['left']:.1f} кг\n\n🍽 Сегодня: {food_text}\n🔥 Сожжено: {dd['burned']} ккал\n⚡ Чистые: {net_text}\n❤️ Индекс: {score}/100 {label}\n\n📈 {forecast(uid,u)}")

async def send_backup(msg):
    if not os.path.exists(DB_PATH): await msg.answer('База данных пока не создана.'); return
    await msg.answer_document(FSInputFile(DB_PATH, filename=f"fitness_bot_backup_{now().strftime('%Y%m%d_%H%M')}.db"), caption='💾 Backup базы данных')

def build_week(uid, start, end):
    with db() as con:
        reps=con.execute('SELECT food_calories, food_tracked FROM daily_reports WHERE telegram_id=? AND report_date BETWEEN ? AND ?',(uid,start.isoformat(),end.isoformat())).fetchall()
        wos=con.execute('SELECT calories_burned FROM workouts WHERE telegram_id=? AND workout_date BETWEEN ? AND ?',(uid,start.isoformat(),end.isoformat())).fetchall()
    tracked=[r for r in reps if ('food_tracked' not in r.keys()) or r['food_tracked']==1]
    untracked_days=len(reps)-len(tracked)
    food=sum(r['food_calories'] for r in tracked); burned=sum(w['calories_burned'] for w in wos); net=food-burned
    text=f"📌 <b>Итог недели</b>\n{start.isoformat()} — {end.isoformat()}\n\n🍽 Всего съедено: {food} ккал\n❔ Дней без подсчета калорий: {untracked_days}\n🔥 Всего сожжено: {burned} ккал\n⚡ Чистые калории: {net} ккал"
    with db() as con: con.execute('INSERT INTO weekly_summaries(telegram_id,week_start,week_end,total_food_calories,total_workout_calories,net_calories,summary_text,created_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(telegram_id,week_start,week_end) DO UPDATE SET total_food_calories=excluded.total_food_calories,total_workout_calories=excluded.total_workout_calories,net_calories=excluded.net_calories,summary_text=excluded.summary_text',(uid,start.isoformat(),end.isoformat(),food,burned,net,text,now_iso()))
    return text

async def generate_month_report(uid, msg):
    start=now().date().replace(day=1); end=now().date()
    with db() as con:
        reps=con.execute('SELECT report_date, food_calories, food_tracked FROM daily_reports WHERE telegram_id=? AND report_date BETWEEN ? AND ?',(uid,start.isoformat(),end.isoformat())).fetchall()
        wos=con.execute('SELECT workout_date, workout_type, calories_burned FROM workouts WHERE telegram_id=? AND workout_date BETWEEN ? AND ?',(uid,start.isoformat(),end.isoformat())).fetchall()
        weights=con.execute('SELECT weight FROM weight_logs WHERE telegram_id=? AND weight_date BETWEEN ? AND ? ORDER BY weight_date ASC',(uid,start.isoformat(),end.isoformat())).fetchall()
        meas=con.execute('SELECT * FROM body_measurements WHERE telegram_id=? AND measurement_date BETWEEN ? AND ? ORDER BY measurement_date ASC',(uid,start.isoformat(),end.isoformat())).fetchall()
    if not weights: await msg.answer('⚠️ За месяц нет веса. Внеси вес.'); return
    if not meas: await msg.answer('⚠️ За месяц нет замеров. Внеси замеры.'); return
    tracked=[r for r in reps if ('food_tracked' not in r.keys()) or r['food_tracked']==1]
    untracked_days=len(reps)-len(tracked)
    report_dates={r['report_date'] for r in reps}
    workout_dates={w['workout_date'] for w in wos}
    no_workout_days=sum(1 for d in report_dates if d not in workout_dates)
    food=sum(r['food_calories'] for r in tracked); burned=sum(w['calories_burned'] for w in wos); net=food-burned; days=len(tracked) or 1
    u=user(uid); gp=goal_progress(u); weight_delta=weights[-1]['weight']-weights[0]['weight']
    report=f"📅 <b>Отчет за месяц {month_key()}</b>\n\n🍽 Съедено: {food} ккал\nСреднее по дням с подсчетом: {round(food/days)} ккал/день\nДней с отчетом: {len(reps)}\n❔ Дней без подсчета калорий: {untracked_days}\n\n🔥 Сожжено: {burned} ккал\nТренировок: {len(wos)}\n🚫 Дней без тренировки: {no_workout_days}\n\n⚡ Чистые калории: {net} ккал\n\n⚖️ Вес: {weights[0]['weight']:g} → {weights[-1]['weight']:g} кг\nИзменение: {weight_delta:+.1f} кг\n\n🎯 Прогресс:\n{bar(gp['percent'])}\nСброшено всего: {gp['lost']:.1f} кг\nОсталось: {gp['left']:.1f} кг"
    with db() as con: con.execute('INSERT INTO monthly_reports(telegram_id,report_month,report_text,created_at) VALUES(?,?,?,?) ON CONFLICT(telegram_id,report_month) DO UPDATE SET report_text=excluded.report_text',(uid,month_key(),report,now_iso()))
    await msg.answer(report)

async def daily_job():
    with db() as con: users=con.execute('SELECT telegram_id FROM users WHERE is_registered=1').fetchall()
    for r in users:
        try: await bot.send_message(r['telegram_id'], '⏰ Время внести отчет за день.', reply_markup=kb_main())
        except Exception: pass
async def week_job():
    end=now().date(); start=end-timedelta(days=6)
    with db() as con: users=con.execute('SELECT telegram_id FROM users WHERE is_registered=1').fetchall()
    for r in users:
        try: await bot.send_message(r['telegram_id'], build_week(r['telegram_id'],start,end))
        except Exception: pass
async def month_job():
    with db() as con: users=con.execute('SELECT telegram_id FROM users WHERE is_registered=1').fetchall()
    for r in users:
        try: await bot.send_message(r['telegram_id'],'📅 Новый месяц. Внеси вес и замеры, затем сформируй отчет.', reply_markup=kb_main())
        except Exception: pass

async def main():
    init_db()
    sched=AsyncIOScheduler(timezone=str(TZ))
    sched.add_job(daily_job,'cron',hour=20,minute=0)
    sched.add_job(week_job,'cron',day_of_week='sun',hour=20,minute=30)
    sched.add_job(month_job,'cron',day=1,hour=9,minute=0)
    sched.start()
    print('Fitness bot v1.1 started')
    await dp.start_polling(bot)

if __name__ == '__main__': asyncio.run(main())
