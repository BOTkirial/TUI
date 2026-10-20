"""Retour sur le thread UI depuis un worker thread."""

from __future__ import annotations

from typing import Any, Callable

from textual.dom import DOMNode


def call_ui(node: DOMNode, callback: Callable[..., Any], *args: Any) -> None:
    """`app.call_from_thread(callback, *args)`, ignoré si `node` a été retiré entre-temps.

    Un worker thread n'est pas interrompu quand son écran est fermé : sans cette
    garde, le rappel ferait un `query_one` sur un écran détaché (NoMatches),
    l'exception remonterait dans le worker et ferait quitter l'app.
    """

    def guarded() -> None:
        if node.is_attached:
            callback(*args)

    node.app.call_from_thread(guarded)
