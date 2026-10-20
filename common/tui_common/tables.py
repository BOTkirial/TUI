"""Tableaux partagés par les TUI."""

from __future__ import annotations

from textual import events
from textual.widgets import DataTable

from .clipboard import to_clipboard


class AutoWidthTable(DataTable):
    """DataTable dont les colonnes automatiques s'affichent à leur vraie largeur.

    Contournement Textual (8.x) : la largeur des colonnes automatiques n'est
    calculée qu'au prochain idle, et la largeur ne fait pas partie de la clé du
    cache des cellules rendues ; celles affichées entre-temps (à la largeur de
    l'en-tête) restaient tronquées, typiquement après un filtre.
    """

    def _update_dimensions(self, new_rows) -> None:
        super()._update_dimensions(new_rows)
        self._clear_caches()


class CopyTable(AutoWidthTable):
    """Tableau dont la cellule courante se copie (ctrl+shift+c ou double-clic)."""

    def copy_text(self) -> str:
        """Contenu complet de la cellule courante (hook de copie de BaseApp)."""
        if not self.row_count:
            return ""
        cell = self.get_cell_at(self.cursor_coordinate)
        return cell.plain if hasattr(cell, "plain") else str(cell)

    def on_click(self, event: events.Click) -> None:
        # le 1er clic (géré par DataTable) a déjà placé le curseur sur la cellule ;
        # row < 0 = en-tête, dont le clic ne déplace pas le curseur
        if event.chain != 2 or event.style.meta.get("row", -1) < 0:
            return
        text = self.copy_text()
        if text:
            to_clipboard(self.app, text)
            self.app.notify("Cellule copiée", timeout=1.5)
