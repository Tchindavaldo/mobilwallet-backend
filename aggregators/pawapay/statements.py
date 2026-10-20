"""Relevés pawaPay (POST /v2/statements) -> table `pawapay_statement_lines`.

pawaPay ne liste pas les transactions : le relevé CSV du wallet est la seule
source en masse (argent réellement bougé ; les échecs n'y figurent pas, ils
restent dans `transactions`). Génération asynchrone : on la demande, on interroge
GET /v2/statements/{id} jusqu'au `downloadUrl`, on télécharge le CSV et on
insère les lignes nouvelles (idempotent, index unique).

Synchro automatique (`schedule`) selon PAWAPAY_STATEMENT_SYNC (day|week|month,
défaut week) : de la dernière fin couverte MOINS un recouvrement de 2 jours
(lignes du jour publiées jusqu'à 20 min en retard) jusqu'à maintenant, par
tranches de 31 jours max (limite pawaPay)."""

import asyncio
import csv
import io
import logging
from datetime import datetime, timedelta, timezone

import httpx

from core.config import settings
from core.db import db

from . import client

log = logging.getLogger("ai_browser2")

_pp = settings.pawapay
_LINES = "pawapay_statement_lines"
_SYNCS = "pawapay_statement_syncs"
_MAX_SPAN = timedelta(days=31)
_OVERLAP = timedelta(days=2)
_PERIOD = {"day": timedelta(days=1), "week": timedelta(days=7), "month": timedelta(days=30)}


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


async def _generate(start: datetime, end: datetime) -> dict:
    """Demande un relevé et attend son CSV. {statement_id, status, csv|error}."""
    body = {"wallet": {"country": _pp.country, "currency": _pp.currency},
            "callbackUrl": f"{settings.public_base_url.rstrip('/')}/webhook/pawapay/statement",
            "startDate": _iso(start), "endDate": _iso(end)}
    init = await client._post("/v2/statements", body)
    sid = init.get("statementId")
    if init.get("status") != "ACCEPTED" or not sid:
        return {"statement_id": sid, "status": "REJECTED",
                "error": str(init.get("failureReason") or init)[:500]}
    for _ in range(120):  # ~10 min max
        await asyncio.sleep(5)
        st = await client._get(f"/v2/statements/{sid}")
        data = (st or {}).get("data", st) or {}
        if data.get("status") == "COMPLETED" and data.get("downloadUrl"):
            async with httpx.AsyncClient(timeout=60) as http:
                r = await http.get(data["downloadUrl"])
                r.raise_for_status()
            return {"statement_id": sid, "status": "COMPLETED", "csv": r.text}
        if data.get("status") == "FAILED":
            return {"statement_id": sid, "status": "FAILED",
                    "error": str(data.get("failureReason") or data)[:500]}
    return {"statement_id": sid, "status": "FAILED", "error": "timeout génération"}


def _parse(text: str, statement_id: str) -> list[dict]:
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        if not r.get("Transaction ID"):
            continue
        rows.append({
            "occurred_at": r["Creation date"] + ("" if "+" in r["Creation date"] else "+00:00"),
            "msisdn": r.get("MSISDN") or None,
            "provider_transaction_id": r["Transaction ID"],
            "amount": r.get("Amount") or "0",
            "mno": r.get("MNO") or None,
            "line_type": r.get("Type") or "",
            "balance": r.get("Balance") or None,
            "reason": r.get("Reason") or None,
            "metadata": r.get("Metadata") or None,
            "country": _pp.country, "currency": _pp.currency,
            "statement_id": statement_id,
        })
    return rows


async def _store(rows: list[dict]) -> int:
    """Insère les lignes absentes ; retourne le nombre de nouvelles lignes."""
    if not rows or not db.enabled:
        return 0
    res = await db._run(lambda: db._client.table(_LINES).upsert(
        rows, on_conflict="provider_transaction_id,line_type,occurred_at,amount",
        ignore_duplicates=True).execute())
    return len((res.data if res else []) or [])


async def _log_sync(**row) -> None:
    if db.enabled:
        await db._run(lambda: db._client.table(_SYNCS).insert(row).execute())


async def sync(start: datetime, end: datetime) -> dict:
    """Synchronise [start, end] par tranches de 31 j. {lines, inserted, chunks}."""
    total = inserted = 0
    chunks = []
    cur = start
    while cur < end:
        stop = min(cur + _MAX_SPAN, end)
        try:
            res = await _generate(cur, stop)
            rows = _parse(res["csv"], res["statement_id"]) if res.get("csv") else []
            new = await _store(rows)
        except Exception as e:  # noqa: BLE001 — une tranche ratée n'arrête pas les autres
            res, rows, new = {"status": "FAILED", "error": str(e)[:500]}, [], 0
        await _log_sync(statement_id=res.get("statement_id"), start_date=_iso(cur),
                        end_date=_iso(stop), status=res["status"], lines=len(rows),
                        inserted=new, error=res.get("error"))
        chunks.append({"start": _iso(cur), "end": _iso(stop), "status": res["status"],
                       "lines": len(rows), "inserted": new, "error": res.get("error")})
        total += len(rows)
        inserted += new
        cur = stop
    log.info("pawapay statements %s -> %s : %d lignes, %d nouvelles", _iso(start), _iso(end),
             total, inserted)
    return {"lines": total, "inserted": inserted, "chunks": chunks}


async def _last_end() -> datetime | None:
    if not db.enabled:
        return None
    res = await db._run(lambda: db._client.table(_SYNCS).select("end_date")
                        .eq("status", "COMPLETED").order("end_date", desc=True)
                        .limit(1).execute())
    data = (res.data if res else []) or []
    return datetime.fromisoformat(data[0]["end_date"].replace("Z", "+00:00")) if data else None


async def sync_since_last() -> dict:
    """Depuis la dernière fin couverte (- recouvrement), ou 31 j, jusqu'à maintenant."""
    now = datetime.now(timezone.utc)
    last = await _last_end()
    start = (last - _OVERLAP) if last else now - _MAX_SPAN
    return await sync(start, now)


async def list_lines(page: int = 1, limit: int = 20, start_date: str | None = None,
                     end_date: str | None = None, msisdn: str | None = None) -> dict | None:
    """Lignes stockées, plus récentes d'abord, paginées."""
    if not db.enabled:
        return None

    def _q():
        q = db._client.table(_LINES).select("*", count="exact")
        if start_date:
            q = q.gte("occurred_at", start_date)
        if end_date:
            q = q.lte("occurred_at", f"{end_date}T23:59:59" if len(end_date) == 10 else end_date)
        if msisdn:
            q = q.ilike("msisdn", f"%{msisdn}")
        off = (page - 1) * limit
        return q.order("occurred_at", desc=True).range(off, off + limit - 1).execute()

    res = await db._run(_q)
    total = (res.count if res else 0) or 0
    pages = (total + limit - 1) // limit
    return {"data": (res.data if res else []) or [],
            "pagination": {"current_page": page, "limit": limit, "total_pages": pages,
                           "total_items": total, "has_next_page": page < pages}}


async def schedule() -> None:
    """Boucle de fond : une synchro au démarrage puis à chaque période."""
    period = _PERIOD.get(_pp.statement_sync, _PERIOD["week"])
    while True:
        try:
            last = await _last_end()
            if last is None or datetime.now(timezone.utc) - last >= period:
                await sync_since_last()
        except Exception as e:  # noqa: BLE001
            log.warning("pawapay statements: synchro auto échouée : %s", e)
        await asyncio.sleep(3600)  # re-vérifie chaque heure si la période est échue
