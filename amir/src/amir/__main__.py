"""Entry point: `python -m amir` runs the bot; `python -m amir --check-config` validates the config.

Exit status: 0 after a clean shutdown or a valid config, 1 when the bot stops on an error,
2 for a configuration error (including Discord IDs still unset when running the bot).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from collections.abc import Iterable
from pathlib import Path

import aiohttp
import discord

from amir import __version__
from amir.bot.client import AmirBot, build_services
from amir.bot.commands import register_amir_commands
from amir.bot.status import config_report
from amir.config.settings import ConfigError, Settings, default_config_path, load_settings
from amir.logging.setup import configure_logging
from amir.net.http import HttpFetcher
from amir.storage.db import Database

log = logging.getLogger("amir")


def _print_problems(problems: Iterable[str]) -> None:
    for problem in problems:
        print(f"config error: {problem}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="amir", description="Amir Beckett, Discord bot")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate the configuration, print a redacted report and exit",
    )
    args = parser.parse_args(argv)
    try:
        settings = load_settings(default_config_path(os.environ), os.environ)
    except ConfigError as exc:
        _print_problems(exc.problems)
        return 2
    if args.check_config:
        print(config_report(settings))
        return 0
    missing = settings.missing_runtime_ids()
    if missing:
        _print_problems(f"{name}: must be set before the bot can run" for name in missing)
        return 2
    return asyncio.run(_run(settings))


async def _run(settings: Settings) -> int:
    token = settings.token.get_secret_value()
    configure_logging(
        settings.logging.level, secrets=[token], voice_debug=settings.logging.voice_debug
    )
    log.info(
        "Amir Beckett %s starting; publishing %s",
        __version__,
        "ON" if settings.publishing.enabled else "OFF",
    )
    db: Database | None = None
    try:
        db = await Database.open(Path(settings.storage.db_path))
        await db.migrate()
    except Exception:
        log.exception("the database could not be opened or migrated")
        if db is not None:
            await db.close()
        return 1
    http: aiohttp.ClientSession | None = None
    try:
        http = aiohttp.ClientSession()
        fetcher = HttpFetcher(http, settings.http)
        bot = AmirBot(
            settings,
            lambda client: build_services(client, settings, db, http, fetcher),
            register_commands=register_amir_commands,
        )
    except Exception:
        log.exception("the bot could not be set up")
        if http is not None:
            await http.close()
        await db.close()
        return 1
    _close_on_signals(bot)
    try:
        await bot.start(token)
    except discord.LoginFailure:
        log.error("Discord rejected the token in DISCORD_TOKEN")
        return 1
    except Exception:
        log.exception("the bot stopped on an unexpected error")
        return 1
    finally:
        await bot.close()  # a no-op wait when a signal already closed it
    log.info("Amir Beckett stopped")
    return 0


def _close_on_signals(bot: AmirBot) -> None:
    loop = asyncio.get_running_loop()
    pending: set[asyncio.Task[None]] = set()

    def request_close(name: str) -> None:
        log.info("%s received; shutting down", name)
        task = loop.create_task(bot.close())
        pending.add(task)  # keep a reference until it finishes
        task.add_done_callback(pending.discard)

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, request_close, sig.name)


if __name__ == "__main__":
    raise SystemExit(main())
