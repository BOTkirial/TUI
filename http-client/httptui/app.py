"""TUI client HTTP : éditeur de requêtes, import curl, fuzzing."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
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
    OptionList,
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
from .envfile import (
    ENV_PREFIX,
    EnvFile,
    env_path,
    list_folder_envs,
    load_envs_dir,
    read_env,
    serialize_env,
    substitute,
    used_variables,
    valid_env_name,
    write_env_text,
)
from .httpfile import (
    METHODS,
    HttpRequest,
    delete_request,
    format_headers,
    invalid_header_lines,
    list_request_folders,
    load_requests_dir,
    parse_headers,
    save_request,
    split_request_path,
)
from .postman import parse_postman, to_postman

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

    def cells(self) -> tuple[Text, ...]:
        """Cellules affichées, dans l'ordre de `FUZZ_COLUMNS`.

        En `Text` : DataTable lirait une `str` comme du markup, et les valeurs
        de fuzz (`[/]`, `user[$ne]`…) feraient planter l'app.
        """
        r = self.result
        status = r.reason if r.error else str(r.status)
        cells = (str(self.idx), self.label, status, f"{r.elapsed_ms:.0f}", str(r.size))
        return tuple(Text(c) for c in cells)


def _request_path(req: HttpRequest) -> str:
    """Chemin `dossier/nom` d'une requête : texte sur lequel porte le filtre."""
    return f"{req.folder}/{req.name}" if req.folder else req.name


def _env_display(env: EnvFile) -> str:
    """Chemin affiché `dossier/.env.nom` d'un environnement."""
    name = f"{ENV_PREFIX}{env.name}"
    return f"{env.folder}/{name}" if env.folder else name


def _leaf_path(item: HttpRequest | EnvFile) -> str:
    return _env_display(item) if isinstance(item, EnvFile) else _request_path(item)


def _leaf_key(item) -> tuple[bool, str, str] | None:
    """Identifie une feuille de l'arbre entre deux relectures du disque."""
    if isinstance(item, (HttpRequest, EnvFile)):
        return isinstance(item, EnvFile), item.folder, item.name
    return None


def _request_label(req: HttpRequest) -> Text:
    # Text plutôt que str : un nom contenant `[` ne doit pas être lu comme du markup
    return Text(f"{req.method}  {req.name}")


def _env_label(env: EnvFile) -> Text:
    return Text.assemble(("ENV", "italic"), f"  {ENV_PREFIX}{env.name}")


