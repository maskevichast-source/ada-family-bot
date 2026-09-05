# Деплой на существующий Railway через GitHub

Актуальная инструкция: **[RAILWAY_DEPLOY_RU.md](RAILWAY_DEPLOY_RU.md)**.

Все напоминания — в группу. Прежняя инструкция о личной доставке отменена.
Сервис не переносится: GitHub → Railway, Dockerfile, `python run.py`.
Постоянный Volume `/app/data` нужен для диалогов между redeploy.
