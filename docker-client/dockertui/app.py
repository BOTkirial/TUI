"""TUI Docker : conteneurs (par stack compose), images, réseaux et volumes."""

from __future__ import annotations

import os
import re
import threading
from collections import deque
from typing import Callable

from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Input,
    Label,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
    Tabs,
    TextArea,
    Tree,
)
from textual.widgets.tree import TreeNode

from tui_common import COMMON_CSS, BaseApp, ConfirmPrompt, CopyTable, call_ui, matches

from . import engine
from .engine import Container, DockerError, Image, Network, Snapshot, Stack, Volume

# pastille d'état des conteneurs : état Docker -> style
STATE_STYLES = {
    "running": "green",
    "exited": "red",
    "dead": "red",
    "paused": "yellow",
    "restarting": "yellow",
    "created": "dim",
}

# séquences de contrôle d'une ligne de log : échappements ANSI complets (CSI, OSC,
# ESC + 1 caractère) puis caractères de contrôle isolés (tabulation conservée)
_CONTROL = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b.?"
    r"|[\x00-\x08\x0b-\x1f\x7f]"
)


def _clean_log_line(line: str) -> str:
    """Ne garde que les couleurs (SGR `ESC[…m`) : Rich laisse passer d'autres
    séquences, dont `ESC c` (reset du terminal, envoyé par les modes « watch »
    pour effacer l'écran) qui casse l'affichage et fige l'app."""
    return _CONTROL.sub(
        lambda m: m[0] if m[0].startswith("\x1b[") and m[0].endswith("m") else "", line
    )


# nœud regroupant les conteneurs lancés hors docker compose
STANDALONE = "(hors compose)"


