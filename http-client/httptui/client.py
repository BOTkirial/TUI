"""Exécution d'une requête HTTP et mise en forme de la réponse."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import httpx

from .httpfile import HttpRequest


@dataclass
class HttpResult:
    status: int
    reason: str
    elapsed_ms: float
    size: int
    headers: dict[str, str]
    body: str
    error: str | None = None


def make_client(timeout: float = 30.0) -> httpx.AsyncClient:
    """Crée un client réutilisable (connexions persistantes / keep-alive)."""
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True)


async def send_request(
    request: HttpRequest,
    timeout: float = 30.0,
    client: httpx.AsyncClient | None = None,
) -> HttpResult:
    """Envoie une requête et renvoie un résultat normalisé (jamais d'exception).

    Si `client` est fourni, il est réutilisé (keep-alive) ; sinon un client
    jetable est créé le temps de l'appel.
    """
    start = time.perf_counter()
    try:
        if client is not None:
            response = await client.request(
                request.method,
                request.url,
                headers=request.headers or None,
                content=request.body.encode("utf-8") if request.body else None,
            )
        else:
            async with make_client(timeout) as owned:
                response = await owned.request(
                    request.method,
                    request.url,
                    headers=request.headers or None,
                    content=request.body.encode("utf-8") if request.body else None,
                )
        elapsed = (time.perf_counter() - start) * 1000
        return HttpResult(
            status=response.status_code,
            reason=response.reason_phrase,
            elapsed_ms=elapsed,
            size=len(response.content),
            headers=dict(response.headers),
            body=_pretty_body(response),
        )
    except Exception as exc:  # réseau, DNS, timeout, URL invalide...
        elapsed = (time.perf_counter() - start) * 1000
        return HttpResult(
            status=0, reason="ERREUR", elapsed_ms=elapsed, size=0,
            headers={}, body="", error=str(exc),
        )


def _pretty_body(response: httpx.Response) -> str:
    """Indente le JSON pour la lisibilité, sinon renvoie le texte brut."""
    content_type = response.headers.get("content-type", "")
    text = response.text
    if "json" in content_type:
        try:
            return json.dumps(response.json(), indent=2, ensure_ascii=False)
        except Exception:
            return text
    return text
