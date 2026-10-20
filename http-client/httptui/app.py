"""TUI client HTTP : éditeur de requêtes, import curl, fuzzing."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from pathlib import Path

from rich.text import Text
from textual import events, work
from textual.app import ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.timer import Timer
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Input,
    Label,
    Select,
    TabbedContent,
    TabPane,
    TextArea,
    Tree,
)
from textual.widgets.tree import TreeNode

from tui_common import (
    COMMON_CSS,
    AutoWidthTable,
    BaseApp,
    ConfirmPrompt,
    matches,
    to_clipboard,
)

from .client import HttpResult, make_client, send_request
from .fuzzer import Fuzzer
from .curlimport import parse_curl, request_name
from .httpfile import (
    METHODS,
    HttpRequest,
    delete_request,
    format_headers,
    list_request_folders,
    load_requests_dir,
    parse_headers,
    save_request,
    split_request_path,
)

# colonnes du tableau de fuzzing : (clé de tri, libellé affiché)
FUZZ_COLUMNS = [
    ("idx", "#"),
    ("fuzz", "Fuzz"),
    ("status", "Statut"),
    ("time", "Temps (ms)"),
    ("size", "Taille"),
]

# corps de réponse conservé par requête, borné pour éviter les abus mémoire
_MAX_BODY_KEEP = 200_000

# corps affiché dans l'onglet Réponse : au-delà, TextArea rame (coloration, défilement)
_MAX_BODY_SHOW = 1_000_000

# délai d'attente avant d'appliquer le filtre de recherche (debounce)
_FILTER_DEBOUNCE = 0.25

# nombre de requêtes de fuzzing envoyées en parallèle (borne la charge)
_FUZZ_CONCURRENCY = 16


@dataclass
class FuzzRow:
    """Une requête fuzzée et sa réponse (corps tronqué à `_MAX_BODY_KEEP`)."""

    idx: int
    label: str
    url: str
    result: HttpResult
    # corps en minuscules, calculé une fois : sert au filtre de recherche
    body_lower: str = field(init=False)

    def __post_init__(self) -> None:
        self.body_lower = self.result.body.lower()

    def cells(self) -> tuple[str, ...]:
        """Cellules affichées, dans l'ordre de `FUZZ_COLUMNS`."""
        r = self.result
        status = r.reason if r.error else str(r.status)
        return (str(self.idx), self.label, status, f"{r.elapsed_ms:.0f}", str(r.size))


def _request_path(req: HttpRequest) -> str:
    """Chemin `dossier/nom` d'une requête : texte sur lequel porte le filtre."""
    return f"{req.folder}/{req.name}" if req.folder else req.name


def _request_label(req: HttpRequest) -> Text:
    # Text plutôt que str : un nom contenant `[` ne doit pas être lu comme du markup
    return Text(f"{req.method}  {req.name}")