class LogScreen(Screen):
    """Logs d'un conteneur ou d'une stack, suivis en continu (`docker logs -f`)."""

    BINDINGS = [
        Binding("escape", "close", "Retour"),
        Binding("f9", "close", "Retour", show=False),
        Binding("ctrl+f", "toggle_follow", "Suivre"),
        Binding("ctrl+l", "clear", "Effacer"),
    ]

    # rafraîchissement de l'affichage : les lignes reçues entre deux passages
    # sont écrites en un seul bloc (un rendu par ligne fige l'app sur un gros débit)
    _FLUSH_INTERVAL = 0.2
    # lignes en attente au plus : au-delà, les plus anciennes sont sautées
    # pour que l'affichage ne prenne jamais de retard
    _MAX_PENDING = 500

    def __init__(self, target: Container | Stack):
        super().__init__()
        self._target = target
        self._proc = None
        self._closing = False
        # lignes brutes (octets) en attente d'affichage, bornées
        self._pending: deque[bytes] = deque(maxlen=self._MAX_PENDING)
        self._dropped = 0
        self._lock = threading.Lock()

    def compose(self) -> ComposeResult:
        kind = "stack" if isinstance(self._target, Stack) else "conteneur"
        with Vertical(id="page"):
            yield Label(Text(f"Logs — {kind} {self._target.name}"), classes="title")
            with Horizontal(classes="button-bar"):
                yield Button("← Retour", id="back")
                yield Checkbox("Suivre", True, id="follow")
                yield Button("Effacer", id="clear")
            yield RichLog(
                id="log-output", max_lines=5_000, markup=False, highlight=False, classes="framed"
            )
            yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(RichLog).focus()
        self._set_status("⏳ Connexion au flux…")
        self.set_interval(self._FLUSH_INTERVAL, self._flush)
        self._stream()

    def on_unmount(self) -> None:
        self._closing = True
        if self._proc is not None:
            self._proc.terminate()

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(Text(text))

    @work(thread=True, exclusive=True, group="logs")
    def _stream(self) -> None:
        try:
            proc = engine.follow_logs(self._target)
        except DockerError as exc:
            call_ui(self, self._set_status, f"✗ {exc}")
            return
        self._proc = proc
        # écran fermé entre le lancement et l'affectation : on_unmount n'a pas vu le processus
        if self._closing:
            proc.terminate()
            return
        call_ui(self, self._set_status, "● Flux en direct")
        # lecture par blocs : découper des octets coûte peu, le décodage n'est fait
        # que pour les lignes affichées. Fin de boucle = processus mort (écran
        # fermé, conteneur arrêté…)
        fd = proc.stdout.fileno()
        rest = b""
        while chunk := os.read(fd, 65536):
            *lines, rest = (rest + chunk).split(b"\n")
            if lines:
                with self._lock:
                    overflow = len(self._pending) + len(lines) - self._MAX_PENDING
                    self._dropped += max(0, overflow)
                    self._pending.extend(lines)
        if rest:
            with self._lock:
                self._pending.append(rest)
        code = proc.wait()
        if not self._closing:
            call_ui(self, self._set_status, f"■ Fin du flux (code {code})")

    def _flush(self) -> None:
        with self._lock:
            lines = list(self._pending)
            self._pending.clear()
            dropped, self._dropped = self._dropped, 0
        if not lines and not dropped:
            return
        # couleurs ANSI éventuelles des applications conservées
        texts = [
            Text.from_ansi(_clean_log_line(line.decode(errors="replace"))) for line in lines
        ]
        if dropped:
            texts.insert(0, Text(
                f"… {dropped} ligne(s) non affichée(s) (débit trop élevé)",
                style="dim italic",
            ))
        # un seul write : un seul rendu et un seul défilement par passage
        self.query_one(RichLog).write(Text("\n").join(texts))

    # --- évènements / actions ---------------------------------------------------
    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        log = self.query_one(RichLog)
        log.auto_scroll = event.value
        if event.value:
            log.scroll_end(animate=False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "clear":
            self.action_clear()
        elif event.button.id == "back":
            self.action_close()

    def action_toggle_follow(self) -> None:
        follow = self.query_one("#follow", Checkbox)
        follow.value = not follow.value

    def action_clear(self) -> None:
        self.query_one(RichLog).clear()

    def action_close(self) -> None:
        self.app.pop_screen()


class InspectOutput(TextArea):
    """Sortie de `docker inspect` ; sans sélection, ctrl+shift+c copie tout le JSON."""

    def copy_text(self) -> str:
        return self.text


class InspectScreen(Screen):
    """`docker inspect` d'un conteneur ou des conteneurs d'une stack."""

    BINDINGS = [
        Binding("escape", "close", "Retour"),
        Binding("f4", "close", "Retour", show=False),
        Binding("f5", "reload", "Actualiser"),
    ]

    def __init__(self, target: Container | Stack):
        super().__init__()
        self._target = target

    def compose(self) -> ComposeResult:
        kind = "stack" if isinstance(self._target, Stack) else "conteneur"
        with Vertical(id="page"):
            yield Label(Text(f"Inspect — {kind} {self._target.name}"), classes="title")
            with Horizontal(classes="button-bar"):
                yield Button("← Retour", id="back")
                yield Button("Actualiser", id="reload")
            yield InspectOutput(
                id="inspect-output", read_only=True, show_line_numbers=True, classes="framed"
            )
            yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(InspectOutput).focus()
        self.action_reload()

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(Text(text))

    @work(thread=True, exclusive=True, group="inspect")
    def _load(self) -> None:
        try:
            output = engine.inspect(self._target)
        except DockerError as exc:
            # écran peut-être fermé avant la réponse de docker : call_ui
            call_ui(self, self._set_status, f"✗ {exc}")
            return
        call_ui(self, self._show, output)

    def _show(self, output: str) -> None:
        self.query_one(InspectOutput).load_text(output)
        self._set_status("✓ ctrl+shift+c copie la sélection, ou tout le JSON sans sélection")

    # --- évènements / actions ---------------------------------------------------
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "reload":
            self.action_reload()
        elif event.button.id == "back":
            self.action_close()

    def action_reload(self) -> None:
        self._set_status("⏳ docker inspect…")
        self._load()

    def action_close(self) -> None:
        self.app.pop_screen()


class ContainerTree(Tree):
    """Arbre stacks → conteneurs ; copie = nom du nœud courant."""

    def copy_text(self) -> str:
        node = self.cursor_node
        if node is None or node.data is None:
            return ""
        return node.data.name


class FilterPane(Vertical):
    """Onglet avec champ filtre au-dessus d'une liste (tableau ou arbre).

    Même comportement que les filtres des clients HTTP/SQL : ↑↓ depuis le
    champ déplacent le curseur, Suppr en fin de saisie supprime, une frappe
    sur la liste est redirigée vers le champ.
    """

    placeholder = "filtrer…"

    def compose_filter(self) -> ComposeResult:
        # select_on_focus=False : sinon la lettre redirigée depuis la liste
        # est sélectionnée au focus puis écrasée par la suivante
        yield Input(placeholder=self.placeholder, classes="filter", select_on_focus=False)

    @property
    def filter_input(self) -> Input:
        return self.query_one(".filter", Input)

    @property
    def list_widget(self) -> DataTable | Tree:
        raise NotImplementedError

    def filter_terms(self) -> list[str]:
        return self.filter_input.value.lower().split()

    def focus_filter(self) -> None:
        self.filter_input.focus()

    def clear_filter(self) -> None:
        self.filter_input.value = ""

    def refresh_view(self, filter_changed: bool = False) -> None:
        """Réaffiche la liste selon le filtre.

        Filtre modifié : curseur sur le 1er résultat (sauf filtre vidé) ;
        sinon (données rechargées) il reste sur le même élément si possible.
        """

    def action_delete(self) -> None:
        """Suppression de l'élément sous le curseur."""

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self.refresh_view(filter_changed=True)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Entrée dans le filtre : on passe sur la liste
        event.stop()
        self.list_widget.focus()

    def on_key(self, event: events.Key) -> None:
        widget = self.list_widget
        focused = self.screen.focused
        if focused is self.filter_input and event.key in ("up", "down"):
            if event.key == "up":
                widget.action_cursor_up()
            else:
                widget.action_cursor_down()
            event.stop()
        elif (
            focused is self.filter_input and event.key == "delete"
            and self.filter_input.cursor_position >= len(self.filter_input.value)
        ):
            # onglet affiché, pas forcément celui-ci : juste après un changement
            # d'onglet, le focus peut encore être dans le filtre de l'ancien
            self.screen.action_delete()
            event.stop()
        # frappe d'un caractère sur la liste : redirigée vers le filtre
        # (sauf espace, qui plie/déplie une stack)
        elif (
            focused is widget and event.is_printable
            and event.character and event.key != "space"
        ):
            self.filter_input.focus()
            self.filter_input.insert_text_at_cursor(event.character)
            event.stop()


class ResourcePane(FilterPane):
    """Liste filtrable d'une ressource Docker supprimable (image, réseau, volume)."""

    # (titre de colonne, largeur fixe ou None = auto)
    columns: list[tuple[str, int | None]] = []
    # libellés : « Sélectionne une image », « Supprimer l'image … ? », « 3 / 5 image(s) »
    a_noun = "un élément"
    the_noun = "l'élément"
    plural = "élément(s)"
    # sous-commande docker de suppression, reprise dans la ligne d'état
    rm_command = "rm"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._items: list = []
        # éléments affichés (après filtre), dans l'ordre des lignes
        self._visible: list = []
        # tri par clic sur l'en-tête : (index de colonne, décroissant) ; None = ordre docker
        self._sort: tuple[int, bool] | None = None

    def compose(self) -> ComposeResult:
        yield from self.compose_filter()
        yield CopyTable(cursor_type="cell", zebra_stripes=True, classes="resource-table framed")
        # même ordre que les autres TUI : action destructrice en dernier
        with Horizontal(classes="button-bar pane-buttons"):
            yield Button("Actualiser", classes="reload")
            yield Button("Supprimer", variant="error", classes="delete")
        yield Static("", classes="pane-count")

    def on_mount(self) -> None:
        self._add_columns()

    def _add_columns(self) -> None:
        """En-têtes, avec ▲/▼ sur la colonne triée."""
        table = self.list_widget
        for i, (title, width) in enumerate(self.columns):
            if self._sort is not None and self._sort[0] == i:
                title = f"{title} {'▼' if self._sort[1] else '▲'}"
            table.add_column(title, width=width)

    @property
    def list_widget(self) -> CopyTable:
        return self.query_one(CopyTable)

    # --- à définir par ressource -------------------------------------------
    def key(self, item) -> str:
        raise NotImplementedError

    def cells(self, item) -> list[str]:
        raise NotImplementedError

    def sort_value(self, item, column: int, cells: list[str]):
        """Clé de tri d'une colonne ; par défaut le texte affiché, sans casse."""
        return cells[column].lower()

    def filter_text(self, item, cells: list[str]) -> str:
        """Texte sur lequel porte le filtre ; par défaut les cellules affichées."""
        return " ".join(cells)

    def describe(self, item) -> str:
        """Libellé dans la confirmation de suppression."""
        return self.key(item)

    def delete_refusal(self, item) -> str:
        """Raison d'interdire la suppression ("" = autorisée)."""
        return ""

    def remove(self, item) -> None:
        raise NotImplementedError

    # --- affichage ---------------------------------------------------------
    def set_items(self, items: list) -> None:
        self._items = items
        self.refresh_view()

    def selected(self):
        table = self.list_widget
        row = table.cursor_row
        if table.row_count and 0 <= row < len(self._visible):
            return self._visible[row]
        return None

    def refresh_view(self, filter_changed: bool = False) -> None:
        table = self.list_widget
        current = self.selected()
        current_key = self.key(current) if current is not None else None
        column = table.cursor_column
        terms = self.filter_terms()
        if filter_changed and terms:
            current_key = None
        rows = [(item, self.cells(item)) for item in self._items]
        found = [(item, cells) for item, cells in rows if matches(self.filter_text(item, cells), terms)]
        if self._sort is not None:
            column, reverse = self._sort
            found.sort(key=lambda row: self.sort_value(row[0], column, row[1]), reverse=reverse)
        self._visible = [item for item, _ in found]
        # colonnes recréées : leur libellé porte l'indicateur de tri
        table.clear(columns=True)
        self._add_columns()
        for item, cells in found:
            # Text : un nom contenant `[` ne doit pas être lu comme du markup
            table.add_row(*(Text(c) for c in cells))
        keys = [self.key(item) for item in self._visible]
        row = keys.index(current_key) if current_key in keys else 0
        if keys:
            table.move_cursor(row=row, column=column)
        self.query_one(".pane-count", Static).update(
            f"{len(self._visible)} / {len(self._items)} {self.plural}"
        )

    # --- évènements ----------------------------------------------------------
    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        # 1er clic : croissant, 2e : décroissant, 3e : retour à l'ordre de docker
        event.stop()
        column = event.column_index
        if self._sort is None or self._sort[0] != column:
            self._sort = (column, False)
        elif not self._sort[1]:
            self._sort = (column, True)
        else:
            self._sort = None
        self.refresh_view()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.has_class("delete"):
            self.action_delete()
        elif event.button.has_class("reload"):
            self.screen.action_reload()

    def action_delete(self) -> None:
        item = self.selected()
        if item is None:
            self.app.notify(f"Sélectionne {self.a_noun}", severity="warning")
            return
        refusal = self.delete_refusal(item)
        if refusal:
            self.app.notify(refusal, severity="warning")
            return

        def done(confirmed: bool | None) -> None:
            if confirmed:
                label = f"{self.rm_command} {self.describe(item)}"
                self.screen.run_docker(label, self.remove, item)

        self.app.push_screen(
            ConfirmPrompt(f"Supprimer {self.the_noun} « {self.describe(item)} » ?", "Supprimer"),
            done,
        )


def _join(names: list[str]) -> str:
    return ", ".join(names) if names else "—"


_SIZE_UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12}


