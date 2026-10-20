"""Lance au démarrage les tâches de fond propres à chaque agrégateur : appelle
`aggregators.<nom>.background.start()` s'il existe (neutre : aucun nom en dur)."""

import importlib
import logging

from core import registry

log = logging.getLogger("ai_browser2")


def start_all() -> list:
    tasks = []
    for name in registry.names():
        try:
            mod = importlib.import_module(f"aggregators.{name}.background")
        except ModuleNotFoundError:
            continue
        tasks += mod.start() or []
        log.info("Tâches de fond démarrées : %s", name)
    return tasks
