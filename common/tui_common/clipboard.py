"""Copie vers le presse-papier système.

`App.copy_to_clipboard` de Textual émet la séquence OSC 52 via le driver du
terminal : c'est le terminal hôte qui l'intercepte et remplit le presse-papier,
ce qui fonctionne donc à travers SSH et un conteneur Docker, sans accès direct
au presse-papier de la machine.
"""

from __future__ import annotations

from textual.app import App


def to_clipboard(app: App, text: str) -> None:
    """Copie `text` dans le presse-papier (rien si vide)."""
    if text:
        app.copy_to_clipboard(text)
