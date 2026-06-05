"""MobileWallet backend — thin launcher. The app lives in core/server.py."""

import os

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7332"))
    host = os.environ.get("HOST", "0.0.0.0")
    # On sert l'app ASGI combinée (FastAPI + Socket.IO) exposée comme
    # `core.server:asgi`. RELOAD=1 active le hot-reload (string d'import requise).
    reload = os.environ.get("RELOAD", "0") == "1"
    if reload:
        uvicorn.run("core.server:asgi", host=host, port=port, log_level="info", reload=True)
    else:
        from core.server import asgi

        uvicorn.run(asgi, host=host, port=port, log_level="info")
