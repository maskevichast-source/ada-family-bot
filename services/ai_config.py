"""Единая точка настройки модели DeepSeek.

Раньше строка "deepseek-chat" была захардкожена в трёх разных файлах
(deepseek_service.py, text_handler.py, main.py). Если DeepSeek снова
переименует или отключит модель, придётся править в одном месте —
здесь, либо просто задать переменную окружения DEEPSEEK_MODEL в
настройках хостинга (Replit Secrets и т.п.), не трогая код.

ВАЖНО: по данным пользователя, модель "deepseek-chat" могла быть
переименована в "deepseek-v4-flash" после отключения старого имени
24.07.2026. Перед деплоем стоит свериться с официальной документацией
DeepSeek (https://api-docs.deepseek.com/) и при необходимости
поменять значение по умолчанию ниже или задать DEEPSEEK_MODEL в окружении.
"""

import os

DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


