"""Data models of the browser layer: CapturedRequest (network capture,
to_curl) and DomSnapshot (what the AI sees each turn)."""

import logging
from dataclasses import dataclass, field

log = logging.getLogger("ai_browser2")


@dataclass
class CapturedRequest:
    """A captured HTTP request/response pair."""
    url: str = ""
    method: str = ""
    status: int = 0
    request_headers: dict = field(default_factory=dict)
    request_body: str = ""
    response_headers: dict = field(default_factory=dict)
    response_body: str = ""
    timestamp: float = 0.0

    def to_curl(self) -> str:
        """Generate a curl command to replay this request."""
        parts = [f"curl -X {self.method} '{self.url}'"]
        for k, v in self.request_headers.items():
            if k.lower() in ("host", "content-length", "connection"):
                continue
            parts.append(f"-H '{k}: {v}'")
        if self.request_body:
            parts.append(f"--data-raw '{self.request_body}'")
        return " \\\n  ".join(parts)


@dataclass
class DomSnapshot:
    url: str = ""
    outer_html: str = ""           # DOM du frame actif (iframe si on est dedans)
    page_outer_html: str = ""      # DOM de la page principale (toujours présent)
    screenshot_b64: str = ""
    interactive_elements: list[dict] = field(default_factory=list)
    url_history: list[str] = field(default_factory=list)   # URLs visitées depuis le début
    elapsed_s: float = 0.0                                  # secondes depuis le début du loop
    deadline_exceeded: bool = False                         # fenêtre opérateur dépassée (l'IA doit conclure)
    frame_detached_count: int = 0  # combien de fois l'iframe s'est détachée depuis le début
    # Réseau — vision complète comme DevTools
    pending_requests: list[dict] = field(default_factory=list)
    recent_responses: list[dict] = field(default_factory=list)
    failed_requests: list[dict] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
