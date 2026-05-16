# Fitness Telegram Bot

Персональный Telegram-бот для учета калорий, тренировок, веса, замеров тела, недельных сводок и месячных отчетов.

## Что умеет

- Регистрация пользователя через вопросы
- Ежедневный ввод съеденных калорий
- Учет любых тренировок: силовая, велотренажер, футбол, бег, ходьба, плавание, бокс, йога/растяжка, другое
- Учет сожженных калорий по каждой тренировке
- Прогресс-бары по питанию, тренировкам и цели
- Ввод веса
- Ввод замеров тела: шея, грудь, живот, таз, бедро, икры, предплечья
- Короткая недельная сводка: сколько съедено, сколько сожжено, чистые калории
- Полный месячный отчет после ввода веса и замеров
- Сравнение месячного отчета с прошлым месяцем, начиная со второго месяца
- История недель и месяцев

## Файлы проекта

```text
bot.py
requirements.txt
.env.example
.gitignore
render.yaml
.github/workflows/check.yml
```

## Локальный запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

В `.env` вставить токен от BotFather:

```env
BOT_TOKEN=your_bot_token_here
BOT_TZ=Europe/Kyiv
DB_PATH=fitness_bot.db
```

Запуск:

```bash
python bot.py
```

## Запуск на Render Free

1. Создай бота через `@BotFather` и получи токен.
2. Создай репозиторий на GitHub.
3. Загрузи все файлы проекта.
4. В Render нажми `New` → `Web Service`.
5. Выбери GitHub-репозиторий.
6. Укажи:

```text
Build Command: pip install -r requirements.txt
Start Command: python bot.py
```

7. В Environment Variables добавь:

```text
BOT_TOKEN = твой токен от BotFather
BOT_TZ = Europe/Kyiv
DB_PATH = fitness_bot.db
```

8. Нажми `Create Web Service`.
9. В Telegram открой своего бота и напиши `/start`.

## Важно

На бесплатном Render сервис может засыпать. Для личного бота это нормальный старт, но напоминания могут приходить не всегда строго по времени.
