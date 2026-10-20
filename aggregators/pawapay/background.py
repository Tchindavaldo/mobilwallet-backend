"""Tâches de fond pawaPay, lancées au démarrage par core/background_jobs.py."""

import asyncio

from . import statements


def start() -> list[asyncio.Task]:
    return [asyncio.create_task(statements.schedule())]
