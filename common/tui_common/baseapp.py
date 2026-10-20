"""App Textual de base : souris + copie automatique de la sélection.

Comportement « à la Claude Code » : dès qu'on relâche la souris après avoir
sélectionné du texte, la sélection part dans le presse-papier. Un raccourci
explicite (ctrl+shift+c) fait la même chose au clavier ; sans sélection, il
copie ce que le widget focalisé expose via `copy_text()` (ex. cellule courante).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from textual.app import App, SystemCommand
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import TextArea

from .clipboard import to_clipboard

# styles partagés : à placer en tête du CSS_PATH de chaque app
COMMON_CSS = Path(__file__).with_name("common.tcss")


class BaseApp(App):
    """Classe mère commune aux trois TUI."""

    # fusionnés par Textual avec les BINDINGS des sous-classes, actifs sur tous les écrans
    BINDINGS = [
        Binding("ctrl+shift+c", "copy_selection", "Copier"),
        Binding("ctrl+q", "quit", "Quitter"),
    ]

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        # Palette ctrl+p sans la capture d'écran SVG native de Textual.
        for command in super().get_system_commands(screen):
            if command.title != "Screenshot":
                yield command

    def notify(self, message: str, *, title: str = "", severity="information",
               timeout: float | None = None, markup: bool = False) -> None:
        # jamais de markup : les messages contiennent des noms de fichiers, de
        # tables ou des erreurs (`[/]` y lèverait une MarkupError). Les écrans
        # passent markup=True explicitement (Widget.notify) : forcé ici.
        super().notify(message, title=title, severity=severity, timeout=timeout, markup=False)

    def _selected_text(self) -> str:
        """Récupère le texte sélectionné, quelle que soit la source."""
        focused = self.focused
        # 1) sélection dans la zone de texte focalisée
        if isinstance(focused, TextArea):
            selected = focused.selected_text
            if selected:
                return selected
        # 2) sélection applicative (texte des widgets statiques), si la
        #    version de Textual l'expose
        getter = getattr(self, "get_selected_text", None)
        if callable(getter):
            try:
                text = getter()
                if text:
                    return text
            except Exception:
                pass
        return ""

    def _auto_copy(self) -> None:
        text = self._selected_text()
        if text:
            to_clipboard(self, text)

    def on_mouse_up(self, event) -> None:  # noqa: D401 - hook Textual
        # Copie auto au relâchement de la souris (fin de sélection).
        self._auto_copy()

    def _focused_copy_text(self) -> str:
        """Texte que le widget focalisé propose de copier sans sélection (hook `copy_text`)."""
        getter = getattr(self.focused, "copy_text", None)
        return getter() if callable(getter) else ""

    def action_copy_selection(self) -> None:
        # pas de hook dans _selected_text : la copie auto au clic écraserait
        # le presse-papier à chaque déplacement dans un tableau
        text = self._selected_text() or self._focused_copy_text()
        if text:
            to_clipboard(self, text)
            self.notify("Copié dans le presse-papier", timeout=1.5)
        else:
            self.notify("Aucune sélection", severity="warning", timeout=1.5)
