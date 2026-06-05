"""Socket.IO temps réel : push du verdict vers l'app du client en direct.

Le client se connecte en fournissant sa clé API (`auth={"token": "sk_…"}`). On
l'authentifie avec la MÊME résolution que le REST (core.auth) et on le place dans
la room `app:<id>` (+ `dev:<id>`). Quand une transaction settle, notifications.py
émet l'événement `transaction.update` dans la room de l'app — chaque client ne
reçoit donc que ses propres transactions.

Le serveur Socket.IO est monté en ASGI par-dessus FastAPI (cf. core/server.py).
"""

import logging

import socketio

from core import auth

log = logging.getLogger("ai_browser2")

# CORS ouvert : l'auth se fait par clé API à la connexion, pas par origine.
sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")


def mount(asgi_app):
    """Enveloppe l'app FastAPI dans l'app ASGI Socket.IO (montée sur /socket.io)."""
    return socketio.ASGIApp(sio, other_asgi_app=asgi_app)


@sio.event
async def connect(sid, environ, auth_data):
    """Authentifie une connexion socket par clé API. Refuse si invalide/inactive."""
    token = (auth_data or {}).get("token") if isinstance(auth_data, dict) else None
    if not token:
        raise socketio.exceptions.ConnectionRefusedError("clé API requise (auth.token)")
    try:
        ctx = await auth.require_api_key(authorization=f"Bearer {token}")
    except Exception:  # HTTPException (401/403) ou autre -> refus
        raise socketio.exceptions.ConnectionRefusedError("clé API invalide ou inactive")

    await sio.enter_room(sid, f"app:{ctx.app_id}")
    await sio.enter_room(sid, f"dev:{ctx.developer_id}")
    await sio.save_session(sid, {"app_id": ctx.app_id, "developer_id": ctx.developer_id,
                                 "env": ctx.env})
    log.info("socket connecté sid=%s app=%s dev=%s", sid, ctx.app_id, ctx.developer_id)


@sio.event
async def disconnect(sid):
    log.info("socket déconnecté sid=%s", sid)


async def emit_transaction_update(app_id: int, payload: dict) -> None:
    """Émet le verdict d'une transaction dans la room de l'app concernée."""
    if app_id is None:
        return
    try:
        await sio.emit("transaction.update", payload, room=f"app:{app_id}")
    except Exception as e:  # noqa: BLE001 — le temps réel ne doit jamais casser le flux
        log.warning("emit transaction.update échoué (app=%s): %s", app_id, e)
