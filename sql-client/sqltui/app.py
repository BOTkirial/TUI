"""TUI client SQL : écran de connexion au lancement, puis écran de requêtes."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass

from rich.text import Text
from textual import work
from textual.worker import get_current_worker
from textual.app import ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Input,
    Label,
    OptionList,
    Select,
    Static,
    TextArea,
)

from tui_common import (
    COMMON_CSS,
    BaseApp,
    ConfirmPrompt,
    CopyTable,
    call_ui,
    matches,
    to_clipboard,
)

from . import dbeaver
from .connections import (
    DEFAULT_PORTS,
    Connection,
    ConnectionStore,
)
from .drivers import ColumnSource, Database, QueryResult, RowUpdate, TableRef
from .dumpcmd import dump_command, restore_command

DRIVERS = list(DEFAULT_PORTS)
# champs du tunnel SSH, actifs seulement si la case « Tunnel SSH » est cochée
SSH_FIELDS = ("#f-ssh-host", "#f-ssh-port", "#f-ssh-user", "#f-ssh-password", "#f-ssh-key")
# cellule modifiée mais pas encore enregistrée (ctrl+s)
EDITED_STYLE = "bold black on yellow"
NULL_STYLE = "dim italic"
# lignes affichées au plus par résultat : au-delà, le tableau fige l'interface
MAX_ROWS = 10_000


class PickList(OptionList):
    """Liste de choix sans retour circulaire en bout de liste (comme les arbres
    et tableaux des autres TUI ; OptionList repart du début par défaut)."""

    def action_cursor_up(self) -> None:
        if self.highlighted is None or self.highlighted > 0:
            super().action_cursor_up()

    def action_cursor_down(self) -> None:
        if self.highlighted is None or self.highlighted < self.option_count - 1:
            super().action_cursor_down()


def _fill_list(options: OptionList, labels: list[str]) -> None:
    """Remplace le contenu d'une liste et surligne le premier élément.

    OptionList plutôt que ListView : pas un widget monté par élément, le filtre
    reste fluide avec des milliers de tables.
    """
    options.clear_options()
    # Text : un nom contenant `[` ne doit pas être lu comme du markup
    options.add_options(Text(label) for label in labels)
    options.highlighted = 0 if labels else None


def _to_text(value) -> str:
    """Valeur SQL -> texte affiché et éditable (JSON réel plutôt que repr Python)."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _cell(value, edited: bool = False) -> Text:
    """Cellule du tableau ; Text évite que DataTable interprète le contenu comme du markup."""
    if value is None:
        return Text("NULL", style=EDITED_STYLE if edited else NULL_STYLE)
    return Text(_to_text(value), style=EDITED_STYLE if edited else "")