class PathPrompt(ModalScreen[tuple[str, str] | None]):
    """Petite fenêtre pour saisir une valeur (chemin, nom…).

    Résultat : (id du bouton : « ok » ou celui d'un bouton secondaire, saisie),
    ou None si annulé.
    """

    BINDINGS = [Binding("escape", "cancel", "Annuler")]

    def __init__(
        self,
        label: str,
        placeholder: str = "",
        ok_label: str = "OK",
        value: str = "",
        extra_buttons: list[tuple[str, str]] = (),
    ):
        super().__init__()
        self._label = label
        self._placeholder = placeholder
        self._ok_label = ok_label
        self._value = value
        # boutons secondaires (id, libellé) : renvoient (id, saisie), même vide
        self._extra_buttons = list(extra_buttons)

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            # Text : le libellé contient des noms de fichiers (un `[` serait du markup)
            yield Label(Text(self._label), classes="section")
            yield Input(self._value, placeholder=self._placeholder, id="path")
            with Horizontal(id="dialog-buttons"):
                yield Button(self._ok_label, variant="primary", id="ok")
                for button_id, label in self._extra_buttons:
                    yield Button(label, id=button_id)
                yield Button("Annuler", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#path", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        value = self.query_one("#path", Input).value
        extra = {button_id for button_id, _ in self._extra_buttons}
        if event.button.id in extra:
            self.dismiss((event.button.id, value))
        elif event.button.id == "ok" and value:
            self.dismiss(("ok", value))
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(("ok", event.value) if event.value else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class RequestTree(Tree):
    """Arbre des requêtes et environnements : un clic sur une feuille la
    sélectionne sans l'ouvrir.

    L'ouverture passe par Entrée (`select_cursor` → `NodeSelected`) ;
    un clic sur un dossier le plie/déplie toujours.
    """

    async def _on_click(self, event: events.Click) -> None:
        line = event.style.meta.get("line")
        node = self.get_node_at_line(line) if line is not None else None
        if node is not None and isinstance(node.data, (HttpRequest, EnvFile)):
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


class PostmanPrompt(ModalScreen[tuple[str, str] | None]):
    """Chemin d'un export Postman (collection ou environnement) + dossier cible."""

    BINDINGS = [Binding("escape", "cancel", "Annuler")]

    def __init__(self, target: str = ""):
        super().__init__()
        self._target = target

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide"):
            yield Label(
                Text(f"Fichier JSON exporté de Postman (collection v2.x ou environnement, relatif à {self.app.root})"),
                classes="section",
            )
            yield Input(placeholder="ma-collection.postman_collection.json", id="source")
            yield Label("Importer dans le dossier (vide = racine)", classes="section")
            yield Input(self._target, placeholder="dossier/", id="target")
            with Horizontal(id="dialog-buttons"):
                yield Button("Importer", variant="primary", id="ok")
                yield Button("Annuler", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#source", Input).focus()

    def _submit(self) -> None:
        source = self.query_one("#source", Input).value.strip()
        target = self.query_one("#target", Input).value
        self.dismiss((source, target) if source else None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._submit()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)


# choix « sans environnement » de EnvPrompt (un nom d'env ne peut pas être vide)
_NO_ENV = ""


class EnvPrompt(ModalScreen[str | None]):
    """Choix de l'environnement d'envoi. Résultat : nom, `_NO_ENV`, ou None si annulé."""

    BINDINGS = [Binding("escape", "cancel", "Annuler")]

    def __init__(self, folder: str, envs: list[str], default: str | None):
        super().__init__()
        self._folder = folder
        self._choices = [*envs, _NO_ENV]
        self._default = default

    def compose(self) -> ComposeResult:
        where = f"« {self._folder}/ »" if self._folder else "la racine"
        with Vertical(id="dialog"):
            yield Label(Text(f"Environnement d'envoi ({where})"), classes="section")
            yield OptionList(
                *(Text(f"{ENV_PREFIX}{e}") for e in self._choices[:-1]),
                Text("(aucun environnement)", style="italic"),
                id="env-choice", classes="framed",
            )
            with Horizontal(id="dialog-buttons"):
                yield Button("Envoyer", variant="primary", id="ok")
                yield Button("Annuler", id="cancel")

    def on_mount(self) -> None:
        options = self.query_one("#env-choice", OptionList)
        # pré-sélection : dernier environnement utilisé dans ce dossier
        if self._default in self._choices:
            options.highlighted = self._choices.index(self._default)
        else:
            options.highlighted = 0
        options.focus()

    def _submit(self) -> None:
        index = self.query_one("#env-choice", OptionList).highlighted
        self.dismiss(self._choices[index] if index is not None else None)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._submit()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class EnvScreen(Screen[EnvFile]):
    """Édition brute d'un fichier `.env.*` (format `NOM=valeur`)."""

    BINDINGS = [
        Binding("ctrl+s", "save", "Enregistrer"),
        Binding("escape", "back", "Requêtes"),
    ]

    def __init__(self, env: EnvFile):
        super().__init__()
        self._env = env
        self._saved = ""
        self._discard_warned = False

    def compose(self) -> ComposeResult:
        with Vertical(id="editor"):
            yield Label(Text(_env_display(self._env)), classes="title")
            yield Label(
                "Une variable par ligne : NOM=valeur (# = commentaire) — "
                "remplace {{NOM}} dans les requêtes de ce dossier",
                classes="hint",
            )
            yield TextArea(id="env-text", classes="editor-area framed")
        yield Footer()

    def on_mount(self) -> None:
        try:
            self._saved = env_path(self.app.requests_dir, self._env).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError as exc:
            self.notify(f"Lecture impossible : {exc}", severity="error")
        area = self.query_one("#env-text", TextArea)
        area.text = self._saved
        area.focus()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self._discard_warned = False

    def action_save(self) -> None:
        text = self.query_one("#env-text", TextArea).text
        path = env_path(self.app.requests_dir, self._env)
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            self.notify(f"Enregistrement impossible : {exc}", severity="error")
            return
        self._saved = text
        self.notify(f"Enregistré dans {path}")

    def action_back(self) -> None:
        text = self.query_one("#env-text", TextArea).text
        if text != self._saved and not self._discard_warned:
            self._discard_warned = True
            self.notify(
                "Modifications non enregistrées : ctrl+s pour enregistrer, Échap à nouveau pour les abandonner",
                severity="warning",
            )
            return
        self.dismiss(self._env)


class RequestListScreen(Screen):
    """Écran d'accueil plein écran : choix (ou création) de la requête à ouvrir."""

    BINDINGS = [
        Binding("ctrl+n", "new", "Nouvelle"),
        # aussi proposés par la fenêtre « Nouvelle » : masqués du pied de page
        Binding("ctrl+o", "import_curl", "Import curl", show=False),
        Binding("ctrl+t", "import_postman", "Import Postman", show=False),
        # priority : sinon ctrl+e (fin de ligne) est pris par le champ filtre
        Binding("ctrl+e", "export_postman", "Export Postman", priority=True),
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
                "Tape pour filtrer · ↑↓ pour choisir · Entrée pour ouvrir (requête ou .env.*) · Suppr pour supprimer",
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
                yield Button("Actualiser", id="reload")
                yield Button("Supprimer", id="delete", variant="error")
        yield Footer()

    def on_mount(self) -> None:
        self._load_requests()
        self.query_one("#request-filter", Input).focus()

    def _load_requests(
        self,
        select: tuple[bool, str, str] | None = None,
        expanded: set[str] = frozenset(),
    ) -> None:
        """(Re)lit le disque et reconstruit l'arbre.

        `select` = `_leaf_key` de la feuille sous le curseur (sinon la 1re requête) ;
        `expanded` = dossiers à garder dépliés.
        """
        self.app.requests = load_requests_dir(self.app.requests_dir)
        self.app.envs = load_envs_dir(self.app.requests_dir)
        target = next(
            (i for i in [*self.app.envs, *self.app.requests] if _leaf_key(i) == select),
            None,
        )
        self._show(target, expanded)

    def _show(self, select: HttpRequest | EnvFile | None, expanded: set[str]) -> None:
        """Reconstruit l'arbre depuis la mémoire (dossiers relus sur disque).

        Contrairement à `_load_requests`, garde les requêtes créées mais pas
        encore enregistrées.
        """
        self._folders = list_request_folders(self.app.requests_dir)
        self._expanded = set(expanded)
        self._build_tree()
        self._focus_leaf(select)

    # --- arbre des requêtes ---------------------------------------------
    def _filter_terms(self) -> list[str]:
        return self.query_one("#request-filter", Input).value.lower().split()

    def _build_tree(self) -> None:
        """(Re)construit l'arbre depuis `app.envs` + `app.requests`, restreint par le filtre.

        Dans chaque dossier : sous-dossiers, puis environnements, puis requêtes.
        """
        tree = self.query_one("#requests", Tree)
        tree.clear()
        tree.show_root = False
        tree.root.expand()
        self._folder_nodes = {"": tree.root}
        terms = self._filter_terms()
        if terms:
            # seuls les dossiers menant à un résultat, tous dépliés ;
            # créés avant les requêtes pour garder l'ordre dossiers d'abord
            found = [
                i for i in [*self.app.envs, *self.app.requests]
                if matches(_leaf_path(i), terms)
            ]
            needed = {i.folder for i in found}
            for folder in self._folders:
                if folder in needed:
                    self._folder_node(folder)
            for item in found:
                self._add_leaf_node(item)
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
        for item in [*self.app.envs, *self.app.requests]:
            self._add_leaf_node(item)

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
        keep = under_cursor if not filtering and _leaf_key(under_cursor) else None
        self._focus_leaf(keep)

    def _iter_nodes(self):
        """Nœuds de l'arbre, dans l'ordre d'affichage."""
        pending = [self.query_one("#requests", Tree).root]
        while pending:
            node = pending.pop()
            yield node
            pending.extend(reversed(node.children))

    def _node_for(self, item: HttpRequest | EnvFile) -> TreeNode | None:
        """Nœud affichant `item` (par identité), None s'il est masqué par le filtre."""
        return next((n for n in self._iter_nodes() if n.data is item), None)

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

    def _add_leaf_node(self, item: HttpRequest | EnvFile) -> TreeNode:
        label = _env_label(item) if isinstance(item, EnvFile) else _request_label(item)
        return self._folder_node(item.folder).add_leaf(label, data=item)

    def _focus_leaf(self, item: HttpRequest | EnvFile | None) -> None:
        """Curseur sur `item` s'il est affiché, sinon sur la 1re feuille visible.

        Recherche par identité puis par `_leaf_key` : après une relecture du
        disque, l'objet est une nouvelle instance.
        """
        node = self._node_for(item) if item is not None else None
        if node is None and item is not None:
            key = _leaf_key(item)
            node = next((n for n in self._iter_nodes() if _leaf_key(n.data) == key), None)
        if node is None:
            node = next((n for n in self._iter_nodes() if _leaf_key(n.data)), None)
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
        if isinstance(node.data, (HttpRequest, EnvFile)):
            return node.data.folder
        return node.data or ""

    # --- ouverture / retour ----------------------------------------------
    def _open_request(self, req: HttpRequest) -> None:
        self.app.push_screen(RequestScreen(req), self._request_closed)

    def _request_closed(self, req: HttpRequest | None) -> None:
        """Retour de l'éditeur : `req` = dernière version (sauvée ou non) de la requête."""
        # reconstruit : le libellé (méthode) a pu changer à la sauvegarde
        self._build_tree()
        self._focus_leaf(req)
        self.query_one("#request-filter", Input).focus()

    def _add_and_open(self, req: HttpRequest) -> None:
        """Nouvelle requête : en mémoire (même hors filtre), puis ouverte."""
        self.app.requests.append(req)
        self._add_leaf_node(req)
        self._open_request(req)

    def _open_env(self, env: EnvFile) -> None:
        self.app.push_screen(EnvScreen(env), self._env_closed)

    def _env_closed(self, env: EnvFile | None) -> None:
        self._focus_leaf(env)
        self.query_one("#request-filter", Input).focus()

    # --- évènements ------------------------------------------------------
    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        # un dossier se déplie tout seul ; requête / environnement : éditeur
        if isinstance(event.node.data, HttpRequest):
            self._open_request(event.node.data)
        elif isinstance(event.node.data, EnvFile):
            self._open_env(event.node.data)

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
        # pas de `.` en tête : fichier caché, ou pris pour un environnement `.env.*`
        base = raw.strip().replace("/", "_").replace("\\", "_").lstrip(".") or "requête"
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
            # imports ouverts ici (et non depuis la fenêtre, déjà fermée, qui
            # emporterait le callback de résultat de la suivante avec elle)
            if button == "curl":
                self.action_import_curl(raw)
                return
            if button == "postman":
                # seul le dossier sert de cible (le nom vient de la collection)
                folder = raw.strip().rpartition("/")[0]
                self.action_import_postman(f"{folder}/" if folder else "")
                return
            folder, _, leaf = raw.strip().rpartition("/")
            if leaf.startswith(ENV_PREFIX):
                self._create_env(folder, leaf[len(ENV_PREFIX):])
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
                "Nom de la nouvelle requête (dossier/nom) ou environnement (dossier/.env.nom)",
                placeholder="dossier/ma-requête", ok_label="Créer",
                value=f"{folder}/" if folder else "",
                extra_buttons=[("curl", "Import curl"), ("postman", "Import Postman")],
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

    def _create_env(self, folder: str, name: str) -> None:
        """Crée le fichier `.env.<name>` (vide) dans `folder`, puis l'ouvre."""
        # dossier normalisé : `/etc` ou ` api ` ne doivent pas sortir du répertoire
        parts = self._valid_parts(folder) if folder.strip("/") else []
        if parts is None:
            return
        folder = "/".join(parts)
        if not valid_env_name(name):
            self.notify("Nom d'environnement invalide", severity="error")
            return
        env = EnvFile(name, folder)
        path = env_path(self.app.requests_dir, env)
        if path.exists():
            self.notify(f"« {_env_display(env)} » existe déjà", severity="error")
            return
        try:
            write_env_text(path, "")
        except OSError as exc:
            self.notify(f"Création impossible : {exc}", severity="error")
            return
        self.app.envs.append(env)
        self._show(env, self._expanded_folders())
        self._open_env(env)

    def _write_env(self, env: EnvFile, variables: dict[str, str]) -> bool:
        """Écrit un environnement importé ; un fichier existant n'est pas écrasé."""
        path = env_path(self.app.requests_dir, env)
        if path.exists():
            self.notify(f"« {_env_display(env)} » existe déjà : non écrasé", severity="warning")
            return False
        write_env_text(path, serialize_env(variables))
        self.app.envs.append(env)
        return True

    def action_import_postman(self, target: str | None = None) -> None:
        """Import d'une collection ou d'un environnement Postman exporté (JSON)."""
        def done(result: tuple[str, str] | None) -> None:
            if not result:
                return
            source, target = result
            parts = self._valid_parts(target) if target.strip().strip("/") else []
            if parts is None:
                return
            base = "/".join(parts)
            # chemin relatif : par rapport à HTTP_ROOT (/work/http dans Docker)
            path = self.app.root / Path(source).expanduser()
            try:
                imported = parse_postman(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                self.notify(f"Import Postman impossible : {exc}", severity="error")
                return
            try:
                self._save_postman(imported, base)
            except OSError as exc:
                self.notify(f"Enregistrement impossible : {exc}", severity="error")
                # import partiel : l'arbre montre ce qui a déjà été écrit
                self._show(None, self._expanded_folders())
            for warning in imported.warnings:
                self.notify(warning, severity="warning")

        if target is None:
            folder = self._selected_folder()
            target = f"{folder}/" if folder else ""
        self.app.push_screen(PostmanPrompt(target), done)

    def action_export_postman(self, everything: bool = False) -> None:
        """Exporte en collection v2.1 la requête ou le dossier (récursif) sous le
        curseur, ou toutes les requêtes si `everything`."""
        node = self._cursor_node()
        item = node.data if node is not None else None
        if everything:
            name, requests, folders, base = (
                self.app.requests_dir.resolve().name or "requêtes",
                self.app.requests, self._folders, "",
            )
            item = ""
        elif isinstance(item, HttpRequest):
            name, requests, folders, base = item.name, [item], [], item.folder
        elif isinstance(item, str) and item:  # nœud de dossier
            name, requests, folders, base = (
                item.rpartition("/")[2], self.app.requests, self._folders, item,
            )
        else:
            self.notify("Sélectionne une requête ou un dossier à exporter", severity="warning")
            return
        collection = to_postman(name, requests, folders, base)

        def write(path: Path) -> None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(collection, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                self.notify(f"Export impossible : {exc}", severity="error")
                return
            self.notify(f"Collection « {name} » exportée dans {path}")

        def chosen(result: tuple[str, str] | None) -> None:
            if not result:
                return
            path = self.app.root / Path(result[1].strip()).expanduser()
            if not path.exists():
                write(path)
                return
            self.app.push_screen(
                ConfirmPrompt(f"« {path} » existe déjà. L'écraser ?", "Écraser"),
                lambda ok: write(path) if ok else None,
            )

        if everything:
            what = "toutes les requêtes"
        elif isinstance(item, HttpRequest):
            what = f"la requête « {_leaf_path(item)} »"
        else:
            what = f"le dossier « {item} » (récursif)"
        self.app.push_screen(
            PathPrompt(
                f"Exporter {what} vers (fichier JSON, relatif à {self.app.root})",
                ok_label="Exporter",
                value=f"{name}.postman_collection.json",
            ),
            chosen,
        )

    def _save_postman(self, imported, base: str) -> None:
        """Écrit les requêtes / variables importées sous le dossier `base`."""
        if imported.is_environment:
            env = EnvFile(imported.name, base)
            if self._write_env(env, imported.variables):
                self.notify(f"Environnement importé : {_env_display(env)}")
            self._show(env, self._expanded_folders())
            return
        saved: list[HttpRequest] = []
        for req in imported.requests:
            req.folder = "/".join(p for p in (base, req.folder) if p)
            # jamais d'écrasement : nom rendu unique dans son dossier
            req.name = self._unique_name(req.name, req.folder)
            save_request(self.app.requests_dir, req)
            # ajoutée tout de suite : `_unique_name` doit la voir pour la suivante
            self.app.requests.append(req)
            saved.append(req)
        # variables de collection : un .env par dossier importé (portée = dossier seul)
        if imported.variables:
            for folder in sorted({r.folder for r in saved}):
                self._write_env(EnvFile(imported.name, folder), imported.variables)
        folders = {r.folder for r in saved}
        # dépliés : les dossiers importés et tous leurs parents
        expanded = {"/".join(f.split("/")[:i]) for f in folders for i in range(1, f.count("/") + 2)}
        self._show(saved[0] if saved else None, self._expanded_folders() | expanded)
        self.notify(f"{len(saved)} requête(s) importée(s) depuis « {imported.name} »")

    def action_delete(self) -> None:
        """Supprime (après confirmation) la requête ou l'environnement sous le curseur."""
        node = self._cursor_node()
        item = node.data if node is not None else None
        if not _leaf_key(item):
            self.notify("Sélectionne une requête ou un environnement à supprimer", severity="warning")
            return
        is_env = isinstance(item, EnvFile)
        noun = "l'environnement" if is_env else "la requête"

        def done(confirmed: bool | None) -> None:
            if not confirmed:
                return
            try:
                if is_env:
                    env_path(self.app.requests_dir, item).unlink(missing_ok=True)
                else:
                    delete_request(self.app.requests_dir, item)
            except OSError as exc:
                self.notify(f"Suppression impossible : {exc}", severity="error")
                return
            # curseur ensuite sur la feuille suivante affichée (sinon la précédente)
            shown = [n.data for n in self._iter_nodes() if _leaf_key(n.data)]
            i = next(k for k, r in enumerate(shown) if r is item)
            if i + 1 < len(shown):
                neighbour = shown[i + 1]
            elif i > 0:
                neighbour = shown[i - 1]
            else:
                neighbour = None
            self.app.requests = [r for r in self.app.requests if r is not item]
            self.app.envs = [e for e in self.app.envs if e is not item]
            node.remove()
            self._focus_leaf(neighbour)
            done_msg = "Environnement « {} » supprimé" if is_env else "Requête « {} » supprimée"
            self.notify(done_msg.format(_leaf_path(item)))

        self.app.push_screen(
            ConfirmPrompt(f"Supprimer {noun} « {_leaf_path(item)} » ?", "Supprimer"),
            done,
        )

    def action_clear_filter(self) -> None:
        self.query_one("#request-filter", Input).value = ""

    def action_reload(self) -> None:
        """Relit les `.http` (modifiés par un éditeur externe) sans perdre la vue."""
        node = self._cursor_node()
        current = node.data if node is not None else None
        self._load_requests(select=_leaf_key(current), expanded=self._expanded_folders())
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
        # listes proposées par le sélecteur de balise (pour détecter un changement)
        self._fuzz_names = self.app.fuzzer.available()

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
                    [(n, n) for n in self._fuzz_names],
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

    def _warn_headers(self) -> None:
        """Signale les lignes d'en-tête ignorées (sans `:`) ou en double."""
        lines = self.query_one("#headers", TextArea).text.splitlines()
        invalid = invalid_header_lines(lines)
        if invalid:
            self.notify(
                f"En-tête(s) sans « : » ignoré(s) : {', '.join(invalid)}", severity="warning"
            )
        names = [line.partition(":")[0].strip().lower() for line in lines if ":" in line]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            self.notify(
                f"En-tête(s) en double, seule la dernière valeur est gardée : {', '.join(duplicates)}",
                severity="warning",
            )

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
        response = self.query_one("#response", TextArea)
        response.text = self._format_fuzz_detail(row)
        self.query_one("#main-tabs", TabbedContent).active = "tab-response"
        # le focus doit quitter le tableau : resté dans l'onglet Fuzzing (Entrée),
        # il y fait revenir TabbedContent (TabPane.Focused)
        response.focus()

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
        self._warn_headers()
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
        """Met à jour le choix de balise (fichiers de listes ajoutés/retirés à chaud)."""
        names = self.app.fuzzer.available()
        # set_options réinitialise la sélection : seulement si les listes changent
        if names != self._fuzz_names:
            self._fuzz_names = names
            self.query_one("#fuzzlist", Select).set_options([(n, n) for n in names])

    def _check_fuzz_lists(self, request: HttpRequest) -> bool:
        """Faux (avec alerte) si une balise vise une liste absente ou vide."""
        missing = self.app.fuzzer.missing_lists(request)
        if missing:
            self.notify(
                f"Liste(s) introuvable(s) ou vide(s) dans {self.app.fuzz_dir} : {', '.join(missing)}",
                severity="error",
            )
        return not missing

    def _with_env(self, then: Callable[[HttpRequest], None]) -> None:
        """Demande l'environnement (si le dossier en a) puis appelle `then`
        avec la requête de l'éditeur, variables `{{NOM}}` substituées."""
        request = self._current_request()
        self._warn_headers()
        folder = request.folder
        envs = list_folder_envs(self.app.requests_dir, folder)

        def chosen(name: str | None) -> None:
            if name is None:
                return  # annulé
            self.app.last_env[folder] = name
            variables: dict[str, str] = {}
            if name != _NO_ENV:
                try:
                    variables = read_env(self.app.requests_dir, EnvFile(name, folder))
                except OSError as exc:
                    self.notify(f"Environnement illisible : {exc}", severity="error")
                    return
            concrete = substitute(request, variables)
            missing = used_variables(concrete)
            if missing:
                self.notify(
                    f"Variable(s) non définie(s), envoyées telles quelles : {', '.join(missing)}",
                    severity="warning",
                )
            then(concrete)

        if not envs:
            chosen(_NO_ENV)
            return
        self.app.push_screen(
            EnvPrompt(folder, envs, self.app.last_env.get(folder)), chosen
        )

    def _prepare_combos(
        self, request: HttpRequest
    ) -> list[tuple[dict[str, str], HttpRequest]] | None:
        """Requête développée en combinaisons de fuzzing (None si liste absente)."""
        self._reload_fuzz_lists()
        if not self._check_fuzz_lists(request):
            return None
        return self.app.fuzzer.expand(request)

    def action_send(self) -> None:
        self._with_env(self._send)

    def _send(self, request: HttpRequest) -> None:
        combos = self._prepare_combos(request)
        if combos is None:
            return
        if len(combos) > 1:
            self.notify(f"{len(combos)} combinaisons détectées — envoi de la 1re (ctrl+f pour tout fuzzer)")
        _, concrete = combos[0]
        self.query_one("#main-tabs", TabbedContent).active = "tab-response"
        self._do_send(concrete)

    # groupes distincts : un envoi simple n'annule pas un fuzzing en cours (et inversement)
    @work(exclusive=True, group="send")
    async def _do_send(self, request: HttpRequest) -> None:
        self.query_one("#response", TextArea).text = "⏳ Envoi en cours…"
        result = await send_request(request, client=self.app.client)
        self.query_one("#response", TextArea).text = self._format_result(result)

    def action_fuzz(self) -> None:
        self._with_env(self._fuzz)

    def _fuzz(self, request: HttpRequest) -> None:
        combos = self._prepare_combos(request)
        if combos is None:
            return
        if len(combos) <= 1:
            self.notify("Aucune balise {{fuzz:…}} dans la requête", severity="warning")
            return
        total = self.app.fuzzer.count(request)
        if total > len(combos):
            self.notify(
                f"{total} combinaisons : seules les {len(combos)} premières sont envoyées",
                severity="warning",
            )
        self.query_one("#main-tabs", TabbedContent).active = "tab-fuzz"
        self.fuzz_rows = []
        self._build_fuzz_columns()
        self._run_fuzz(combos)

    def _fuzz_progress(self, text: str) -> None:
        """Avancement affiché dans le titre de l'onglet Fuzzing."""
        self.query_one("#main-tabs", TabbedContent).get_tab("tab-fuzz").label = text

    @work(exclusive=True, group="fuzz")
    async def _run_fuzz(self, combos) -> None:
        total = len(combos)
        self.notify(f"Fuzzing : {total} requêtes…")
        self._fuzz_progress(f"Fuzzing ⏳ 0/{total}")
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
                self._fuzz_progress(f"Fuzzing ⏳ {len(self.fuzz_rows)}/{total}")
        finally:
            # worker annulé (retour à la liste, nouveau fuzz) : on stoppe les envois restants
            for task in tasks:
                task.cancel()
        self._fuzz_progress(f"Fuzzing ✓ {total}")

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
        # redirections suivies : le statut affiché est celui de la dernière réponse
        redirects = "".join(f"↪ {r}\n" for r in result.redirects)
        body = result.body
        if len(body) > _MAX_BODY_SHOW:
            body = f"{body[:_MAX_BODY_SHOW]}\n\n… corps tronqué à l'affichage ({len(result.body)} caractères)"
        return f"{redirects}{head}\n{'─' * len(head)}\n{format_headers(result.headers)}\n\n{body}"


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
        self.root = Path(root)
        self.requests_dir = Path(requests_dir)
        self.requests: list[HttpRequest] = []
        # fichiers .env.* (environnements) du répertoire des requêtes
        self.envs: list[EnvFile] = []
        # dernier environnement choisi à l'envoi, par dossier (pré-sélectionné)
        self.last_env: dict[str, str] = {}
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
        if isinstance(screen, RequestListScreen):
            yield SystemCommand(
                "Export Postman (tout)",
                "Exporter toutes les requêtes en une collection Postman",
                lambda: screen.action_export_postman(everything=True),
            )
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
