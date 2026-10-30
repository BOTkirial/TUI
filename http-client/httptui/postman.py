"""Import / export Postman (collection v2.0 / v2.1, ou environnement exporté).

Seule la partie statique est reprise : méthode, URL, en-têtes, corps et
authentification simple (Bearer, Basic, clé d'API). Les scripts
(pre-request / tests), les corps fichiers et multipart sont ignorés
avec un avertissement. Les variables `{{nom}}` sont conservées telles
quelles : elles sont résolues à l'envoi par un fichier `.env.*`.

L'export produit une collection v2.1 (corps en mode raw, en-têtes tels
quels, auth comprise) ; les `.env.*` ne sont pas exportés.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from dataclasses import dataclass, field
from urllib.parse import quote

from .curlimport import has_header
from .envfile import valid_var_name
from .httpfile import METHODS, HttpRequest

# Content-Type déduit de `options.raw.language` d'un corps brut
_RAW_TYPES = {
    "json": "application/json",
    "xml": "application/xml",
    "html": "text/html",
    "javascript": "application/javascript",
    "text": "text/plain",
}


SCHEMA_V21 = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"


@dataclass
class PostmanImport:
    """Résultat d'un import : requêtes (dossiers relatifs à la cible) + variables."""

    name: str
    requests: list[HttpRequest] = field(default_factory=list)
    # variables de collection, ou valeurs d'un environnement exporté
    variables: dict[str, str] = field(default_factory=dict)
    is_environment: bool = False
    warnings: list[str] = field(default_factory=list)


def safe_name(raw: str, default: str = "sans nom") -> str:
    """Nom utilisable comme fichier/dossier : pas de `/`, pas de `.` en tête."""
    name = re.sub(r"[/\\\x00-\x1f]", "_", str(raw or "")).strip().lstrip(".").strip()
    return name or default


def _enabled(entries) -> list[dict]:
    """Entrées `{key, value, disabled}` actives (les autres formes sont ignorées)."""
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and not e.get("disabled") and e.get("key")]


def _variables(entries, warnings: list[str]) -> dict[str, str]:
    # `enabled: false` (environnements) ou `disabled: true` (collections)
    variables = {
        str(e["key"]): str(e.get("value") if e.get("value") is not None else "")
        for e in _enabled(entries)
        if e.get("enabled", True) is not False
    }
    # un nom comme `base url` ne serait jamais relu depuis le .env ni substitué
    invalid = [k for k in variables if not valid_var_name(k)]
    if invalid:
        warnings.append(f"variable(s) au nom inutilisable ignorée(s) : {', '.join(invalid)}")
    return {k: v for k, v in variables.items() if k not in invalid}


def _auth_params(auth: dict, kind: str) -> dict[str, str]:
    """Paramètres d'une auth : liste `[{key, value}]` (v2.1) ou objet (v2.0)."""
    params = auth.get(kind)
    if isinstance(params, list):
        return {str(p.get("key")): str(p.get("value", "")) for p in params if isinstance(p, dict)}
    if isinstance(params, dict):
        return {str(k): str(v) for k, v in params.items()}
    return {}


def _url_text(url) -> str:
    if isinstance(url, str):
        return url
    if not isinstance(url, dict):
        return ""
    raw = url.get("raw")
    if not raw:
        # pas de `raw` : URL reconstituée depuis ses morceaux
        host = url.get("host", "")
        host = ".".join(host) if isinstance(host, list) else str(host)
        path = url.get("path", "")
        path = "/".join(str(p) for p in path) if isinstance(path, list) else str(path)
        raw = f"{url['protocol']}://" if url.get("protocol") else ""
        raw += host + (f":{url['port']}" if url.get("port") else "")
        raw += f"/{path}" if path else ""
        query = "&".join(f"{q['key']}={q.get('value') or ''}" for q in _enabled(url.get("query")))
        raw += f"?{query}" if query else ""
    # variables de chemin `:id` dont la valeur est renseignée
    for var in _enabled(url.get("variable")):
        if var.get("value") not in (None, ""):
            # fonction de remplacement : la valeur n'est pas un modèle (`\1`, `C:\x`…)
            value = str(var["value"])
            raw = re.sub(rf":{re.escape(str(var['key']))}(?=/|\?|#|$)", lambda _m: value, raw)
    return raw