def _size_bytes(size: str) -> float:
    """« 1.2GB » -> octets (taille affichée par `docker image ls`), 0 si illisible."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([kmgt]?b)\s*", size.lower())
    return float(m[1]) * _SIZE_UNITS[m[2]] if m else 0.0


class ImagesPane(ResourcePane):
    placeholder = "filtrer les images (dépôt, tag, conteneur…)"
    a_noun, the_noun, plural = "une image", "l'image", "image(s)"
    rm_command = "image rm"
    columns = [
        ("Dépôt", None), ("Tag", None), ("ID", 12), ("Taille", None),
        ("Créée", None), ("Utilisée par", None),
    ]

    def key(self, item: Image) -> str:
        return f"{item.id}/{item.ref}"

    def cells(self, item: Image) -> list[str]:
        return [
            item.repository, item.tag, item.id, item.size, item.created,
            _join(item.used_by),
        ]

    def sort_value(self, item: Image, column: int, cells: list[str]):
        if column == 3:
            return _size_bytes(item.size)
        if column == 4:
            return item.created_at
        return super().sort_value(item, column, cells)

    def filter_text(self, item: Image, cells: list[str]) -> str:
        # « dépôt:tag » collé tel quel doit trouver l'image
        return f"{item.ref} {super().filter_text(item, cells)}"

    def describe(self, item: Image) -> str:
        return item.ref

    def remove(self, item: Image) -> None:
        engine.remove_image(item)


class NetworksPane(ResourcePane):
    placeholder = "filtrer les réseaux (nom, pilote, conteneur…)"
    a_noun, the_noun, plural = "un réseau", "le réseau", "réseau(x)"
    rm_command = "network rm"
    columns = [
        ("Nom", None), ("Pilote", None), ("Portée", None), ("ID", 12), ("Conteneurs", None),
    ]

    def key(self, item: Network) -> str:
        return item.id

    def cells(self, item: Network) -> list[str]:
        return [item.name, item.driver, item.scope, item.id, _join(item.containers)]

    def describe(self, item: Network) -> str:
        return item.name

    def delete_refusal(self, item: Network) -> str:
        if item.builtin:
            return f"Le réseau « {item.name} » est un réseau prédéfini de Docker"
        return ""

    def remove(self, item: Network) -> None:
        engine.remove_network(item)


class VolumesPane(ResourcePane):
    placeholder = "filtrer les volumes (nom, stack, conteneur, chemin…)"
    a_noun, the_noun, plural = "un volume", "le volume", "volume(s)"
    rm_command = "volume rm"
    # nom tronqué à l'affichage (volumes anonymes = 64 caractères hexa),
    # copié en entier
    columns = [
        ("Nom", 28), ("Stack", None), ("Pilote", None),
        ("Chemin sur disque", 48), ("Monté dans", None),
    ]

    def key(self, item: Volume) -> str:
        return item.name

    def cells(self, item: Volume) -> list[str]:
        uses = [f"{u.container} → {u.destination}" for u in item.uses]
        return [item.name, item.project or "—", item.driver, item.mountpoint, _join(uses)]

    def remove(self, item: Volume) -> None:
        engine.remove_volume(item)


class ContainersPane(FilterPane):
    """Arbre des stacks compose et de leurs conteneurs, avec détail du nœud courant."""

    placeholder = "filtrer les conteneurs (stack, service, image, état…)"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._snapshot: Snapshot | None = None
        # stacks repliées hors filtre (le filtre déplie tout, on restaure ensuite)
        self._collapsed: set[str] = set()
        self._filtering = False

    def compose(self) -> ComposeResult:
        with Horizontal(classes="filter-row"):
            yield from self.compose_filter()
            yield Checkbox("En route uniquement", True, id="running-only")
        with Horizontal(id="containers-body"):
            yield ContainerTree("stacks", id="containers", classes="framed")
            yield Static("", id="container-detail", classes="framed")
        # raccourcis affichés dans le pied de page, comme dans les autres TUI ;
        # action destructrice (Down) en dernier
        with Horizontal(classes="button-bar pane-buttons"):
            yield Button("Up", variant="success", id="up")
            yield Button("Stop", variant="warning", id="stop")
            yield Button("Logs", id="logs")
            yield Button("Inspect", id="inspect")
            yield Button("Actualiser", classes="reload")
            yield Button("Down", variant="error", id="down")
        yield Static("", classes="pane-count")

    def on_mount(self) -> None:
        tree = self.list_widget
        tree.show_root = False
        tree.root.expand()

    @property
    def list_widget(self) -> ContainerTree:
        return self.query_one(ContainerTree)

    # --- arbre -------------------------------------------------------------
    def set_snapshot(self, snapshot: Snapshot) -> None:
        self._snapshot = snapshot
        self.refresh_view()

    def _collapsed_stacks(self) -> set[str]:
        """Stacks repliées hors filtre (pendant un filtre : l'état d'avant)."""
        if self._filtering:
            return self._collapsed
        return {
            _node_key(node) for node in self.list_widget.root.children
            if not node.is_expanded
        }

    def refresh_view(self, filter_changed: bool = False) -> None:
        if self._snapshot is None:
            return
        terms = self.filter_terms()
        filtering = bool(terms)
        self._collapsed = self._collapsed_stacks()
        self._filtering = filtering
        node = self.list_widget.cursor_node
        keep = _node_key(node) if node is not None else None

        tree = self.list_widget
        tree.clear()
        tree.root.expand()
        groups: list[tuple[Stack | None, list[Container]]] = [
            (stack, stack.containers) for stack in self._snapshot.stacks()
        ]
        standalone = [c for c in self._snapshot.containers if not c.project]
        if standalone:
            groups.append((None, standalone))

        running_only = self.query_one("#running-only", Checkbox).value
        shown = 0
        for stack, containers in groups:
            found = [
                c for c in containers
                if (not running_only or c.state == "running")
                and matches(_container_text(c), terms)
            ]
            if not found:
                continue
            name = stack.name if stack else STANDALONE
            node = tree.root.add(_stack_label(name, containers), data=stack)
            for c in found:
                node.add_leaf(_container_label(c), data=c)
            shown += len(found)
            if filtering or _node_key(node) not in self._collapsed:
                node.expand()
        self._focus(None if filter_changed and filtering else keep)
        total = len(self._snapshot.containers)
        running = sum(c.state == "running" for c in self._snapshot.containers)
        self.query_one(".pane-count", Static).update(
            f"{shown} / {total} conteneur(s) — {running} actif(s)"
        )

    def _focus(self, key: str | None) -> None:
        """Curseur sur le nœud `key`, sinon sur le premier conteneur affiché."""
        tree = self.list_widget
        nodes = list(_iter_nodes(tree.root))
        target = next((n for n in nodes if _node_key(n) == key), None) if key else None
        if target is None:
            target = next((n for n in nodes if isinstance(n.data, Container)), None)
        if target is None:
            self._show_detail(None)
            return
        if target.parent is not None:
            target.parent.expand()
        # Tree calcule les lignes paresseusement : lire last_line force ce calcul,
        # sinon node.line vaut encore -1 et le curseur tombe sur la 1re ligne
        tree.last_line
        tree.move_cursor(target)
        self._show_detail(target)

    def _show_detail(self, node: TreeNode | None) -> None:
        detail = self.query_one("#container-detail", Static)
        data = node.data if node is not None else None
        if isinstance(data, Container):
            detail.update(_container_detail(data))
        elif isinstance(data, Stack):
            detail.update(_stack_detail(data))
        else:
            detail.update("")

    # --- évènements ----------------------------------------------------------
    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        self._show_detail(event.node)

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        event.stop()
        self.refresh_view()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id in ("up", "stop", "down", "logs", "inspect"):
            self.run_action(event.button.id)
        elif event.button.has_class("reload"):
            self.screen.action_reload()

    # --- actions -------------------------------------------------------------
    def action_delete(self) -> None:
        self.run_action("down")

    def run_action(self, action: str) -> None:
        """up / stop / down / logs / inspect sur la stack ou le conteneur sous le curseur."""
        node = self.list_widget.cursor_node
        data = node.data if node is not None else None
        screen = self.screen
        if action == "logs" and isinstance(data, (Stack, Container)):
            self.app.push_screen(LogScreen(data))
        elif action == "inspect" and isinstance(data, (Stack, Container)):
            self.app.push_screen(InspectScreen(data))
        elif isinstance(data, Stack):
            if action == "down":
                self.app.push_screen(
                    ConfirmPrompt(
                        f"docker compose down sur la stack « {data.name} » ?\n"
                        "Ses conteneurs et réseaux seront supprimés (pas les volumes).",
                        "Down",
                    ),
                    lambda ok: ok and screen.run_docker(
                        f"compose down {data.name}", engine.compose, data, "down"
                    ),
                )
            else:
                args = ("up", "-d") if action == "up" else ("stop",)
                screen.run_docker(f"compose {action} {data.name}", engine.compose, data, *args)
        elif isinstance(data, Container):
            if action == "up":
                screen.run_docker(f"start {data.name}", engine.start_container, data)
            elif action == "stop":
                screen.run_docker(f"stop {data.name}", engine.stop_container, data)
            else:
                self.app.push_screen(
                    ConfirmPrompt(f"Supprimer le conteneur « {data.name} » ?", "Supprimer"),
                    lambda ok: ok and screen.run_docker(
                        f"rm {data.name}", engine.remove_container, data
                    ),
                )
        else:
            self.app.notify(
                "Sélectionne une stack compose ou un conteneur", severity="warning"
            )


def _iter_nodes(root: TreeNode):
    """Nœuds sous `root`, dans l'ordre d'affichage."""
    for node in root.children:
        yield node
        yield from _iter_nodes(node)


def _node_key(node: TreeNode) -> str:
    if isinstance(node.data, Container):
        return f"c:{node.data.id}"
    if isinstance(node.data, Stack):
        return f"s:{node.data.name}"
    return f"s:{STANDALONE}"


def _container_text(c: Container) -> str:
    """Texte sur lequel porte le filtre des conteneurs."""
    return " ".join((c.project or STANDALONE, c.service, c.name, c.image, c.state, c.ports))


def _stack_label(name: str, containers: list[Container]) -> Text:
    running = sum(c.state == "running" for c in containers)
    style = "green" if running == len(containers) else "yellow" if running else "red"
    label = Text(name, style="bold")
    label.append(f"  {running}/{len(containers)} actif(s)", style=style)
    return label


def _container_label(c: Container) -> Text:
    label = Text("● ", style=STATE_STYLES.get(c.state, "yellow"))
    label.append(c.name)
    return label


def _detail_line(text: Text, key: str, value: str) -> None:
    text.append(f"{key} : ", style="bold")
    text.append(f"{value}\n")


def _container_detail(c: Container) -> Text:
    text = Text()
    _detail_line(text, "Conteneur", c.name)
    _detail_line(text, "ID", c.id)
    _detail_line(text, "Image", c.image)
    _detail_line(text, "État", c.status)
    if c.project:
        _detail_line(text, "Stack", f"{c.project} / {c.service}")
    _detail_line(text, "Ports", c.ports or "—")
    _detail_line(text, "Réseaux", _join(c.networks))
    text.append("Montages :\n", style="bold")
    for m in c.mounts:
        text.append(f"  [{m.type}] ", style="dim")
        text.append(f"{m.source} → {m.destination}\n")
    if not c.mounts:
        text.append("  —\n")
    return text


def _stack_detail(s: Stack) -> Text:
    text = Text()
    _detail_line(text, "Stack", s.name)
    _detail_line(text, "Dossier", s.working_dir or "—")
    text.append("Fichiers compose :\n", style="bold")
    for f in s.config_files:
        text.append(f"  {f}\n")
    _detail_line(text, "Conteneurs", f"{s.running}/{len(s.containers)} actif(s)")
    return text


class DockerScreen(Screen):
    """Écran principal plein écran : un onglet par type de ressource."""

    BINDINGS = [
        Binding("f5", "reload", "Actualiser"),
        Binding("f6", "container('up')", "Up"),
        Binding("f7", "container('stop')", "Stop"),
        Binding("f8", "container('down')", "Down"),
        Binding("f9", "container('logs')", "Logs"),
        Binding("f4", "container('inspect')", "Inspect"),
        Binding("delete", "delete", "Supprimer"),
        Binding("escape", "clear_filter", "Effacer filtre"),
        Binding("ctrl+pagedown", "next_tab(1)", "Onglet suivant"),
        Binding("ctrl+pageup", "next_tab(-1)", "Onglet précédent"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="page"):
            with TabbedContent(id="tabs"):
                with TabPane("Conteneurs", id="tab-containers"):
                    yield ContainersPane(id="containers-pane")
                with TabPane("Images", id="tab-images"):
                    yield ImagesPane(id="images-pane")
                with TabPane("Réseaux", id="tab-networks"):
                    yield NetworksPane(id="networks-pane")
                with TabPane("Volumes", id="tab-volumes"):
                    yield VolumesPane(id="volumes-pane")
            yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.active_pane().focus_filter()
        self.action_reload()

    def active_pane(self) -> FilterPane:
        pane = self.query_one("#tabs", TabbedContent).active_pane
        return pane.query_one(FilterPane)

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(Text(text))

    # --- chargement ----------------------------------------------------------
    def action_reload(self) -> None:
        self._load()

    @work(thread=True, exclusive=True, group="reload")
    def _load(self) -> None:
        try:
            snap = engine.snapshot()
        except DockerError as exc:
            self.app.call_from_thread(self._set_status, f"✗ Docker inaccessible : {exc}")
            return
        self.app.call_from_thread(self._apply, snap)

    def _apply(self, snap: Snapshot) -> None:
        self.query_one(ContainersPane).set_snapshot(snap)
        self.query_one(ImagesPane).set_items(snap.images)
        self.query_one(NetworksPane).set_items(snap.networks)
        self.query_one(VolumesPane).set_items(snap.volumes)

    def run_docker(self, label: str, func: Callable, *args) -> None:
        """Lance une commande docker en arrière-plan, puis rafraîchit l'affichage."""
        self._set_status(f"⏳ {label}…")
        self._run_docker(label, func, *args)

    # pas exclusive : plusieurs stacks peuvent démarrer en parallèle
    @work(thread=True, group="docker")
    def _run_docker(self, label: str, func: Callable, *args) -> None:
        try:
            func(*args)
        except DockerError as exc:
            self.app.call_from_thread(self._set_status, f"✗ {label} : {exc}")
            self.app.call_from_thread(
                self.app.notify, str(exc), title=label, severity="error", timeout=8
            )
        else:
            self.app.call_from_thread(self._set_status, f"✓ {label}")
        self.app.call_from_thread(self.action_reload)

    # --- évènements ----------------------------------------------------------
    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self.active_pane().focus_filter()
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # F4, F6-F9 : seulement sur l'onglet Conteneurs (masqués du pied de page sinon)
        if action == "container":
            try:
                return isinstance(self.active_pane(), ContainersPane)
            except NoMatches:  # appelé avant que les onglets soient montés
                return False
        return True

    # --- actions -------------------------------------------------------------
    def action_container(self, action: str) -> None:
        pane = self.active_pane()
        if isinstance(pane, ContainersPane):
            pane.run_action(action)

    def action_delete(self) -> None:
        self.active_pane().action_delete()

    def action_clear_filter(self) -> None:
        pane = self.active_pane()
        pane.clear_filter()
        pane.focus_filter()

    def action_next_tab(self, step: int) -> None:
        # actions natives des onglets : `TabbedContent.active = …` modifié deux
        # fois de suite perd parfois le 2e changement
        tabs = self.query_one("#tabs", TabbedContent).query_one(Tabs)
        if step > 0:
            tabs.action_next_tab()
        else:
            tabs.action_previous_tab()


class DockerTui(BaseApp):
    CSS_PATH = [COMMON_CSS, "app.tcss"]
    TITLE = "Docker TUI"

    def on_mount(self) -> None:
        self.push_screen(DockerScreen())


def main() -> None:
    DockerTui().run()


if __name__ == "__main__":
    main()
