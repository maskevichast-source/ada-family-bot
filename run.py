"""Railway/GitHub start command: python run.py."""
import asyncio

def run():
    from config import validate_settings
    from services.runtime import worker_lock
    validate_settings()
    with worker_lock():
        from main import main
        asyncio.run(main())

if __name__ == "__main__":
    run()