def _set_default(headers: dict[str, str], name: str, value: str) -> None:
    """Ajoute l'en-tête sauf s'il existe déjà (quelle que soit la casse)."""
    if not has_header(headers, name):
        headers[name] = value


def _apply_auth(
    auth: dict | None, headers: dict[str, str], url: str, where: str, warnings: list[str]
) -> str:
    """Ajoute l'auth aux en-têtes (ou à l'URL pour une clé d'API en query) ; renvoie l'URL."""
    if not auth:
        return url
    kind = auth.get("type", "noauth")
    params = _auth_params(auth, kind)
    if kind == "noauth":
        pass
    elif kind == "bearer":
        _set_default(headers, "Authorization", f"Bearer {params.get('token', '')}")
    elif kind == "basic":
        user_pass = f"{params.get('username', '')}:{params.get('password', '')}"
        if "{{" in user_pass:
            # une variable ne peut pas être résolue avant l'encodage base64
            warnings.append(f"{where} : auth Basic avec variables, à encoder à la main")
        token = base64.b64encode(user_pass.encode()).decode()
        _set_default(headers, "Authorization", f"Basic {token}")
    elif kind == "apikey":
        key, value = params.get("key", ""), params.get("value", "")
        if params.get("in") == "query":
            url += ("&" if "?" in url else "?") + f"{key}={value}"
        elif key:
            _set_default(headers, key, value)
    else:
        warnings.append(f"{where} : authentification « {kind} » non reprise")
    return url


def _body(body, headers: dict[str, str], where: str, warnings: list[str]) -> str:
    if not isinstance(body, dict) or body.get("disabled"):
        return ""
    mode = body.get("mode")

    def content_type(value: str) -> None:
        _set_default(headers, "Content-Type", value)

    if mode == "raw":
        text = str(body.get("raw") or "")
        language = (body.get("options") or {}).get("raw", {}).get("language")
        if text and language in _RAW_TYPES:
            content_type(_RAW_TYPES[language])
        return text
    if mode == "urlencoded":
        # accolades préservées : les {{variables}} doivent rester substituables
        pairs = [
            f"{quote(str(p['key']), safe='{}')}={quote(str(p.get('value') or ''), safe='{}')}"
            for p in _enabled(body.get("urlencoded"))
        ]
        if pairs:
            content_type("application/x-www-form-urlencoded")
        return "&".join(pairs)
    if mode == "graphql":
        graphql = body.get("graphql") or {}
        variables = graphql.get("variables") or None
        if isinstance(variables, str):
            try:
                variables = json.loads(variables) if variables.strip() else None
            except ValueError:
                warnings.append(f"{where} : variables GraphQL illisibles, ignorées")
                variables = None
        content_type("application/json")
        payload = {"query": graphql.get("query", "")}
        if variables:
            payload["variables"] = variables
        return json.dumps(payload, indent=2, ensure_ascii=False)
    if mode in ("formdata", "file"):
        warnings.append(f"{where} : corps « {mode} » non repris")
    return ""


def _convert_request(
    item: dict, folder: str, auth: dict | None, warnings: list[str]
) -> HttpRequest:
    name = safe_name(item.get("name"), "requête")
    where = f"{folder}/{name}" if folder else name
    raw = item.get("request")
    if isinstance(raw, str):  # forme courte : URL seule
        raw = {"url": raw}
    method = str(raw.get("method") or "GET").upper()
    if method not in METHODS:
        warnings.append(f"{where} : méthode {method} remplacée par GET")
        method = "GET"
    headers: dict[str, str] = {}
    raw_headers = raw.get("header")
    if isinstance(raw_headers, str):
        raw_headers = [
            {"key": k.strip(), "value": v.strip()}
            for k, _, v in (line.partition(":") for line in raw_headers.splitlines())
        ]
    for h in _enabled(raw_headers):
        headers[str(h["key"])] = str(h.get("value") or "")
    body = _body(raw.get("body"), headers, where, warnings)
    # auth de la requête, sinon héritée du dossier/de la collection
    own = raw.get("auth")
    if own and own.get("type") != "inherit":
        auth = own
    url = _apply_auth(auth, headers, _url_text(raw.get("url")), where, warnings)
    return HttpRequest(
        name=name, method=method, url=url, headers=headers, body=body, folder=folder
    )


