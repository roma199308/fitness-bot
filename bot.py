import os, sqlite3, asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN не найден. Добавь BOT_TOKEN в Railway Variables")
TZ = ZoneInfo(os.getenv("BOT_TZ", "Europe/Kyiv"))
DB = os.getenv("DB_PATH", "fitness_bot.db")

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
states = {}

def now(): return datetime.now(TZ)
def today(): return now().date().isoformat()
def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init_db():
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, start_weight REAL, current_weight REAL, target_weight REAL, height REAL, age INTEGER, gender TEXT, activity TEXT, calorie_goal INTEGER, registered INTEGER DEFAULT 0, created_at TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS food (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, date TEXT, kcal INTEGER, UNIQUE(user_id,date))")
        c.execute("CREATE TABLE IF NOT EXISTS workouts (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, date TEXT, type TEXT, kcal INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS weights (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, date TEXT, weight REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS measures (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, date TEXT, neck REAL, chest REAL, belly REAL, hips REAL, thigh REAL, calves REAL, forearms REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS week_history (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, text TEXT, created_at TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS month_history (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, text TEXT, created_at TEXT)")

def ensure(uid):
    with db() as c:
        if not c.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone():
            c.execute("INSERT INTO users (id, created_at) VALUES (?,?)", (uid, now().isoformat()))

def user(uid):
    ensure(uid)
    with db() as c: return c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

def fnum(s): return float(str(s).replace(",", ".").strip())
def inum(s): return int(round(fnum(s)))
def pbar(p):
    p=max(0,p); n=round(min(p,100)/10); return "█"*n + "░"*(10-n) + f" {p:.0f}%"

def targets(u):
    w=u["current_weight"] or u["start_weight"] or 100
    return (600,900,800,1200) if w>=95 else (500,800,700,1000) if w>=85 else (450,700,600,900)

def progress(u):
    sw=u["start_weight"] or 0; cw=u["current_weight"] or sw; tw=u["target_weight"] or 0
    total=sw-tw; lost=sw-cw; left=cw-tw; pct=(lost/total*100) if total>0 else 0
    return total,lost,left,max(0,min(100,pct))

def menu():
    kb=InlineKeyboardBuilder()
    for t,d in [("🔥 План",'plan'),("🍽 Калории",'food'),("🏋️ Тренировка",'workout'),("📊 Сегодня",'today'),("⚖️ Вес",'weight'),("📏 Замеры",'measure'),("🎯 Цель",'goal'),("📅 Месяц",'month'),("🗂 История недель",'weeks'),("🗂 История месяцев",'months'),("😵 Хочу сорваться",'panic')]: kb.button(text=t, callback_data=d)
    kb.adjust(2); return kb.as_markup()

def gender_kb():
    kb=InlineKeyboardBuilder(); kb.button(text="Мужской", callback_data="g:male"); kb.button(text="Женский", callback_data="g:female"); kb.adjust(2); return kb.as_markup()

def activity_kb():
    kb=InlineKeyboardBuilder();
    for x in ["Малоподвижный","Средний","Активный"]: kb.button(text=x, callback_data=f"a:{x}")
    kb.adjust(1); return kb.as_markup()

def workout_kb():
    kb=InlineKeyboardBuilder()
    for x in ["Силовая","Велотренажер","Футбол","Бег","Ходьба","Плавание","Бокс","Йога","Другое"]: kb.button(text=x, callback_data=f"wt:{x}")
    kb.adjust(2); return kb.as_markup()

async def ask_reg(m, uid):
    s=states[uid]['step']
    texts={'sw':'1/7 Текущий вес, например 101.3','tw':'2/7 Целевой вес, например 80','h':'3/7 Рост, например 183','age':'4/7 Возраст, например 33','cal':'6/7 Лимит калорий, например 1600'}
    if s=='gender': await m.answer('5/7 Выбери пол:', reply_markup=gender_kb())
    elif s=='activity': await m.answer('Выбери активность без тренировок:', reply_markup=activity_kb())
    else: await m.answer(texts[s])

@dp.message(CommandStart())
async def start(m: Message):
    uid=m.from_user.id; ensure(uid); u=user(uid)
    if u['registered']:
        await m.answer('Меню:', reply_markup=menu()); return
    states[uid]={'flow':'reg','step':'sw','data':{}}
    await m.answer('Привет! Настроим фитнес-бота.')
    await ask_reg(m,uid)

@dp.message(Command('menu'))
async def cmd_menu(m: Message): await m.answer('Меню:', reply_markup=menu())

@dp.callback_query(F.data.startswith('g:'))
async def gender(c: CallbackQuery):
    st=states.get(c.from_user.id)
    if st and st['flow']=='reg': st['data']['gender']=c.data[2:]; st['step']='activity'; await ask_reg(c.message,c.from_user.id)
    await c.answer()
