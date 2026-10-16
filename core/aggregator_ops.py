"""Accès neutre aux opérations optionnelles d'un agrégateur
(`aggregators.<nom>.operations.<fonction>`). Un agrégateur qui ne propose pas
l'opération -> 400 `operation_unsupported`. Panne amont -> 503."""

import importlib

from fastapi import HTTPException

from core.upstream_errors import UPSTREAM_MESSAGES


def get(aggregator: str, name: str):
    """Fonction `name` de l'agrégateur, ou lève 400."""
    try:
        mod = importlib.import_module(f"aggregators.{aggregator}.operations")
        return getattr(mod, name)
    except (ModuleNotFoundError, AttributeError):
        raise HTTPException(400, {
            "error": "operation_unsupported",
            "message": f"L'agrégateur '{aggregator}' ne propose pas cette opération.",
            "aggregator": aggregator})


async def call(aggregator: str, name: str, *args, **kwargs):
    """Appelle l'opération ; une panne amont (exception portant `.code`) -> 503."""
    fn = get(aggregator, name)
    try:
        return await fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "code", None)
        if code in UPSTREAM_MESSAGES:
            raise HTTPException(503, {"code": code, "message": UPSTREAM_MESSAGES[code]})
        raise