def _has_scripts(node: dict) -> bool:
    return any(
        isinstance(e, dict) and (e.get("script") or {}).get("exec")
        for e in node.get("event") or []
    )


def parse_postman(data) -> PostmanImport:
    """Convertit le JSON d'un export Postman. ValueError si le format est inconnu."""
    if not isinstance(data, dict):
        raise ValueError("JSON Postman attendu (objet)")

    # environnement exporté : {name, values: [{key, value, enabled}]}
    if "values" in data and "item" not in data:
        result = PostmanImport(name=safe_name(data.get("name"), "postman"), is_environment=True)
        result.variables = _variables(data.get("values"), result.warnings)
        return result
    if "item" not in data:
        if "requests" in data:
            raise ValueError("collection Postman v1 non gérée : réexporter en v2.1")
        raise ValueError("ni collection ni environnement Postman")

    info = data.get("info") or {}
    result = PostmanImport(name=safe_name(info.get("name"), "postman"))
    result.variables = _variables(data.get("variable"), result.warnings)
    scripts = int(_has_scripts(data))

    def walk(items, folder: str, auth: dict | None) -> None:
        nonlocal scripts
        for item in items or []:
            if not isinstance(item, dict):
                continue
            scripts += _has_scripts(item)
            if "item" in item:  # dossier
                sub = safe_name(item.get("name"), "dossier")
                own = item.get("auth")
                walk(
                    item["item"],
                    f"{folder}/{sub}" if folder else sub,
                    own if own and own.get("type") != "inherit" else auth,
                )
            elif item.get("request"):
                result.requests.append(
                    _convert_request(item, folder, auth, result.warnings)
                )

    walk(data["item"], "", data.get("auth"))
    if scripts:
        result.warnings.append(f"{scripts} script(s) Postman ignoré(s)")
    return result


def _export_request(req: HttpRequest) -> dict:
    request: dict = {
        "method": req.method,
        "header": [{"key": k, "value": v, "type": "text"} for k, v in req.headers.items()],
        "url": req.url,
    }
    if req.body.strip():
        content_type = next(
            (v.lower() for k, v in req.headers.items() if k.lower() == "content-type"), ""
        )
        # langage d'affichage Postman déduit du Content-Type (text par défaut)
        language = next((lang for lang in _RAW_TYPES if lang in content_type), "text")
        request["body"] = {
            "mode": "raw", "raw": req.body, "options": {"raw": {"language": language}},
        }
    return {"name": req.name, "request": request}


def to_postman(
    name: str, requests: list[HttpRequest], folders: list[str] = (), base: str = ""
) -> dict:
    """Collection v2.1 des requêtes de `base` et de ses sous-dossiers (récursif).

    Les dossiers sont relatifs à `base`, qui devient la racine de la collection ;
    `folders` (sous-dossiers existants) permet d'exporter aussi les dossiers vides.
    """
    def inside(folder: str) -> bool:
        return not base or folder == base or folder.startswith(f"{base}/")

    def relative(folder: str) -> str:
        return folder[len(base):].lstrip("/") if base else folder

    root: list[dict] = []
    items: dict[str, list[dict]] = {"": root}

    def folder_items(folder: str) -> list[dict]:
        if folder not in items:
            parent, _, leaf = folder.rpartition("/")
            node = {"name": leaf, "item": []}
            folder_items(parent).append(node)
            items[folder] = node["item"]
        return items[folder]

    # dossiers créés d'abord : ils précèdent les requêtes, comme dans l'arbre
    for folder in sorted(folders):
        if inside(folder) and relative(folder):
            folder_items(relative(folder))
    for req in requests:
        if inside(req.folder):
            folder_items(relative(req.folder)).append(_export_request(req))
    return {
        "info": {"_postman_id": str(uuid.uuid4()), "name": name, "schema": SCHEMA_V21},
        "item": root,
    }