class ConnectionForm(ModalScreen[Connection | None]):
    """Formulaire de création ou de modification d'une connexion."""

    def __init__(self, existing: Connection | None = None):
        super().__init__()
        # connexion éditée (None = création)
        self._existing = existing

    def compose(self) -> ComposeResult:
        conn = self._existing
        with Vertical(id="dialog"):
            title = "Modifier la connexion" if conn else "Nouvelle connexion"
            yield Label(title, classes="section")
            with Horizontal(id="form-columns"):
                with Vertical(classes="form-column"):
                    yield Input(value=conn.name if conn else "", placeholder="nom", id="f-name")
                    yield Select(
                        [(d, d) for d in DRIVERS],
                        value=conn.driver if conn and conn.driver in DRIVERS else "postgres",
                        allow_blank=False, id="f-driver",
                    )
                    yield Input(
                        value=conn.host if conn else "localhost", placeholder="hôte", id="f-host"
                    )
                    yield Input(
                        value=str(conn.port) if conn else "",
                        placeholder="port (défaut selon le driver)", id="f-port",
                    )
                    yield Input(
                        value=conn.database if conn else "",
                        placeholder="base de données", id="f-database",
                    )
                    yield Input(value=conn.user if conn else "", placeholder="utilisateur", id="f-user")
                    yield Input(
                        value=conn.password if conn else "",
                        placeholder="mot de passe", password=True, id="f-password",
                    )
                with Vertical(classes="form-column"):
                    yield Checkbox("Tunnel SSH", value=conn.ssh_enabled if conn else False, id="f-ssh")
                    yield Input(
                        value=conn.ssh_host if conn else "",
                        placeholder="serveur SSH (bastion)", id="f-ssh-host",
                    )
                    yield Input(
                        value=str(conn.ssh_port) if conn and conn.ssh_port != 22 else "",
                        placeholder="port SSH (22)", id="f-ssh-port",
                    )
                    yield Input(
                        value=conn.ssh_user if conn else "",
                        placeholder="utilisateur SSH", id="f-ssh-user",
                    )
                    yield Input(
                        value=conn.ssh_password if conn else "", password=True,
                        placeholder="mot de passe SSH / passphrase de la clé",
                        id="f-ssh-password",
                    )
                    yield Input(
                        value=conn.ssh_key if conn else "",
                        placeholder="clé privée (~/.ssh/…) — vide = agent", id="f-ssh-key",
                    )
                    yield Label(
                        "Hôte/port de la base : vus depuis le bastion",
                        classes="hint",
                    )
            with Horizontal(id="dialog-buttons"):
                yield Button("Enregistrer", variant="primary", id="ok")
                yield Button("Tester", id="test")
                yield Button("Annuler", id="cancel")
            yield Static("", id="form-status")

    def on_mount(self) -> None:
        self._toggle_ssh_fields()
        self.query_one("#f-name", Input).focus()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        self._toggle_ssh_fields()

    def _toggle_ssh_fields(self) -> None:
        # désactivés mais conservés : décocher ne perd pas la configuration du tunnel
        enabled = self.query_one("#f-ssh", Checkbox).value
        for selector in SSH_FIELDS:
            self.query_one(selector, Input).disabled = not enabled

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._submit()
        elif event.button.id == "test":
            self._start_test()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Entrée dans n'importe quel champ = Enregistrer
        self._submit()

    def _read_form(self, require_name: bool = True) -> Connection | None:
        """Construit une Connection depuis les champs, None si saisie invalide."""
        name = self.query_one("#f-name", Input).value.strip()
        if require_name and not name:
            self.app.notify("Le nom est obligatoire", severity="error")
            return None
        driver = str(self.query_one("#f-driver", Select).value)
        port = self._read_port("#f-port", DEFAULT_PORTS[driver], "Port invalide")
        if port is None:
            return None
        ssh_enabled = self.query_one("#f-ssh", Checkbox).value
        # tunnel décoché : champ grisé, une valeur invalide ne doit pas bloquer
        if ssh_enabled:
            ssh_port = self._read_port("#f-ssh-port", 22, "Port SSH invalide")
            if ssh_port is None:
                return None
        else:
            ssh_port = self._existing.ssh_port if self._existing else 22
        ssh_host = self.query_one("#f-ssh-host", Input).value.strip()
        if ssh_enabled and not ssh_host:
            self.app.notify("Le serveur SSH est obligatoire avec un tunnel", severity="error")
            return None
        return Connection(
            name=name,
            driver=driver,
            host=self.query_one("#f-host", Input).value.strip() or "localhost",
            port=port,
            database=self.query_one("#f-database", Input).value.strip(),
            user=self.query_one("#f-user", Input).value.strip(),
            password=self.query_one("#f-password", Input).value,
            ssh_enabled=ssh_enabled,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            ssh_user=self.query_one("#f-ssh-user", Input).value.strip(),
            ssh_password=self.query_one("#f-ssh-password", Input).value,
            ssh_key=self.query_one("#f-ssh-key", Input).value.strip(),
        )

    def _read_port(self, selector: str, default: int, error: str) -> int | None:
        """Port saisi dans `selector` (vide = `default`), None si invalide."""
        raw = self.query_one(selector, Input).value.strip()
        if not raw:
            return default
        try:
            port = int(raw)
        except ValueError:
            port = 0
        if not 1 <= port <= 65535:
            self.app.notify(error, severity="error")
            return None
        return port

    def _submit(self) -> None:
        conn = self._read_form()
        if conn is None:
            return
        # en modification, un renommage ne doit pas écraser une autre connexion
        original = self._existing.name if self._existing else None
        if original is not None and conn.name != original and any(
            c.name == conn.name for c in self.app.store.connections
        ):
            self.app.notify(f"Une connexion « {conn.name} » existe déjà", severity="error")
            return
        self.dismiss(conn)

    def _set_status(self, text: str) -> None:
        self.query_one("#form-status", Static).update(Text(text))

    def _start_test(self) -> None:
        conn = self._read_form(require_name=False)
        if conn is None:
            return
        self._set_status("⏳ Test en cours…")
        self._test(conn)

    @work(thread=True, exclusive=True)
    def _test(self, conn: Connection) -> None:
        db = Database(conn)
        try:
            db.connect()
        except Exception as exc:
            # formulaire peut-être fermé avant la fin du test : call_ui
            call_ui(self, self._set_status, f"✗ {exc}")
            return
        finally:
            db.close()
        call_ui(self, self._set_status, "✓ Connexion réussie")


