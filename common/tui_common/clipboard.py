"""Copie vers le presse-papier système.

On privilégie l'API de Textual (qui route proprement la séquence via le
driver du terminal). En dernier recours seulement, on écrit la séquence
OSC 52 à la main.
"""

from __future__ import annotations

import base64
import sys


def osc52_copy(text: str) -> None:
    """Copie `text` via la séquence d'échappement OSC 52.

    C'est le terminal hôte qui intercepte la séquence et remplit le
    presse-papier : ça fonctionne donc à travers SSH et un conteneur Docker,
    sans accès direct au presse-papier de la machine.
    """
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    sys.stdout.write(f"\033]52;c;{payload}\a")
    sys.stdout.flush()


def to_clipboard(app, text: str) -> None:
    """Copie `text`, en passant par Textual si possible."""
    if not text:
        return
    copier = getattr(app, "copy_to_clipboard", None)
    if callable(copier):
        try:
            copier(text)
            return
        except Exception:
            pass  # on retombe sur l'OSC52 manuel
    osc52_copy(text)
