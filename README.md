# Ада — GitHub → Railway, групповые напоминания

Новый ZIP **заменяет предыдущую сборку с личной доставкой**.
Все напоминания приходят в `FAMILY_CHAT_ID`; Влад, Диана или оба — адресаты
в тексте сообщения. «Обоим» = одно сообщение с двумя упоминаниями.

## Существующий способ работы сохранён
GitHub-репозиторий → существующий Railway service. Переезд не нужен.
В комплекте `railway.json`, Dockerfile и команда старта `python run.py`.
Бот использует long polling, а не HTTP-сайт.

- [Обновление на Railway](RAILWAY_DEPLOY_RU.md)
- [Сверка функций и циклов](FEATURES_RU.md)
- [Команды](COMMANDS_RU.md)
- [Аудит и ограничения](AUDIT_RU.md)
- `TEST_RESULTS.txt` — результат проверок.

## Не потерять данные
Не заменяйте живую Google-таблицу старым XLSX.
Подключите **Railway Volume `/app/data`**, задайте:
```
ADA_STATE_DIR=/app/data
CHAT_HISTORY_FILE=/app/data/chat_history.json
```
Переменные с путём сами по себе Volume НЕ создают.
Одна replica. Старый worker остановить до переключения.
Секреты остаются в Railway Variables, не загружаются в GitHub.

Сохранены обе стартовые функции, семь циклов, финансовые и бытовые функции,
контекст и тон Ады. Полная сверка — FEATURES_RU.md.
Миграция добавочная: Reminders H:J, Subscriptions H, Trips E, новый Debts.
Transactions не очищается; ID, суммы и даты не переписываются миграцией.

## Проверки
134 локальных тестовых случая, компиляция всех Python-файлов.
Сформированы PNG, Excel и двухстраничный PDF. PDF и дашборд визуально проверены
на присланном снимке через имитацию Google Sheets.

**Живые API, полная установка зависимостей, Docker build и Railway deployment
здесь не выполнялись.** В тестах Telegram/Google/LLM подменены.
Python тестирования 3.13.5; целевой Docker/CI — 3.11.
```
pip install -r requirements-dev.txt
pip check
python -m compileall -q .
python -m pytest -q
```
Старые заметки в `docs/archive/` — только история, не инструкция этого релиза.
