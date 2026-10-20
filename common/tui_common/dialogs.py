"""Fenêtres modales partagées par les TUI."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label


class ConfirmPrompt(ModalScreen[bool]):
    """Demande de confirmation (Entrée = confirmer, Échap = annuler)."""

    BINDINGS = [Binding("escape", "cancel", "Annuler")]

    def __init__(self, message: str, ok_label: str = "OK"):
        super().__init__()
        self._message = message
        self._ok_label = ok_label

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            # Text : un nom contenant `[` ne doit pas être lu comme du markup
            yield Label(Text(self._message), classes="section")
            with Horizontal(id="dialog-buttons"):
                yield Button(self._ok_label, variant="error", id="ok")
                yield Button("Annuler", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#ok", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "ok")

    def action_cancel(self) -> None:
        self.dismiss(False)