class ConnectionScreen(Screen):
    """Écran plein d'accueil : gestion et choix de la connexion."""

    BINDINGS = [
        Binding("ctrl+n", "new", "Nouvelle"),
        # priority : sinon ctrl+e (fin de ligne) est capturé par le champ filtre
        Binding("ctrl+e", "edit", "Modifier", priority=True),
        Binding("delete", "delete", "Supprimer"),
        Binding("escape", "clear_filter", "Effacer filtre"),
        Binding("down", "cursor_down", show=False),
        Binding("up", "cursor_up", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        # connexions affichées (après filtre), dans l'ordre de la liste
        self._visible: list[Connection] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="conn-page"):
            yield Label("SQL TUI — Connexions", classes="title")
            yield Label(
                "Tape pour filtrer · ↑↓ pour choisir · Entrée pour connecter · Suppr pour supprimer",
                classes="hint",
            )
            # select_on_focus=False : sinon la lettre redirigée depuis la liste
            # est sélectionnée au focus puis écrasée par la suivante
            yield Input(placeholder="filtrer les connexions…", id="filter", select_on_focus=False)
            yield PickList(id="connections", classes="framed")
            with Horizontal(classes="button-row"):
                yield Button("Nouvelle", id="new", variant="success")
                yield Button("Connecter", id="connect", variant="primary")
                yield Button("Modifier", id="edit")
                yield Button("Supprimer", id="delete", variant="error")
            with Horizontal(classes="button-row"):
                yield Button("Import DBeaver", id="dbeaver")
            yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_connections()
        self.query_one("#filter", Input).focus()

    # --- helpers ---------------------------------------------------------
    def _refresh_connections(self) -> None:
        terms = self.query_one("#filter", Input).value.lower().split()
        self._visible = [c for c in self.app.store.connections if matches(c.label(), terms)]
        _fill_list(self.query_one("#connections", OptionList), [c.label() for c in self._visible])

    def _selected_connection(self) -> Connection | None:
        index = self.query_one("#connections", OptionList).highlighted
        if index is not None and 0 <= index < len(self._visible):
            return self._visible[index]
        return None

    def _set_status(self, text: str) -> None:
        # Text : les messages d'erreur des pilotes ne sont pas du markup
        self.query_one("#status", Static).update(Text(text))

    def dump_target(self) -> tuple[Connection, str] | None:
        """Connexion et base visées par les commandes dump/restauration de la palette."""
        conn = self._selected_connection()
        return (conn, conn.database) if conn else None

    # --- évènements ------------------------------------------------------
    def on_button_pressed(self, event: Button.Pressed) -> None:
        handlers = {
            "new": self.action_new,
            "connect": self._connect_selected,
            "edit": self.action_edit,
            "delete": self.action_delete,
            "dbeaver": self._import_dbeaver,
        }
        handler = handlers.get(event.button.id or "")
        if handler:
            handler()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # Entrée / clic sur une connexion = se connecter
        self._connect_selected()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self._refresh_connections()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Entrée dans le filtre = se connecter à la connexion sélectionnée
        if event.input.id == "filter":
            self._connect_selected()

    def on_key(self, event) -> None:
        filter_input = self.query_one("#filter", Input)
        # Suppr dans le filtre : supprime la connexion s'il n'y a rien à effacer à droite
        if (
            self.focused is filter_input and event.key == "delete"
            and filter_input.cursor_position >= len(filter_input.value)
        ):
            self.action_delete()
            event.stop()
        # frappe d'un caractère sur la liste : on la redirige vers le filtre
        elif (
            self.focused is self.query_one("#connections", OptionList)
            and event.is_printable and event.character
        ):
            filter_input.focus()
            filter_input.insert_text_at_cursor(event.character)
            event.stop()

    # --- actions ---------------------------------------------------------
    def action_cursor_down(self) -> None:
        self.query_one("#connections", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#connections", OptionList).action_cursor_up()

    def action_clear_filter(self) -> None:
        self.query_one("#filter", Input).value = ""

    def _select_by_name(self, name: str) -> None:
        """Rafraîchit la liste puis y sélectionne `name` (vide le filtre s'il la masque)."""
        self._refresh_connections()
        names = [c.name for c in self._visible]
        if name not in names:
            # connexion masquée par le filtre : on le vide sans relancer Changed
            filter_input = self.query_one("#filter", Input)
            with filter_input.prevent(Input.Changed):
                filter_input.value = ""
            self._refresh_connections()
            names = [c.name for c in self._visible]
        if name in names:
            self.query_one("#connections", OptionList).highlighted = names.index(name)

    def _save_store(self, change, *args) -> bool:
        """Applique `change(*args)` au fichier de connexions ; False (avec alerte) si échec."""
        try:
            change(*args)
        except OSError as exc:
            self.app.notify(f"Enregistrement des connexions impossible : {exc}", severity="error")
            return False
        return True

    def action_new(self) -> None:
        def done(conn: Connection | None) -> None:
            if conn is None or not self._save_store(self.app.store.add, conn):  # remplace si même nom
                return
            self._select_by_name(conn.name)
            self.app.notify(f"Connexion « {conn.name} » enregistrée")

        self.app.push_screen(ConnectionForm(), done)

    def action_edit(self) -> None:
        original = self._selected_connection()
        if original is None:
            self.app.notify("Sélectionne une connexion", severity="warning")
            return

        def done(conn: Connection | None) -> None:
            if conn is None or not self._save_store(self.app.store.replace, original.name, conn):
                return
            self._select_by_name(conn.name)
            self.app.notify(f"Connexion « {conn.name} » modifiée")

        self.app.push_screen(ConnectionForm(original), done)

    def action_delete(self) -> None:
        """Supprime (après confirmation) la connexion sélectionnée."""
        conn = self._selected_connection()
        if conn is None:
            self.app.notify("Sélectionne une connexion à supprimer", severity="warning")
            return

        def done(confirmed: bool | None) -> None:
            if not confirmed or not self._save_store(self.app.store.remove, conn.name):
                return
            index = self.query_one("#connections", OptionList).highlighted or 0
            self._refresh_connections()
            # curseur sur la connexion suivante (sinon la précédente), pas en tête de liste
            if self._visible:
                self.query_one("#connections", OptionList).highlighted = min(
                    index, len(self._visible) - 1
                )
            self.app.notify(f"Connexion « {conn.name} » supprimée")

        self.app.push_screen(
            ConfirmPrompt(f"Supprimer la connexion « {conn.name} » ?", "Supprimer"), done
        )

    def _connect_selected(self) -> None:
        conn = self._selected_connection()
        if conn is None:
            self.app.notify("Sélectionne une connexion", severity="warning")
            return
        self._set_status(f"⏳ Connexion à {conn.name}…")
        self._connect(conn)

    @work(thread=True, exclusive=True)
    def _connect(self, conn: Connection) -> None:
        db = Database(conn)
        try:
            db.connect()
            # MySQL sans base : « No database selected » sur SHOW TABLES,
            # on propose plutôt la liste des bases
            if db.has_databases and not conn.database:
                tables, databases = None, db.list_databases()
            else:
                tables, databases = db.list_tables(), None
        except Exception as exc:
            # ferme aussi un tunnel SSH déjà ouvert
            db.close()
            call_ui(self, self._set_status, f"✗ {exc}")
            return
        # un thread n'est pas interrompu par l'annulation (connexion relancée) :
        # celle-ci est abandonnée, sinon deux écrans de requêtes seraient ouverts
        if get_current_worker().is_cancelled:
            db.close()
            return
        # succès : bascule vers l'écran de requêtes
        self.app.call_from_thread(self.app.open_query, db, tables, databases)
        call_ui(self, self._set_status, "")

    def _import_dbeaver(self) -> None:
        workspace = os.environ.get("DBEAVER_WORKSPACE")
        try:
            imported = dbeaver.import_connections(workspace)
        except (OSError, ValueError) as exc:  # data-sources.json illisible
            self.app.notify(f"Import DBeaver impossible : {exc}", severity="error")
            return
        if not imported:
            self.app.notify("Aucune connexion DBeaver trouvée", severity="warning")
            return
        if self._save_store(self.app.store.add, *imported):
            self._refresh_connections()
            self.app.notify(f"{len(imported)} connexion(s) importée(s) de DBeaver")


class SubmitTextArea(TextArea):
    """Zone de texte : Entrée valide, shift+Entrée insère un saut de ligne."""

    # priority : passe avant la gestion native d'Entrée par TextArea ;
    # alt+Entrée / ctrl+j = repli pour les terminaux qui ne distinguent
    # pas shift+Entrée d'Entrée
    BINDINGS = [
        Binding("enter", "submit", show=False, priority=True),
        Binding("shift+enter,alt+enter,ctrl+j", "newline", show=False, priority=True),
    ]

    class Submitted(Message):
        """Validation du contenu (exécution de la requête, saisie d'une cellule)."""

    def action_submit(self) -> None:
        self.post_message(self.Submitted())

    def action_newline(self) -> None:
        # remplace la sélection éventuelle, comme une frappe normale
        self.replace("\n", *self.selection)


@dataclass
class CellValue:
    """Saisie validée dans `CellEditor` (None = NULL)."""

    value: str | None


class CellEditor(ModalScreen[CellValue | None]):
    """Édition d'une cellule du résultat ; renvoie None si annulé."""

    BINDINGS = [Binding("escape", "cancel", "Annuler")]

    def __init__(self, source: ColumnSource, value: str | None):
        super().__init__()
        self._source = source
        self._value = value

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(Text(self._source.label), classes="section")
            current = "NULL" if self._value is None else "valeur"
            yield Label(
                f"Actuellement : {current} · Entrée valider · shift+Entrée saut de ligne",
                classes="hint",
            )
            yield SubmitTextArea(self._value or "", id="cell-value", classes="framed")
            with Horizontal(id="dialog-buttons"):
                yield Button("Valider", variant="primary", id="ok")
                yield Button("NULL", id="null")
                yield Button("Annuler", id="cancel")

    def on_mount(self) -> None:
        editor = self.query_one("#cell-value", SubmitTextArea)
        editor.focus()
        editor.select_all()

    def on_submit_text_area_submitted(self, event: SubmitTextArea.Submitted) -> None:
        self.dismiss(CellValue(self.query_one("#cell-value", SubmitTextArea).text))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self.dismiss(CellValue(self.query_one("#cell-value", SubmitTextArea).text))
        elif event.button.id == "null":
            self.dismiss(CellValue(None))
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ResultsTable(CopyTable):
    """Tableau des résultats : Entrée / F2 édite la cellule courante, double-clic la copie."""

    BINDINGS = [Binding("enter,f2", "edit_cell", "Éditer la cellule")]

    class EditRequested(Message):
        def __init__(self, coordinate: Coordinate):
            super().__init__()
            self.coordinate = coordinate

    def action_edit_cell(self) -> None:
        if self.row_count:
            self.post_message(self.EditRequested(self.cursor_coordinate))


class QueryScreen(Screen):
    """Écran de travail : tables, éditeur SQL et résultats."""

    BINDINGS = [
        Binding("ctrl+r", "run", "Exécuter"),
        Binding("ctrl+s", "save", "Enregistrer"),
        # Échap : vide le filtre s'il est rempli, sinon retour aux connexions
        Binding("escape", "escape", "Filtre / Connexions"),
        Binding("ctrl+b", "databases", "Bases"),
        Binding("down", "cursor_down", show=False),
        Binding("up", "cursor_up", show=False),
    ]

    def __init__(
        self, db: Database, tables: list[str] | None, databases: list[str] | None = None
    ):
        super().__init__()
        # connexion ouverte, fermée quand l'écran est retiré (retour aux connexions)
        self._db = db
        self._tables = tables or []
        # tables=None : pas de base courante (MySQL), la barre latérale liste les bases
        self._databases = databases or []
        self._choosing_db = tables is None
        # base courante (MySQL : change avec ctrl+b), cible des commandes de dump
        self._database = db.meta.database
        # tables (ou bases) affichées après filtre, dans l'ordre de la liste
        self._visible: list[str] = []
        # dernier résultat affiché et modifications en attente :
        # (ligne, colonne) -> nouvelle valeur (None = NULL)
        self._result: QueryResult | None = None
        self._edits: dict[tuple[int, int], str | None] = {}
        # True une fois l'utilisateur averti que ses modifications seront perdues
        self._discard_warned = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            with Vertical(id="sidebar"):
                # connexion ouverte : rappelée en permanence (le statut est écrasé)
                yield Label(Text(self._db.meta.name), classes="title")
                yield Label("Tables", classes="section", id="sidebar-title")
                yield Input(
                    placeholder="filtrer les tables…", id="table-filter", select_on_focus=False
                )
                yield PickList(id="tables", classes="framed")
            with Vertical(id="workspace"):
                yield Label(
                    "Requête SQL  —  Entrée pour exécuter · shift+Entrée pour un saut de ligne",
                    classes="section",
                )
                yield SubmitTextArea(
                    "SELECT 1;", id="query", language="sql", classes="editor-area framed"
                )
                yield ResultsTable(id="results", cursor_type="cell", classes="framed")
                yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        if self._choosing_db:
            self._show_databases(self._databases)
        else:
            self._refresh_tables()
            self._set_status(
                f"✓ Connecté à {self._db.meta.name} — {len(self._tables)} table(s)"
            )
        self.query_one("#table-filter", Input).focus()

    def on_unmount(self) -> None:
        # hors du thread UI : la fermeture du tunnel SSH peut attendre le réseau
        threading.Thread(target=self._db.close, daemon=True).start()

    # --- évènements ------------------------------------------------------
    def on_submit_text_area_submitted(self, event: SubmitTextArea.Submitted) -> None:
        self.action_run()

    def on_results_table_edit_requested(self, event: ResultsTable.EditRequested) -> None:
        self._edit_cell(event.coordinate.row, event.coordinate.column)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # clic sur une table -> SELECT prêt à l'emploi
        if event.option_list.id == "tables":
            self._open_table()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "table-filter":
            self._refresh_tables()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Entrée dans le filtre = ouvrir la table sélectionnée
        if event.input.id == "table-filter":
            self._open_table()

    def on_key(self, event) -> None:
        # frappe d'un caractère sur la liste : on la redirige vers le filtre
        if self.focused is self.query_one("#tables", OptionList) and event.is_printable and event.character:
            filter_input = self.query_one("#table-filter", Input)
            filter_input.focus()
            filter_input.insert_text_at_cursor(event.character)
            event.stop()

    # --- helpers ---------------------------------------------------------
    def _refresh_tables(self) -> None:
        terms = self.query_one("#table-filter", Input).value.lower().split()
        items = self._databases if self._choosing_db else self._tables
        self._visible = [t for t in items if matches(t, terms)]
        _fill_list(self.query_one("#tables", OptionList), self._visible)

    def _set_sidebar(self, title: str, placeholder: str) -> None:
        """Bascule bases <-> tables : titre, filtre vidé, liste reconstruite."""
        self.query_one("#sidebar-title", Label).update(Text(title))
        filter_input = self.query_one("#table-filter", Input)
        filter_input.placeholder = placeholder
        # sans relancer Changed : la liste est reconstruite juste après
        with filter_input.prevent(Input.Changed):
            filter_input.value = ""
        self._refresh_tables()

    def _show_databases(self, databases: list[str]) -> None:
        self._choosing_db = True
        self._databases = databases
        self._set_sidebar("Bases", "filtrer les bases…")
        self._set_status(
            f"✓ Connecté à {self._db.meta.name} — choisis une base ({len(databases)}), Entrée pour l'ouvrir"
        )

    def _show_tables(self, database: str, tables: list[str]) -> None:
        self._choosing_db = False
        self._database = database
        self._tables = tables
        self._set_sidebar(f"Tables — {database}", "filtrer les tables…")
        self._set_status(f"✓ Base {database} — {len(tables)} table(s) · ctrl+b : autres bases")

    def _open_table(self) -> None:
        """Lance un SELECT sur la table sélectionnée (ou ouvre la base sélectionnée)."""
        index = self.query_one("#tables", OptionList).highlighted
        if index is None or not 0 <= index < len(self._visible):
            return
        if self._choosing_db:
            name = self._visible[index]
            self._set_status(f"⏳ Ouverture de la base {name}…")
            self._use_database(name)
            return
        table = self._db.quote_table(self._visible[index])
        self.query_one("#query", TextArea).text = f"SELECT * FROM {table} LIMIT 100;"
        self.action_run()

    @work(thread=True, exclusive=True, group="database")
    def _use_database(self, name: str) -> None:
        try:
            self._db.use_database(name)
            tables = self._db.list_tables()
        except Exception as exc:
            call_ui(self, self._set_status, f"✗ {exc}")
            return
        call_ui(self, self._show_tables, name, tables)

    @work(thread=True, exclusive=True, group="database")
    def _load_databases(self) -> None:
        try:
            databases = self._db.list_databases()
        except Exception as exc:
            call_ui(self, self._set_status, f"✗ {exc}")
            return
        call_ui(self, self._show_databases, databases)

    # --- actions ---------------------------------------------------------
    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # choix de la base : MySQL/MariaDB seulement (masqué du pied de page sinon)
        if action == "databases":
            return self._db.has_databases
        return True

    def action_databases(self) -> None:
        self._set_status("⏳ Lecture des bases…")
        self._load_databases()

    def action_back(self) -> None:
        if self._confirm_discard():
            self.app.pop_screen()

    def action_escape(self) -> None:
        filter_input = self.query_one("#table-filter", Input)
        if filter_input.value:
            filter_input.value = ""
        else:
            self.action_back()

    def action_cursor_down(self) -> None:
        self.query_one("#tables", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#tables", OptionList).action_cursor_up()

    def action_run(self) -> None:
        editor = self.query_one("#query", TextArea)
        sql = editor.selected_text or editor.text
        if not sql.strip() or not self._confirm_discard():
            return
        self._set_status("⏳ Exécution…")
        self._run_query(sql)

    @work(thread=True, exclusive=True)
    def _run_query(self, sql: str) -> None:
        try:
            result = self._db.execute(sql, max_rows=MAX_ROWS)
        except Exception as exc:
            call_ui(self, self._set_status, f"✗ {exc}")
            return
        # conversion en texte dans le worker, pas sur le thread UI
        cells = [[_cell(v) for v in row] for row in result.rows]
        call_ui(self, self._show_result, result, cells)

    def _show_result(self, result: QueryResult, cells: list[list[Text]]) -> None:
        table = self.query_one("#results", DataTable)
        table.clear(columns=True)
        self._result = result if result.columns else None
        self._edits.clear()
        if result.columns:
            # clés = index : survivent aux noms de colonnes en double
            for i, name in enumerate(result.columns):
                table.add_column(name, key=str(i))
            for i, row in enumerate(cells):
                table.add_row(*row, key=str(i))
            editable = "modifiable (Entrée sur une cellule)" if result.keys else "lecture seule"
            count = (
                f"{result.rowcount} premières lignes (résultat tronqué)"
                if result.truncated else f"{result.rowcount} ligne(s)"
            )
            self._set_status(f"✓ {count} — {editable}")
        else:
            self._set_status(f"✓ {result.message}")

    # --- édition des résultats ---------------------------------------------
    def _edit_cell(self, row: int, col: int) -> None:
        result = self._result
        if result is None:
            return
        source = result.sources[col]
        if source is None:
            self.app.notify(
                "Colonne non modifiable : elle doit venir d'une table dont "
                "la clé primaire figure dans le résultat",
                severity="warning",
            )
            return
        original = result.rows[row][col]
        if isinstance(original, (bytes, bytearray, memoryview)):
            self.app.notify("Valeur binaire : édition non supportée", severity="warning")
            return
        if (row, col) in self._edits:
            current = self._edits[(row, col)]
        else:
            current = None if original is None else _to_text(original)

        def done(edit: CellValue | None) -> None:
            # le résultat a pu être remplacé pendant que la modale était ouverte
            if edit is None or self._result is not result:
                return
            unchanged = (
                edit.value is None if original is None else edit.value == _to_text(original)
            )
            if unchanged:
                self._edits.pop((row, col), None)
            else:
                self._edits[(row, col)] = edit.value
            self._discard_warned = False
            self.query_one("#results", DataTable).update_cell(
                str(row), str(col),
                _cell(edit.value if not unchanged else original, edited=not unchanged),
            )
            self._show_pending()

        self.app.push_screen(CellEditor(source, current), done)

    def _show_pending(self) -> None:
        if self._edits:
            self._set_status(f"✎ {len(self._edits)} cellule(s) modifiée(s) — ctrl+s pour enregistrer")
        else:
            self._set_status("Aucune modification en attente")

    def _confirm_discard(self) -> bool:
        """Faut-il continuer malgré des modifications non enregistrées ? (avertit une fois)"""
        if not self._edits or self._discard_warned:
            return True
        self._discard_warned = True
        self.app.notify(
            f"{len(self._edits)} cellule(s) non enregistrée(s) : ctrl+s pour enregistrer, "
            "ou recommence pour les abandonner",
            severity="warning",
        )
        return False

    def _build_updates(self, result: QueryResult, edits: dict) -> list[RowUpdate]:
        """Regroupe les cellules modifiées en un UPDATE par (ligne, table)."""
        grouped: dict[tuple[int, TableRef], dict[str, str | None]] = {}
        for (row, col), value in sorted(edits.items()):
            source = result.sources[col]
            grouped.setdefault((row, source.table), {})[source.column] = value
        updates = []
        for (row, table), values in grouped.items():
            # clé = valeurs d'origine, même si la clé primaire elle-même est modifiée
            key = {result.sources[i].column: result.rows[row][i] for i in result.keys[table]}
            updates.append(RowUpdate(table=table, values=values, key=key))
        return updates

    def action_save(self) -> None:
        if self._result is None or not self._edits:
            self.app.notify("Aucune modification à enregistrer")
            return
        edits = dict(self._edits)
        self._set_status("⏳ Enregistrement…")
        self._save(self._result, edits, self._build_updates(self._result, edits))

    @work(thread=True, exclusive=True)
    def _save(self, result: QueryResult, edits: dict, updates: list[RowUpdate]) -> None:
        try:
            self._db.apply_updates(updates)
        except Exception as exc:
            call_ui(self, self._set_status, f"✗ Rien n'a été enregistré : {exc}")
            return
        call_ui(self, self._saved, result, edits, len(updates))

    def _saved(self, result: QueryResult, edits: dict, count: int) -> None:
        # les valeurs enregistrées deviennent les valeurs d'origine des lignes
        for (row, col), value in edits.items():
            values = list(result.rows[row])
            values[col] = value
            result.rows[row] = tuple(values)
        if self._result is not result:
            return
        table = self.query_one("#results", DataTable)
        for cell, value in edits.items():
            # une cellule ressaisie pendant l'enregistrement reste en attente
            if cell in self._edits and self._edits[cell] == value:
                del self._edits[cell]
                table.update_cell(str(cell[0]), str(cell[1]), _cell(value))
        suffix = f" — {len(self._edits)} cellule(s) encore en attente" if self._edits else ""
        self._set_status(f"✓ {count} ligne(s) mise(s) à jour{suffix}")

    def _set_status(self, text: str) -> None:
        # Text : les messages d'erreur des pilotes ne sont pas du markup
        self.query_one("#status", Static).update(Text(text))

    def dump_target(self) -> tuple[Connection, str] | None:
        """Connexion et base visées par les commandes dump/restauration de la palette."""
        return self._db.meta, self._database


class SqlTui(BaseApp):
    CSS_PATH = [COMMON_CSS, "app.tcss"]
    TITLE = "SQL TUI"

    def __init__(self):
        super().__init__()
        self.store = ConnectionStore()

    def on_mount(self) -> None:
        try:
            self.store.load()
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            # la prochaine sauvegarde écrasera le fichier : on en garde une copie
            try:
                kept = f"copie conservée dans {self.store.backup()}"
            except OSError:
                kept = "copie impossible"
            self.notify(
                f"{self.store.path} illisible ({exc}) — {kept}", severity="error", timeout=15
            )
        self.push_screen(ConnectionScreen())

    def get_system_commands(self, screen):
        yield from super().get_system_commands(screen)
        if isinstance(screen, QueryScreen):
            yield SystemCommand(
                "Connexions", "Revenir à l'écran des connexions", screen.action_back
            )
        if isinstance(screen, (ConnectionScreen, QueryScreen)):
            yield SystemCommand(
                "Copier la commande de dump",
                "pg_dump / mysqldump de la base vers ~/Téléchargements",
                lambda: self._copy_command(screen, dump_command, "dump"),
            )
            yield SystemCommand(
                "Copier la commande de restauration",
                "psql / mysql depuis un dump de ~/Téléchargements (nom du fichier à compléter)",
                lambda: self._copy_command(screen, restore_command, "restauration"),
            )

    def _copy_command(self, screen, build, kind: str) -> None:
        target = screen.dump_target()
        if target is None:
            self.notify("Aucune connexion sélectionnée", severity="warning")
            return
        conn, database = target
        try:
            command = build(conn, database)
        except ValueError as exc:
            self.notify(str(exc), severity="warning")
            return
        to_clipboard(self, command)
        # pas la commande elle-même : elle contient le mot de passe
        self.notify(f"Commande de {kind} copiée ({database or conn.name})", timeout=3)

    def open_query(
        self, db: Database, tables: list[str] | None, databases: list[str] | None = None
    ) -> None:
        """Connexion réussie : ouvre l'écran requêtes, qui possède la connexion."""
        self.push_screen(QueryScreen(db, tables, databases))


def main() -> None:
    SqlTui().run()


if __name__ == "__main__":
    main()