@dp.callback_query(F.data.startswith('a:'))
async def activity(c: CallbackQuery):
    st=states.get(c.from_user.id)
    if st and st['flow']=='reg': st['data']['activity']=c.data[2:]; st['step']='cal'; await ask_reg(c.message,c.from_user.id)
    await c.answer()

@dp.callback_query(F.data=='plan')
async def plan(c):
    u=user(c.from_user.id); il,ih,dl,dh=targets(u); total,lost,left,p=progress(u)
    await c.message.answer(f"🔥 <b>План</b>\n🍽 Лимит: {u['calorie_goal']} ккал\n🏆 Идеально сжечь: {il}-{ih} ккал\n🔥 2 тренировки: {dl}-{dh} ккал\n\n🎯 {pbar(p)}\nСброшено: {lost:.1f}/{total:.1f} кг\nОсталось: {left:.1f} кг"); await c.answer()
@dp.callback_query(F.data=='food')
async def food(c): states[c.from_user.id]={'flow':'food'}; await c.message.answer('Сколько калорий съел сегодня?'); await c.answer()
@dp.callback_query(F.data=='workout')
async def workout(c): states[c.from_user.id]={'flow':'workout_type'}; await c.message.answer('Выбери тренировку:', reply_markup=workout_kb()); await c.answer()
@dp.callback_query(F.data.startswith('wt:'))
async def wt(c): states[c.from_user.id]={'flow':'workout','type':c.data[3:]}; await c.message.answer('Сколько ккал сжег?'); await c.answer()
@dp.callback_query(F.data=='weight')
async def weight(c): states[c.from_user.id]={'flow':'weight'}; await c.message.answer('Введи текущий вес:'); await c.answer()
@dp.callback_query(F.data=='measure')
async def measure(c): states[c.from_user.id]={'flow':'measure','step':0,'data':{}}; await c.message.answer('Шея в см:'); await c.answer()
@dp.callback_query(F.data=='goal')
async def goal(c):
    u=user(c.from_user.id); total,lost,left,p=progress(u)
    await c.message.answer(f"🎯 <b>Цель</b>\nСтарт: {u['start_weight']} кг\nСейчас: {u['current_weight']} кг\nЦель: {u['target_weight']} кг\n{pbar(p)}\nСброшено: {lost:.1f} кг\nОсталось: {left:.1f} кг"); await c.answer()
@dp.callback_query(F.data=='panic')
async def panic(c): await c.message.answer('😵 Стоп. Выпей воды, подожди 10 минут и возвращайся в план. Один момент не ломает прогресс.'); await c.answer()
@dp.callback_query(F.data=='today')
async def today_report(c):
    uid=c.from_user.id; u=user(uid); il,_,_,_=targets(u)
    with db() as x:
        r=x.execute('SELECT kcal FROM food WHERE user_id=? AND date=?',(uid,today())).fetchone(); ws=x.execute('SELECT * FROM workouts WHERE user_id=? AND date=?',(uid,today())).fetchall()
    kcal=r['kcal'] if r else 0; burn=sum(w['kcal'] for w in ws); net=kcal-burn
    await c.message.answer(f"📊 <b>Сегодня</b>\n🍽 {kcal}/{u['calorie_goal']} ккал\n{pbar(kcal/u['calorie_goal']*100 if u['calorie_goal'] else 0)}\n🔥 {burn}/{il} ккал\n{pbar(burn/il*100)}\n⚡ Чистые: {net} ккал"); await c.answer()
@dp.callback_query(F.data=='weeks')
async def weeks(c):
    with db() as x: rows=x.execute('SELECT text FROM week_history WHERE user_id=? ORDER BY id DESC LIMIT 5',(c.from_user.id,)).fetchall()
    await c.message.answer('\n\n'.join(r['text'] for r in rows) if rows else 'Истории недель пока нет.'); await c.answer()
@dp.callback_query(F.data=='months')
async def months(c):
    with db() as x: rows=x.execute('SELECT text FROM month_history WHERE user_id=? ORDER BY id DESC LIMIT 3',(c.from_user.id,)).fetchall()
    await c.message.answer('\n\n'.join(r['text'] for r in rows) if rows else 'Истории месяцев пока нет.'); await c.answer()