class PathPrompt(ModalScreen[tuple[str, str] | None]):
    """Petite fenêtre pour saisir une valeur (chemin, nom…).

    Résultat : (id du bouton « ok » ou « alt », saisie), ou None si annulé.
    """

    def __init__(
        self,
        label: str,
        placeholder: str = "",
        ok_label: str = "OK",
        value: str = "",
        alt_label: str = "",
    ):
        super().__init__()
        self._label = label
        self._placeholder = placeholder
        self._ok_label = ok_label
        self._value = value
        # bouton secondaire optionnel : renvoie ("alt", saisie), même vide
        self._alt_label = alt_label

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._label, classes="section")
            yield Input(self._value, placeholder=self._placeholder, id="path")
            with Horizontal(id="dialog-buttons"):
                yield Button(self._ok_label, variant="primary", id="ok")
                if self._alt_label:
                    yield Button(self._alt_label, id="alt")
                yield Button("Annuler", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#path", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        value = self.query_one("#path", Input).value
        if event.button.id == "alt":
            self.dismiss(("alt", value))
        elif event.button.id == "ok" and value:
            self.dismiss(("ok", value))
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(("ok", event.value) if event.value else None)


class RequestTree(Tree):
    """Arbre des requêtes : un clic sur une requête la sélectionne sans l'ouvrir.

    L'ouverture passe par Entrée (`select_cursor` → `NodeSelected`) ;
    un clic sur un dossier le plie/déplie toujours.
    """

    async def _on_click(self, event: events.Click) -> None:
        line = event.style.meta.get("line")
        node = self.get_node_at_line(line) if line is not None else None
        if node is not None and isinstance(node.data, HttpRequest):
            self.cursor_line = line
            # empêche Tree._on_click (appelé ensuite via la MRO) d'émettre NodeSelected
            event.prevent_default()


class CurlPrompt(ModalScreen[tuple[str, str] | None]):
    """Collage d'une commande curl + chemin (dossier/nom) où l'enregistrer."""

    BINDINGS = [Binding("escape", "cancel", "Annuler")]

    def __init__(self, target: str = ""):
        super().__init__()
        self._target = target

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide"):
            yield Label(
                "Commande curl (navigateur → « Copier en tant que cURL (bash) »)",
                classes="section",
            )
            yield TextArea(id="curl-command", soft_wrap=True, classes="framed")
            yield Label("Enregistrer sous (dossier/nom — vide = nom auto)", classes="section")
            yield Input(self._target, placeholder="dossier/ma-requête", id="target")
            with Horizontal(id="dialog-buttons"):
                yield Button("Importer", variant="primary", id="ok")
                yield Button("Annuler", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#curl-command", TextArea).focus()

    def _submit(self) -> None:
        command = self.query_one("#curl-command", TextArea).text
        target = self.query_one("#target", Input).value
        self.dismiss((command, target) if command.strip() else None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._submit()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)


class RequestListScreen(Screen):
    """Écran d'accueil plein écran : choix (ou création) de la requête à ouvrir."""

    BINDINGS = [
        Binding("ctrl+n", "new", "Nouvelle"),
        Binding("ctrl+o", "import_curl", "Import curl"),
        Binding("f5", "reload", "Actualiser"),
        Binding("delete", "delete", "Supprimer"),
        Binding("escape", "clear_filter", "Effacer filtre"),
    ]

    def __init__(self) -> None:
        super().__init__()
        # arbre des requêtes : nœud par dossier ("" = racine) ; dossiers sur disque
        self._folder_nodes: dict[str, TreeNode] = {}
        self._folders: list[str] = []
        # dossiers dépliés hors filtre (le filtre déplie tout, on restaure ensuite)
        self._expanded: set[str] = set()
        self._filtering = False

    def compose(self) -> ComposeResult:
        with Vertical(id="list-page"):
            yield Label("HTTP TUI — Requêtes", classes="title")
            yield Label(
                "Tape pour filtrer · ↑↓ pour choisir · Entrée pour ouvrir · Suppr pour supprimer",
                classes="hint",
            )
            # select_on_focus=False : sinon la lettre redirigée depuis l'arbre
            # est sélectionnée au focus puis écrasée par la suivante
            yield Input(
                placeholder="filtrer les requêtes…", id="request-filter",
                select_on_focus=False,
            )
            yield RequestTree("requests", id="requests", classes="framed")
            with Horizontal(classes="button-row"):
                yield Button("Nouvelle", id="new", variant="success")
                yield Button("Ouvrir", id="open", variant="primary")
                yield Button("Import curl", id="curl")
                yield Button("Actualiser", id="reload")
                yield Button("Supprimer", id="delete", variant="error")
        yield Footer()

    def on_mount(self) -> None:
        self._load_requests()
        self.query_one("#request-filter", Input).focus()

    def _load_requests(
        self,
        select: tuple[str, str] | None = None,
        expanded: set[str] = frozenset(),
    ) -> None:
        """(Re)lit le disque et reconstruit l'arbre.

        `select` = (dossier, nom) de la requête sous le curseur (sinon la 1re) ;
        `expanded` = dossiers à garder dépliés.
        """
        self.app.requests = load_requests_dir(self.app.requests_dir)
        self._folders = list_request_folders(self.app.requests_dir)
        self._expanded = set(expanded)
        self._build_tree()
        target = next(
            (r for r in self.app.requests if (r.folder, r.name) == select), None
        )
        self._focus_request(target)

    # --- arbre des requêtes ---------------------------------------------
    def _filter_terms(self) -> list[str]:
        return self.query_one("#request-filter", Input).value.lower().split()

    def _build_tree(self) -> None:
        """(Re)construit l'arbre depuis `app.requests`, restreint par le filtre."""
        tree = self.query_one("#requests", Tree)
        tree.clear()
        tree.show_root = False
        tree.root.expand()
        self._folder_nodes = {"": tree.root}
        terms = self._filter_terms()
        if terms:
            # seuls les dossiers menant à un résultat, tous dépliés ;
            # créés avant les requêtes pour garder l'ordre dossiers d'abord
            found = [r for r in self.app.requests if matches(_request_path(r), terms)]
            needed = {r.folder for r in found}
            for folder in self._folders:
                if folder in needed:
                    self._folder_node(folder)
            for req in found:
                self._add_request_node(req)
            for node in self._folder_nodes.values():
                node.expand()
            return
        # dossiers d'abord (y compris vides) : ils précèdent les fichiers
        # de leur parent, comme dans un explorateur
        for folder in self._folders:
            self._folder_node(folder)
        for folder in self._expanded:
            if folder in self._folder_nodes:
                self._folder_nodes[folder].expand()
        for req in self.app.requests:
            self._add_request_node(req)

    def _expanded_folders(self) -> set[str]:
        """Dossiers dépliés hors filtre (pendant un filtre : l'état d'avant)."""
        if self._filtering:
            return self._expanded
        return {f for f, n in self._folder_nodes.items() if f and n.is_expanded}

    def _refresh_filter(self) -> None:
        filtering = bool(self._filter_terms())
        if filtering and not self._filtering:
            # mémorise les dossiers dépliés avant que le filtre ne déplie tout
            self._expanded = self._expanded_folders()
        self._filtering = filtering
        node = self._cursor_node()
        under_cursor = node.data if node is not None else None
        self._build_tree()
        # filtre : 1er résultat ; filtre vidé : on reste sur la requête choisie
        keep = under_cursor if not filtering and isinstance(under_cursor, HttpRequest) else None
        self._focus_request(keep)

    def _iter_nodes(self):
        """Nœuds de l'arbre, dans l'ordre d'affichage."""
        pending = [self.query_one("#requests", Tree).root]
        while pending:
            node = pending.pop()
            yield node
            pending.extend(reversed(node.children))

    def _node_for(self, req: HttpRequest) -> TreeNode | None:
        """Nœud affichant `req` (par identité), None s'il est masqué par le filtre."""
        return next((n for n in self._iter_nodes() if n.data is req), None)

    def _folder_node(self, folder: str) -> TreeNode:
        """Nœud du dossier `folder`, créé (avec ses parents) au besoin."""
        node = self._folder_nodes.get(folder)
        if node is None:
            parent, _, leaf = folder.rpartition("/")
            node = self._folder_node(parent).add(
                Text(f"{leaf}/", style="bold"), data=folder
            )
            self._folder_nodes[folder] = node
        return node

    def _add_request_node(self, req: HttpRequest) -> TreeNode:
        return self._folder_node(req.folder).add_leaf(_request_label(req), data=req)

    def _focus_request(self, req: HttpRequest | None) -> None:
        """Curseur sur `req` si elle est affichée, sinon sur la 1re requête visible."""
        node = self._node_for(req) if req is not None else None
        if node is None:
            node = next(
                (n for n in self._iter_nodes() if isinstance(n.data, HttpRequest)), None
            )
        if node is None:
            return
        parent = node.parent
        while parent is not None:
            parent.expand()
            parent = parent.parent
        tree = self.query_one("#requests", Tree)
        # Tree calcule les lignes paresseusement : lire last_line force ce calcul,
        # sinon node.line vaut encore -1 et le curseur tombe sur la 1re ligne
        tree.last_line
        tree.move_cursor(node)

    def _cursor_node(self) -> TreeNode | None:
        return self.query_one("#requests", Tree).cursor_node

    def _selected_folder(self) -> str:
        """Dossier sous le curseur (celui de la requête si c'est une requête)."""
        node = self._cursor_node()
        if node is None:
            return ""
        if isinstance(node.data, HttpRequest):
            return node.data.folder
        return node.data or ""

    # --- ouverture / retour ----------------------------------------------
    def _open_request(self, req: HttpRequest) -> None:
        self.app.push_screen(RequestScreen(req), self._request_closed)

    def _request_closed(self, req: HttpRequest | None) -> None:
        """Retour de l'éditeur : `req` = dernière version (sauvée ou non) de la requête."""
        # reconstruit : le libellé (méthode) a pu changer à la sauvegarde
        self._build_tree()
        self._focus_request(req)
        self.query_one("#request-filter", Input).focus()

    def _add_and_open(self, req: HttpRequest) -> None:
        """Nouvelle requête : en mémoire (même hors filtre), puis ouverte."""
        self.app.requests.append(req)
        self._add_request_node(req)
        self._open_request(req)

    # --- évènements ------------------------------------------------------
    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        # un dossier se déplie tout seul ; seule une requête ouvre l'éditeur
        if isinstance(event.node.data, HttpRequest):
            self._open_request(event.node.data)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "request-filter":
            self._refresh_filter()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Entrée dans le filtre = comme Entrée sur l'arbre (ouvre / déplie)
        if event.input.id == "request-filter":
            self.query_one("#requests", Tree).action_select_cursor()

    def on_key(self, event) -> None:
        tree = self.query_one("#requests", Tree)
        filter_input = self.query_one("#request-filter", Input)
        # ↑↓ dans le filtre : déplace le curseur de l'arbre sans quitter la saisie
        if self.focused is filter_input and event.key in ("up", "down"):
            if event.key == "up":
                tree.action_cursor_up()
            else:
                tree.action_cursor_down()
            event.stop()
        # Suppr dans le filtre : supprime la requête s'il n'y a rien à effacer à droite
        elif (
            self.focused is filter_input and event.key == "delete"
            and filter_input.cursor_position >= len(filter_input.value)
        ):
            self.action_delete()
            event.stop()
        # frappe d'un caractère sur l'arbre : redirigée vers le filtre
        # (sauf espace, qui plie/déplie un dossier)
        elif (
            self.focused is tree and event.is_printable
            and event.character and event.key != "space"
        ):
            filter_input.focus()
            filter_input.insert_text_at_cursor(event.character)
            event.stop()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        handlers = {
            "new": self.action_new,
            "open": self.query_one("#requests", Tree).action_select_cursor,
            "curl": self.action_import_curl,
            "reload": self.action_reload,
            "delete": self.action_delete,
        }
        handler = handlers.get(event.button.id or "")
        if handler:
            handler()

    # --- actions ---------------------------------------------------------
    def _valid_parts(self, raw: str) -> list[str] | None:
        """Segments de `a/b/c`, ou None (avec alerte) si un segment est invalide."""
        try:
            return split_request_path(raw)
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return None

    def _parse_request_path(self, raw: str) -> tuple[str, str] | None:
        """Valide `dossier/…/nom` (unique) et renvoie (dossier, nom), ou None."""
        # `dossier/` seul (champ pré-rempli validé tel quel) : sans ce test,
        # le dossier deviendrait le nom d'une requête créée à la racine
        if raw.strip().endswith("/"):
            self.notify("Nom de requête manquant après le dossier", severity="error")
            return None
        parts = self._valid_parts(raw)
        if parts is None:
            return None
        folder, name = "/".join(parts[:-1]), parts[-1]
        if any(r.folder == folder and r.name == name for r in self.app.requests):
            shown = f"{folder}/{name}" if folder else name
            self.notify(f"Une requête « {shown} » existe déjà", severity="error")
            return None
        return folder, name

    def _unique_name(self, raw: str, folder: str = "") -> str:
        """Rend un nom de fichier valide et unique dans `folder` (import curl surtout)."""
        base = raw.strip().replace("/", "_").replace("\\", "_") or "requête"
        name, n = base, 2
        existing = {r.name for r in self.app.requests if r.folder == folder}
        while name in existing:
            name = f"{base} ({n})"
            n += 1
        return name

    def action_new(self) -> None:
        def done(result: tuple[str, str] | None) -> None:
            if result is None:
                return
            button, raw = result
            # « Import curl » : ouvert ici (et non depuis la fenêtre, déjà fermée,
            # qui emporterait le callback de résultat de CurlPrompt avec elle)
            if button == "alt":
                self.action_import_curl(raw)
                return
            parsed = self._parse_request_path(raw)
            if parsed is None:
                return
            folder, name = parsed
            self._add_and_open(HttpRequest(name=name, folder=folder))
            self.notify(f"Requête « {name} » créée — ctrl+s pour l'enregistrer")

        # pré-rempli avec le dossier sélectionné ; `/` crée des sous-dossiers
        folder = self._selected_folder()
        self.app.push_screen(
            PathPrompt(
                "Nom de la nouvelle requête (dossier/nom)",
                placeholder="dossier/ma-requête", ok_label="Créer",
                value=f"{folder}/" if folder else "",
                alt_label="Import curl",
            ),
            done,
        )

    def action_import_curl(self, target: str | None = None) -> None:
        """Import d'une commande curl ; `target` pré-remplit le chemin d'enregistrement."""
        def done(result: tuple[str, str] | None) -> None:
            if not result:
                return
            command, target = result
            try:
                req, warnings = parse_curl(command)
            except ValueError as exc:
                self.notify(f"Commande curl illisible : {exc}", severity="error")
                return
            target = target.strip()
            if not target or target.endswith("/"):
                # dossier seul : nom déduit de l'URL, rendu unique
                parts = self._valid_parts(target) if target.strip("/") else []
                if parts is None:
                    return
                req.folder = "/".join(parts)
                req.name = self._unique_name(request_name(req), req.folder)
            else:
                parsed = self._parse_request_path(target)
                if parsed is None:
                    return
                req.folder, req.name = parsed
            try:
                path = save_request(self.app.requests_dir, req)
            except OSError as exc:
                self.notify(f"Enregistrement impossible : {exc}", severity="error")
                return
            self._add_and_open(req)
            self.notify(f"Requête importée dans {path}")
            for warning in warnings:
                self.notify(warning, severity="warning")

        if target is None:
            folder = self._selected_folder()
            target = f"{folder}/" if folder else ""
        self.app.push_screen(CurlPrompt(target), done)

    def action_delete(self) -> None:
        """Supprime (après confirmation) la requête sous le curseur, fichier compris."""
        node = self._cursor_node()
        req = node.data if node is not None else None
        if not isinstance(req, HttpRequest):
            self.notify("Sélectionne une requête à supprimer", severity="warning")
            return

        def done(confirmed: bool | None) -> None:
            if not confirmed:
                return
            try:
                delete_request(self.app.requests_dir, req)
            except OSError as exc:
                self.notify(f"Suppression impossible : {exc}", severity="error")
                return
            # curseur ensuite sur la requête suivante affichée (sinon la précédente)
            shown = [n.data for n in self._iter_nodes() if isinstance(n.data, HttpRequest)]
            i = next(k for k, r in enumerate(shown) if r is req)
            if i + 1 < len(shown):
                neighbour = shown[i + 1]
            elif i > 0:
                neighbour = shown[i - 1]
            else:
                neighbour = None
            self.app.requests = [r for r in self.app.requests if r is not req]
            node.remove()
            self._focus_request(neighbour)
            self.notify(f"Requête « {_request_path(req)} » supprimée")

        self.app.push_screen(
            ConfirmPrompt(f"Supprimer la requête « {_request_path(req)} » ?", "Supprimer"),
            done,
        )

    def action_clear_filter(self) -> None:
        self.query_one("#request-filter", Input).value = ""

    def action_reload(self) -> None:
        """Relit les `.http` (modifiés par un éditeur externe) sans perdre la vue."""
        node = self._cursor_node()
        current = node.data if node is not None else None
        select = (current.folder, current.name) if isinstance(current, HttpRequest) else None
        self._load_requests(select=select, expanded=self._expanded_folders())
        self.notify(f"{len(self.app.requests)} requête(s) rechargée(s) depuis le disque")


class RequestScreen(Screen[HttpRequest]):
    """Écran de travail sur une requête : édition, envoi, fuzzing.

    Renvoie (dismiss) la dernière version de la requête à l'écran de liste.
    """

    BINDINGS = [
        Binding("ctrl+r", "send", "Envoyer"),
        Binding("ctrl+f", "fuzz", "Fuzzer"),
        Binding("ctrl+s", "save", "Enregistrer"),
        Binding("escape", "back", "Requêtes"),
    ]

    def __init__(self, request: HttpRequest):
        super().__init__()
        # version de référence (sur disque, ou créée) ; l'éditeur porte les modifications
        self._request = request
        # True une fois averti que les modifications non sauvées seront perdues
        self._discard_warned = False
        # résultats de fuzzing + état d'affichage (filtre / tri)
        self.fuzz_rows: list[FuzzRow] = []
        self._fuzz_filter = ""
        self._fuzz_sort: tuple[str, bool] | None = None
        self._filter_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="editor"):
            yield Label(Text(_request_path(self._request)), classes="title")
            with Horizontal(id="request-line"):
                yield Select(
                    [(m, m) for m in METHODS], id="method",
                    value="GET", allow_blank=False,
                )
                yield Input(placeholder="https://… (supporte {{fuzz:liste}})", id="url")
            with Horizontal(id="actions", classes="button-bar"):
                yield Button("Envoyer", id="send", variant="primary")
                yield Button("Fuzzer", id="fuzz", variant="warning")
                yield Button("Enregistrer", id="save", variant="success")
                # choisir une liste copie sa balise {{fuzz:…}}, à coller où l'on veut
                yield Select(
                    [(n, n) for n in self.app.fuzzer.available()],
                    id="fuzzlist", prompt="Balise {{fuzz:…}}", allow_blank=True,
                )
            # onglets principaux ; « Requête » contient Headers / Body
            with TabbedContent(id="main-tabs"):
                with TabPane("Requête", id="tab-request"):
                    with TabbedContent(id="request-parts"):
                        with TabPane("Headers", id="tab-headers"):
                            yield TextArea(id="headers", classes="editor-area framed")
                        with TabPane("Body", id="tab-body"):
                            yield TextArea(id="body", language="json", classes="editor-area framed")
                with TabPane("Réponse", id="tab-response"):
                    yield TextArea(id="response", read_only=True, classes="editor-area framed")
                with TabPane("Fuzzing", id="tab-fuzz"):
                    yield Input(
                        placeholder="filtrer par contenu de la réponse…",
                        id="fuzz-search",
                    )
                    yield AutoWidthTable(id="fuzz-results", cursor_type="row", classes="framed")
        yield Footer()

    def on_mount(self) -> None:
        req = self._request
        self.query_one("#method", Select).value = req.method
        self.query_one("#url", Input).value = req.url
        self.query_one("#headers", TextArea).text = req.headers_text()
        self.query_one("#body", TextArea).text = req.body
        self._build_fuzz_columns()
        self.query_one("#url", Input).focus()

    def _current_request(self) -> HttpRequest:
        """Requête ouverte (nom, dossier) avec les champs de l'éditeur."""
        return replace(
            self._request,
            method=str(self.query_one("#method", Select).value or "GET"),
            url=self.query_one("#url", Input).value,
            headers=parse_headers(self.query_one("#headers", TextArea).text.splitlines()),
            body=self.query_one("#body", TextArea).text,
        )

    # --- évènements ------------------------------------------------------
    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "url":
            self._discard_warned = False
        # filtre sur le contenu de la réponse, avec debounce :
        # on annule le rendu en attente et on le replanifie à chaque frappe,
        # pour ne re-rendre qu'une fois la saisie stabilisée.
        if event.input.id == "fuzz-search":
            self._fuzz_filter = event.value
            if self._filter_timer is not None:
                self._filter_timer.stop()
            self._filter_timer = self.set_timer(
                _FILTER_DEBOUNCE, self._render_fuzz_table
            )

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id in ("headers", "body"):
            self._discard_warned = False

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "method":
            self._discard_warned = False
        elif event.select.id == "fuzzlist" and not event.select.is_blank():
            tag = f"{{{{fuzz:{event.value}}}}}"
            to_clipboard(self.app, tag)
            self.notify(f"{tag} copié dans le presse-papier", timeout=2)
            # retour au libellé : la même liste peut être recopiée
            event.select.clear()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # clic sur une ligne -> détail de la réponse dans l'onglet Réponse
        if event.data_table.id != "fuzz-results":
            return
        # clé de ligne = str(idx), posée par `_render_fuzz_table` / `_append_fuzz_row`
        idx = int(event.row_key.value)
        row = next((r for r in self.fuzz_rows if r.idx == idx), None)
        if row is None:
            return
        self.query_one("#response", TextArea).text = self._format_fuzz_detail(row)
        self.query_one("#main-tabs", TabbedContent).active = "tab-response"

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        # clic sur un en-tête -> tri (toggle croissant/décroissant)
        if event.data_table.id != "fuzz-results":
            return
        key = str(event.column_key.value)
        if self._fuzz_sort and self._fuzz_sort[0] == key:
            self._fuzz_sort = (key, not self._fuzz_sort[1])
        else:
            self._fuzz_sort = (key, False)
        self._render_fuzz_table()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        handlers = {
            "send": self.action_send,
            "fuzz": self.action_fuzz,
            "save": self.action_save,
        }
        handler = handlers.get(event.button.id or "")
        if handler:
            handler()

    # --- actions ---------------------------------------------------------
    def action_back(self) -> None:
        # modifications non sauvées : on avertit une fois, Échap à nouveau confirme
        if self._current_request() != self._request and not self._discard_warned:
            self._discard_warned = True
            self.notify(
                "Modifications non enregistrées : ctrl+s pour enregistrer, Échap à nouveau pour les abandonner",
                severity="warning",
            )
            return
        self.dismiss(self._request)

    def action_save(self) -> None:
        # le nom et le dossier (= chemin du fichier) restent ceux de la requête ouverte
        old = self._request
        edited = self._current_request()
        try:
            path = save_request(self.app.requests_dir, edited)
        except OSError as exc:
            self.notify(f"Enregistrement impossible : {exc}", severity="error")
            return
        # recherche par identité : deux requêtes peuvent être égales champ à champ
        index = next(i for i, r in enumerate(self.app.requests) if r is old)
        self.app.requests[index] = edited
        self._request = edited
        self.notify(f"Enregistrée dans {path}")

    def _reload_fuzz_lists(self) -> None:
        """Relit le répertoire de listes (fichiers ajoutés/modifiés à chaud)."""
        app = self.app
        old_names = app.fuzzer.available()
        app.fuzzer = Fuzzer.from_directory(app.fuzz_dir)
        names = app.fuzzer.available()
        # set_options réinitialise la sélection : seulement si les listes changent
        if names != old_names:
            self.query_one("#fuzzlist", Select).set_options([(n, n) for n in names])

    def _check_fuzz_lists(self, request: HttpRequest) -> bool:
        """Faux (avec alerte) si une balise vise une liste absente du répertoire."""
        missing = self.app.fuzzer.missing_lists(request)
        if missing:
            self.notify(
                f"Liste(s) introuvable(s) dans {self.app.fuzz_dir} : {', '.join(missing)}",
                severity="error",
            )
        return not missing

    def _prepare_combos(self) -> list[tuple[dict[str, str], HttpRequest]] | None:
        """Requête de l'éditeur développée en combinaisons (None si liste absente)."""
        request = self._current_request()
        self._reload_fuzz_lists()
        if not self._check_fuzz_lists(request):
            return None
        return self.app.fuzzer.expand(request)

    def action_send(self) -> None:
        combos = self._prepare_combos()
        if combos is None:
            return
        if len(combos) > 1:
            self.notify(f"{len(combos)} combinaisons détectées — envoi de la 1re (ctrl+f pour tout fuzzer)")
        _, concrete = combos[0]
        self.query_one("#main-tabs", TabbedContent).active = "tab-response"
        self._do_send(concrete)

    @work(exclusive=True)
    async def _do_send(self, request: HttpRequest) -> None:
        self.query_one("#response", TextArea).text = "⏳ Envoi en cours…"
        result = await send_request(request, client=self.app.client)
        self.query_one("#response", TextArea).text = self._format_result(result)

    def action_fuzz(self) -> None:
        combos = self._prepare_combos()
        if combos is None:
            return
        if len(combos) <= 1:
            self.notify("Aucune balise {{fuzz:…}} dans la requête", severity="warning")
            return
        self.query_one("#main-tabs", TabbedContent).active = "tab-fuzz"
        self.fuzz_rows = []
        self._build_fuzz_columns()
        self._run_fuzz(combos)

    @work(exclusive=True)
    async def _run_fuzz(self, combos) -> None:
        total = len(combos)
        self.notify(f"Fuzzing : {total} requêtes…")
        # envoi concurrent borné, sur le client partagé (keep-alive)
        sem = asyncio.Semaphore(_FUZZ_CONCURRENCY)

        async def run_one(idx, labels, request):
            async with sem:
                result = await send_request(request, client=self.app.client)
            result.body = result.body[:_MAX_BODY_KEEP]
            label = ", ".join(f"{k}={v!r}" for k, v in labels.items())
            return FuzzRow(idx=idx, label=label, url=request.url, result=result)

        tasks = [
            asyncio.create_task(run_one(i, labels, request))
            for i, (labels, request) in enumerate(combos, start=1)
        ]
        try:
            # affichage live : chaque requête alimente le tableau dès qu'elle finit
            for task in asyncio.as_completed(tasks):
                row = await task
                self.fuzz_rows.append(row)
                self._append_fuzz_row(row)  # respecte le filtre, ignore le tri
        finally:
            # worker annulé (retour à la liste, nouveau fuzz) : on stoppe les envois restants
            for task in tasks:
                task.cancel()

        # les réponses arrivent dans le désordre : on remet l'ordre des requêtes
        # avant le rendu final (le tri explicite éventuel reste prioritaire)
        self.fuzz_rows.sort(key=lambda r: r.idx)
        self._render_fuzz_table()  # vue finale propre (tri éventuel réappliqué)
        self.notify("Fuzzing terminé")

    # --- tableau de fuzzing : colonnes / filtre / tri -------------------
    def _build_fuzz_columns(self) -> None:
        table = self.query_one("#fuzz-results", DataTable)
        table.clear(columns=True)
        for key, label in FUZZ_COLUMNS:
            arrow = ""
            if self._fuzz_sort and self._fuzz_sort[0] == key:
                arrow = " ▼" if self._fuzz_sort[1] else " ▲"
            table.add_column(label + arrow, key=key)

    def _row_visible(self, row: FuzzRow) -> bool:
        return self._fuzz_filter.lower() in row.body_lower

    def _filtered_sorted_rows(self) -> list[FuzzRow]:
        rows = [r for r in self.fuzz_rows if self._row_visible(r)]
        if self._fuzz_sort:
            key, reverse = self._fuzz_sort
            getters = {
                "idx": lambda r: r.idx,
                "fuzz": lambda r: r.label.lower(),
                "status": lambda r: r.result.status,
                "time": lambda r: r.result.elapsed_ms,
                "size": lambda r: r.result.size,
            }
            rows.sort(key=getters[key], reverse=reverse)
        return rows

    def _render_fuzz_table(self) -> None:
        self._build_fuzz_columns()
        table = self.query_one("#fuzz-results", DataTable)
        for r in self._filtered_sorted_rows():
            table.add_row(*r.cells(), key=str(r.idx))

    def _append_fuzz_row(self, row: FuzzRow) -> None:
        # ajout immédiat d'une ligne (respecte le filtre, ignore le tri)
        if self._row_visible(row):
            self.query_one("#fuzz-results", DataTable).add_row(
                *row.cells(), key=str(row.idx)
            )

    def _format_fuzz_detail(self, row: FuzzRow) -> str:
        meta = f"Requête #{row.idx}  —  {row.label}\n{row.url}"
        return f"{meta}\n{self._format_result(row.result)}"

    # --- rendu -----------------------------------------------------------
    def _format_result(self, result: HttpResult) -> str:
        if result.error:
            return f"✗ Erreur : {result.error}"
        head = f"{result.status} {result.reason}   {result.elapsed_ms:.0f} ms   {result.size} octets"
        body = result.body
        if len(body) > _MAX_BODY_SHOW:
            body = f"{body[:_MAX_BODY_SHOW]}\n\n… corps tronqué à l'affichage ({len(result.body)} caractères)"
        return f"{head}\n{'─' * len(head)}\n{format_headers(result.headers)}\n\n{body}"


class HttpTui(BaseApp):
    CSS_PATH = [COMMON_CSS, "app.tcss"]
    TITLE = "HTTP TUI"

    def __init__(
        self,
        requests_dir: str = "requests",
        root: str = ".",
        fuzz_dir: str | None = None,
    ):
        super().__init__()
        # un fichier .http par requête, dans ce répertoire
        self.requests_dir = Path(requests_dir)
        self.requests: list[HttpRequest] = []
        # listes de fuzzing : un fichier .txt par liste
        self.fuzz_dir = Path(fuzz_dir) if fuzz_dir else Path(root) / "fuzz"
        self.fuzzer = Fuzzer.from_directory(self.fuzz_dir)

    def on_mount(self) -> None:
        # client partagé par tous les envois (keep-alive entre deux ctrl+r)
        self.client = make_client()
        self.push_screen(RequestListScreen())

    async def on_unmount(self) -> None:
        await self.client.aclose()

    def get_system_commands(self, screen):
        yield from super().get_system_commands(screen)
        if isinstance(screen, RequestScreen):
            yield SystemCommand(
                "Requêtes", "Revenir à la liste des requêtes", screen.action_back
            )


def main() -> None:
    import os

    root = os.environ.get("HTTP_ROOT", ".")
    requests_dir = os.environ.get("HTTP_REQUESTS_DIR") or str(Path(root) / "requests")
    fuzz_dir = os.environ.get("FUZZ_DIR")
    HttpTui(requests_dir=requests_dir, root=root, fuzz_dir=fuzz_dir).run()


if __name__ == "__main__":
    main()
