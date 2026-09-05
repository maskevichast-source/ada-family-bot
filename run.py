"""Запуск Family Finance Bot (Ада)."""
import asyncio
import logging
import os

def run():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    from config import validate_settings
    from services.runtime import worker_lock

    validate_settings()
    with worker_lock():
        from main import main
        asyncio.run(main())

if __name__ == "__main__":
    run()
