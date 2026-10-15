"""Registre webhook ⇄ polling — une "boîte aux lettres" par transaction provider.

Le webhook entrant et le polling surveillent le MÊME paiement. Le premier qui
obtient un statut terminal le DÉPOSE ici ; l'autre le voit et s'arrête. Indexé
par l'id provider (unique) -> aucune collision entre transactions parallèles.
État mémoire (process unique). Chaque agrégateur a sa propre instance.
"""

import asyncio


class VerdictRegistry:
    def __init__(self):
        self._events: dict[str, asyncio.Event] = {}
        self._verdicts: dict[str, dict] = {}

    def register(self, tx_id: str) -> asyncio.Event:
        """Ouvre une boîte pour cette transaction (appelé par le polling)."""
        ev = asyncio.Event()
        self._events[tx_id] = ev
        return ev

    def deliver(self, tx_id: str, verdict: dict) -> bool:
        """Dépose un verdict terminal (appelé par le webhook OU le polling).

        Retourne True si une boîte attendait (donc on a réveillé l'autre).
        """
        self._verdicts[tx_id] = verdict
        ev = self._events.get(tx_id)
        if ev is not None and not ev.is_set():
            ev.set()
            return True
        return False

    def take_verdict(self, tx_id: str) -> dict | None:
        return self._verdicts.get(tx_id)

    def close(self, tx_id: str):
        """Ferme la boîte (le polling l'appelle en fin de transaction)."""
        self._events.pop(tx_id, None)
        self._verdicts.pop(tx_id, None)