@dp.callback_query(F.data=='month')
async def month(c):
    uid=c.from_user.id; start=now().date().replace(day=1).isoformat(); end=today(); u=user(uid); total,lost,left,p=progress(u)
    with db() as x:
        fs=x.execute('SELECT kcal FROM food WHERE user_id=? AND date BETWEEN ? AND ?',(uid,start,end)).fetchall(); ws=x.execute('SELECT kcal FROM workouts WHERE user_id=? AND date BETWEEN ? AND ?',(uid,start,end)).fetchall(); ms=x.execute('SELECT * FROM measures WHERE user_id=? AND date BETWEEN ? AND ? ORDER BY id',(uid,start,end)).fetchall(); weights=x.execute('SELECT weight FROM weights WHERE user_id=? AND date BETWEEN ? AND ? ORDER BY id',(uid,start,end)).fetchall()
    if not weights: await c.message.answer('⚠️ Сначала внеси вес.'); await c.answer(); return
    if not ms: await c.message.answer('⚠️ Сначала внеси замеры.'); await c.answer(); return
    food=sum(r['kcal'] for r in fs); burn=sum(r['kcal'] for r in ws); txt=f"📅 <b>Отчет за месяц</b>\n🍽 Съедено: {food} ккал\n🔥 Сожжено: {burn} ккал\n⚡ Чистые: {food-burn} ккал\n⚖️ Вес: {weights[0]['weight']} → {weights[-1]['weight']} кг\n🎯 {pbar(p)}\nСброшено: {lost:.1f} кг\nОсталось: {left:.1f} кг"
    with db() as x: x.execute('INSERT INTO month_history (user_id,text,created_at) VALUES (?,?,?)',(uid,txt,now().isoformat()))
    await c.message.answer(txt); await c.answer()

@dp.message()
async def text(m: Message):
    uid=m.from_user.id; ensure(uid); st=states.get(uid)
    if not st: await m.answer('Выбери действие:', reply_markup=menu()); return
    try:
        flow=st['flow']
        if flow=='reg':
            d=st['data']; s=st['step']
            if s=='sw': d['sw']=fnum(m.text); st['step']='tw'
            elif s=='tw': d['tw']=fnum(m.text); st['step']='h'
            elif s=='h': d['h']=fnum(m.text); st['step']='age'
            elif s=='age': d['age']=inum(m.text); st['step']='gender'; await ask_reg(m,uid); return
            elif s=='cal':
                d['cal']=inum(m.text)
                with db() as x:
                    x.execute('UPDATE users SET start_weight=?,current_weight=?,target_weight=?,height=?,age=?,gender=?,activity=?,calorie_goal=?,registered=1 WHERE id=?',(d['sw'],d['sw'],d['tw'],d['h'],d['age'],d['gender'],d['activity'],d['cal'],uid))
                    x.execute('INSERT INTO weights (user_id,date,weight) VALUES (?,?,?)',(uid,today(),d['sw']))
                states.pop(uid,None); await m.answer('✅ Настройка завершена.', reply_markup=menu()); return
            await ask_reg(m,uid)
        elif flow=='food':
            kcal=inum(m.text)
            with db() as x: x.execute('INSERT INTO food (user_id,date,kcal) VALUES (?,?,?) ON CONFLICT(user_id,date) DO UPDATE SET kcal=excluded.kcal',(uid,today(),kcal))
            states.pop(uid,None); await m.answer('✅ Калории сохранены.', reply_markup=menu())
        elif flow=='workout':
            kcal=inum(m.text)
            with db() as x: x.execute('INSERT INTO workouts (user_id,date,type,kcal) VALUES (?,?,?,?)',(uid,today(),st['type'],kcal))
            states.pop(uid,None); await m.answer('✅ Тренировка сохранена.', reply_markup=menu())
        elif flow=='weight':
            w=fnum(m.text)
            with db() as x: x.execute('UPDATE users SET current_weight=? WHERE id=?',(w,uid)); x.execute('INSERT INTO weights (user_id,date,weight) VALUES (?,?,?)',(uid,today(),w))
            states.pop(uid,None); await m.answer('✅ Вес сохранен.', reply_markup=menu())
        elif flow=='measure':
            names=['neck','chest','belly','hips','thigh','calves','forearms']; labels=['Грудь','Живот','Таз','Бедро','Икры','Предплечья']
            i=st['step']; st['data'][names[i]]=fnum(m.text)
            if i<6: st['step']=i+1; await m.answer(f'{labels[i]} в см:'); return
            d=st['data']
            with db() as x: x.execute('INSERT INTO measures (user_id,date,neck,chest,belly,hips,thigh,calves,forearms) VALUES (?,?,?,?,?,?,?,?,?)',(uid,today(),d['neck'],d['chest'],d['belly'],d['hips'],d['thigh'],d['calves'],d['forearms']))
            states.pop(uid,None); await m.answer('✅ Замеры сохранены.', reply_markup=menu())
    except ValueError:
        await m.answer('Введи число. Например: 1600 или 98.4')

async def daily_job():
    with db() as x: rows=x.execute('SELECT id FROM users WHERE registered=1').fetchall()
    for r in rows:
        try: await bot.send_message(r['id'],'⏰ Время внести калории и тренировки.', reply_markup=menu())
        except Exception: pass

async def main():
    init_db(); sch=AsyncIOScheduler(timezone=str(TZ)); sch.add_job(daily_job,'cron',hour=20,minute=0); sch.start(); print('Fitness bot started'); await dp.start_polling(bot)
if __name__=='__main__': asyncio.run(main())
