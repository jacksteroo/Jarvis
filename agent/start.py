# ruff: noqa: E402

import asyncio
import os
import signal
import sys
import structlog
import uvicorn

# Ensure the repo root (parent of this file's directory) is on sys.path so that
# direct Python imports of `subsystems.*` work regardless of how the process is launched.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent.config import Settings
from agent.db import init_db
from agent.core import PepperCore
from agent.scheduler import PepperScheduler
from agent.skills import sync_repo_skills_to_user_dir
from agent.telegram_bot import JARViSTelegramBot
from agent.main import app

logger = structlog.get_logger()

_shutdown_event = asyncio.Event()


def _handle_signal(sig, frame):
    logger.info("shutdown_signal_received", signal=sig)
    _shutdown_event.set()


async def main():
    config = Settings()

    import logging
    log_level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)

    # Shared processors for both structlog-native and stdlib-bridged records
    shared_processors = [
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_log_level,
    ]

    # Configure structlog — always write to stdout (Docker/systemd capture it)
    renderer = structlog.dev.ConsoleRenderer(
        exception_formatter=structlog.dev.plain_traceback
    )

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        processors=shared_processors + [renderer],
    )

    # Route stdlib logging through structlog to stdout
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=shared_processors + [renderer],
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)

    # Optionally also mirror logs to a file. In Docker, bind-mount ./logs to
    # preserve them on the host while keeping stdout as the primary stream.
    if config.LOG_TO_FILE:
        repo_root = os.path.dirname(os.path.dirname(__file__))
        log_path = config.LOG_FILE_PATH
        if not os.path.isabs(log_path):
            log_path = os.path.join(repo_root, log_path)
        log_dir = os.path.dirname(log_path)
        os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.FileHandler(log_path)
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processors=shared_processors + [renderer],
            )
        )
        root.addHandler(file_handler)

    # Silence noisy third-party libraries
    for noisy in ("httpcore", "httpx", "telegram", "hpack", "asyncio",
                  "apscheduler", "tzlocal", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logger.info("pepper_starting", port=config.PORT, local_model=config.DEFAULT_LOCAL_MODEL)

    # Sync repo skills → ~/.pepper/skills before anything reads them. The repo
    # is the deployment artifact; ~/.pepper is the runtime location bind-mounted
    # into the container. This makes `git pull && docker compose restart`
    # sufficient to ship skill changes.
    sync_repo_skills_to_user_dir()

    # Init database
    await init_db(config)
    logger.info("database_initialized")

    # Create DB session factory for passing to components
    from contextlib import asynccontextmanager
    from sqlalchemy.ext.asyncio import AsyncSession
    from agent.db import get_engine

    @asynccontextmanager
    async def session_factory():
        async with AsyncSession(get_engine()) as session:
            yield session

    # Create Pepper core
    pepper = PepperCore(config, db_session_factory=session_factory)
    await pepper.initialize()
    logger.info("pepper_core_initialized")

    # Create scheduler
    scheduler = PepperScheduler(pepper, config)
    pepper._scheduler = scheduler

    # Create Telegram bot (if configured)
    bot = None
    if config.TELEGRAM_BOT_TOKEN:
        bot = JARViSTelegramBot(config.TELEGRAM_BOT_TOKEN, pepper, config)
        scheduler.bot = bot
        # Pending outbound drafts surface as Telegram messages with inline
        # ✅ / ✏️ / ❌ buttons. The notifier fires from PendingActionsQueue.queue().
        pepper.pending_actions.set_notifier(bot.notify_pending_action)
        logger.info("telegram_bot_configured")
    else:
        logger.info("telegram_bot_skipped", reason="TELEGRAM_BOT_TOKEN not set")

    # Make scheduler and pepper available to FastAPI app via app.state
    app.state.pepper = pepper
    app.state.scheduler = scheduler
    app.state.bot = bot

    # Start scheduler
    scheduler.start()

    # Start Telegram bot in background
    bot_task = None
    if bot:
        bot_task = asyncio.create_task(bot.start())

    # Signal handling
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Start uvicorn
    uv_config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=config.PORT,
        log_level=config.LOG_LEVEL.lower(),
        lifespan="off",  # We manage lifespan manually here
    )
    server = uvicorn.Server(uv_config)
    server_task = asyncio.create_task(server.serve())

    logger.info("pepper_running", port=config.PORT, telegram=bool(bot))

    # Wait for shutdown signal
    await _shutdown_event.wait()
    logger.info("shutting_down")

    # Graceful shutdown
    server.should_exit = True
    await server_task

    if bot_task:
        await bot.stop()
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass

    scheduler.stop()
    logger.info("pepper_stopped")


if __name__ == "__main__":
    asyncio.run(main())
