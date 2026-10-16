"""Trésorerie : soldes réels chez chaque agrégateur, réconciliation avec la
compta interne, et reversement automatique du surplus d'arrondi à la plateforme.

Interne d'un agrégateur = Σ soldes des apps + Σ réserves de frais des apps
(au centime) + solde plateforme + réserve libérée pas encore reversée.

Deux reversements à la plateforme, en francs entiers, quand rien n'est en vol :
  - la réserve libérée (payouts vers un autre réseau que le dépôt) ;
  - le surplus d'arrondi : frais et commissions arrondis à l'excès, donc le réel
    dépasse l'interne de quelques fractions de franc.
"""

import asyncio
import logging
import math

from core.db import db

log = logging.getLogger("ai_browser2")

# Au-delà, l'écart n'est plus un arrondi : on ne le reverse pas, on le signale.
SURPLUS_MAX = 10
_lock = asyncio.Lock()


async def real_balances() -> dict:
    """{agrégateur: solde réel (float) | None si injoignable}."""
    from aggregators.digikuntz import status_poll
    from aggregators.pawapay import client as pawapay
    glob = await status_poll.fetch_global_balance()
    pp = await pawapay.fetch_balances()
    return {
        "digikuntz": float(glob["balance"]) if glob and glob.get("balance") is not None else None,
        "pawapay": (sum(float(b.get("balance") or 0) for b in pp
                        if (b.get("currency") or "XAF") == "XAF") if pp is not None else None),
    }


async def reconciliation() -> dict:
    real = await real_balances()
    split = await db.balances_by_aggregator()
    reserves = {a: sum(n.values()) for a, n in (await db.reserves()).items()}
    pending = await _pending_release()
    per: dict = {}
    for name in sorted(set(real) | set(split["apps"]) | set(split["platform"]) | set(reserves)):
        apps, plat = split["apps"].get(name, 0), split["platform"].get(name, 0)
        res = round(reserves.get(name, 0), 2)
        rel = round(pending.get(name, 0), 2)
        internal = round(apps + res + plat + rel, 2)
        r = real.get(name)
        diff = None if r is None else round(r - internal, 2)
        per[name] = {"apps_total": apps, "apps_payout_reserve": res, "platform_balance": plat,
                     "reserve_released_pending": rel,
                     "internal_total": internal, "real_balance": r, "difference": diff,
                     "reconciled": diff is not None and 0 <= diff < 1}
    return {
        "aggregators": per,
        "apps_total": sum(v["apps_total"] for v in per.values()),
        "apps_payout_reserve": round(sum(v["apps_payout_reserve"] for v in per.values()), 2),
        "platform_balance": sum(v["platform_balance"] for v in per.values()),
        "internal_total": round(sum(v["internal_total"] for v in per.values()), 2),
        "real_total": round(sum(v["real_balance"] or 0 for v in per.values()), 2),
        "reconciled": all(v["reconciled"] for v in per.values()),
        "currency": "XAF",
    }


async def _pending_release() -> dict:
    """Réserve libérée pas encore reversée à la plateforme : {agg: montant}."""
    released = await db.reserve_released()
    credited = await db.platform_credited("reserve_release")
    return {a: v - credited.get(a, 0) for a, v in released.items()}


async def _sweep_releases(swept: dict) -> None:
    for name, amount in (await _pending_release()).items():
        whole = math.floor(amount + 1e-9)
        if whole < 1 or await db.has_unsettled(name):
            continue
        await db.credit_platform(whole, reason="reserve_release", aggregator=name)
        log.info("trésorerie %s : réserve libérée %s XAF -> plateforme", name, whole)
        swept[f"{name}:reserve_release"] = whole


async def sweep_rounding_surplus() -> dict:
    """Crédite à la plateforme les francs entiers de surplus d'arrondi, par
    agrégateur. Rien si une transaction est en vol ou si l'écart n'est pas un
    arrondi (négatif ou >= SURPLUS_MAX : signalé dans les logs)."""
    swept: dict = {}
    async with _lock:
        await _sweep_releases(swept)
        rec = await reconciliation()
        for name, v in rec["aggregators"].items():
            diff = v["difference"]
            if diff is None or diff < 1:
                if diff is not None and diff < 0:
                    log.warning("trésorerie %s : réel < interne de %s XAF", name, -diff)
                continue
            if diff >= SURPLUS_MAX:
                log.warning("trésorerie %s : écart de %s XAF, trop grand pour un arrondi "
                            "-> non reversé (à vérifier)", name, diff)
                continue
            if await db.has_unsettled(name):
                continue
            amount = math.floor(diff)
            await db.credit_platform(amount, reason="rounding_surplus", aggregator=name)
            log.info("trésorerie %s : surplus d'arrondi %s XAF -> plateforme", name, amount)
            swept[name] = amount
    return swept


def schedule_sweep(delay_s: float = 30) -> None:
    """Planifie un reversement (après un règlement, le temps que tout soit écrit)."""
    async def _run():
        await asyncio.sleep(delay_s)
        try:
            await sweep_rounding_surplus()
        except Exception as e:  # noqa: BLE001
            log.warning("reversement surplus échoué : %s", e)
    asyncio.create_task(_run())
