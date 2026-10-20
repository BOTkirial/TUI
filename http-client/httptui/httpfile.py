"""Lecture et écriture des requêtes `.http` (format type REST Client / IntelliJ).

Un fichier = une requête, le nom de fichier sert de nom affiché :

    GET https://api.example.com/users/{{ID}}
    Authorization: Bearer {{token}}

    { "body": "éventuel" }

Les fichiers peuvent être rangés dans des sous-dossiers du répertoire des requêtes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# ordre d'affichage dans le sélecteur de méthode
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE")


@dataclass
class HttpRequest:
    name: str = "Nouvelle requête"
    method: str = "GET"
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    # sous-dossier relatif au répertoire des requêtes ("" = racine, séparateur `/`)
    folder: str = ""

    def headers_text(self) -> str:
        return format_headers(self.headers)


def format_headers(headers: dict[str, str]) -> str:
    return "\n".join(f"{k}: {v}" for k, v in headers.items())


def parse_headers(lines: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in lines:
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip()] = value.strip()
    return headers


def _parse_block(name: str, lines: list[str]) -> HttpRequest | None:
    # on saute les commentaires et lignes vides en tête
    idx = 0
    while idx < len(lines):
        stripped = lines[idx].strip()
        if stripped and not stripped.startswith(("#", "//", "@")):
            break
        idx += 1
    else:
        return None  # bloc sans ligne de requête

    # ligne "METHOD URL [HTTP/x]"
    parts = lines[idx].split()
    if not parts:
        return None
    if parts[0].upper() in METHODS:
        method = parts[0].upper()
        url = parts[1] if len(parts) > 1 else ""
    else:
        method, url = "GET", parts[0]
    idx += 1

    # en-têtes jusqu'à une ligne vide
    header_lines: list[str] = []
    while idx < len(lines) and lines[idx].strip():
        header_lines.append(lines[idx])
        idx += 1

    # corps = le reste
    body = "\n".join(lines[idx + 1:]).strip() if idx < len(lines) else ""

    return HttpRequest(
        name=name,
        method=method,
        url=url,
        headers=parse_headers(header_lines),
        body=body,
    )


def parse_request_file(path: str | Path) -> HttpRequest | None:
    """Parse un fichier contenant **une** requête ; le nom vient du nom de fichier."""
    p = Path(path)
    if not p.is_file():
        return None
    # errors="replace" : un fichier non UTF-8 s'affiche (accents abîmés) au lieu de
    # faire échouer le chargement de toutes les requêtes
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    # on tolère une éventuelle ligne de titre `###` héritée de l'ancien format
    if lines and lines[0].startswith("###"):
        lines = lines[1:]
    return _parse_block(p.stem, lines)


def _is_hidden(path: Path, base: Path) -> bool:
    return any(part.startswith(".") for part in path.relative_to(base).parts)


def load_requests_dir(directory: str | Path) -> list[HttpRequest]:
    """Charge récursivement les requêtes (un `*.http` = une requête).

    Le sous-dossier de chaque fichier est conservé dans `HttpRequest.folder` ;
    les dossiers cachés (`.git`…) sont ignorés.
    """
    base = Path(directory)
    requests: list[HttpRequest] = []
    if base.is_dir():
        for path in sorted(base.rglob("*.http")):
            if _is_hidden(path, base):
                continue
            req = parse_request_file(path)
            if req is not None:
                rel = path.parent.relative_to(base).as_posix()
                req.folder = "" if rel == "." else rel
                requests.append(req)
    return requests


def list_request_folders(directory: str | Path) -> list[str]:
    """Sous-dossiers (relatifs, triés), y compris vides, hors dossiers cachés."""
    base = Path(directory)
    if not base.is_dir():
        return []
    return sorted(
        d.relative_to(base).as_posix()
        for d in base.rglob("*")
        if d.is_dir() and not _is_hidden(d, base)
    )


def serialize_request(req: HttpRequest) -> str:
    """Sérialise une requête seule (sans séparateur `###`)."""
    lines = [f"{req.method} {req.url}"]
    for key, value in req.headers.items():
        lines.append(f"{key}: {value}")
    if req.body.strip():
        lines.append("")
        lines.append(req.body.rstrip())
    return "\n".join(lines) + "\n"


def split_request_path(raw: str) -> list[str]:
    """Segments de `dossier/…/nom` ; ValueError si un segment est invalide.

    Refuse `.`/`..` et les segments cachés : le chemin doit rester
    sous le répertoire des requêtes et être relu au prochain démarrage.
    """
    parts = [p.strip() for p in raw.strip().strip("/").split("/")]
    if any(not p or p.startswith(".") or "\\" in p for p in parts):
        raise ValueError("Nom de requête invalide")
    return parts


def request_path(directory: str | Path, name: str, folder: str = "") -> Path:
    """Chemin du fichier d'une requête (le nom affiché = nom de fichier)."""
    return Path(directory) / folder / f"{name}.http"


def save_request(directory: str | Path, req: HttpRequest) -> Path:
    """Écrit une requête dans `<directory>/<folder>/<nom>.http`."""
    path = request_path(directory, req.name, req.folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize_request(req), encoding="utf-8")
    return path


def delete_request(directory: str | Path, req: HttpRequest) -> Path:
    """Supprime le fichier d'une requête (absent = jamais sauvée : rien à faire)."""
    path = request_path(directory, req.name, req.folder)
    path.unlink(missing_ok=True)
    return path
