Fitness Bot PostgreSQL Safe v2.2

Безопасное обновление без сброса базы.

Добавлено:
- reconnect к PostgreSQL
- защита от ConnectionResetError
- /health для проверки бота и БД
- ежедневное напоминание в 20:00
- недельный авто-итог по воскресеньям в 20:30
- месячное напоминание 1 числа в 09:00

Важно:
- reset_database.sql НЕ нужен для этого обновления
- текущие данные в PostgreSQL сохраняются
- заменить только bot.py и requirements.txt
