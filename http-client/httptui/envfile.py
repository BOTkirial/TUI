"""Environnements : fichiers `.env.<nom>` rangés avec les requêtes.

Un environnement s'applique aux requêtes de **son** dossier (pas des
sous-dossiers) ; ses variables remplacent les balises `{{NOM}}` à l'envoi.
Format dotenv simple :

    # commentaire
    BASE_URL=https://api.example.com
    export TOKEN="abc\\ndef"
    RAW='\\n reste tel quel entre guillemets simples'
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from pathlib import Path

from .httpfile import HttpRequest

ENV_PREFIX = ".env."

# `{{NOM}}` ; le `:` est exclu pour ne pas toucher aux balises `{{fuzz:liste}}`
# ni aux variables dynamiques Postman (`{{$guid}}`)
_VAR_NAME = r"[A-Za-z_][\w.\-]*"
VAR_RE = re.compile(r"\{\{\s*(" + _VAR_NAME + r")\s*\}\}")

_LINE_RE = re.compile(r"^\s*(?:export\s+)?(" + _VAR_NAME + r")\s*=\s*(.*)$")

_DQ_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
_DQ_VALUE_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')


@dataclass(eq=False)
class EnvFile:
    """Un fichier `.env.<name>` du dossier `folder` (relatif, "" = racine)."""

    name: str
    folder: str = ""


def env_path(directory: str | Path, env: EnvFile) -> Path:
    return Path(directory) / env.folder / f"{ENV_PREFIX}{env.name}"


def valid_var_name(name: str) -> bool:
    """Nom utilisable dans une balise `{{NOM}}` (et donc relu depuis un `.env`)."""
    return re.fullmatch(_VAR_NAME, name) is not None


def valid_env_name(name: str) -> bool:
    return bool(name) and not any(c in name for c in "/\\") and name.strip() == name


def load_envs_dir(directory: str | Path) -> list[EnvFile]:
    """Tous les `.env.*` du répertoire des requêtes (hors dossiers cachés)."""
    base = Path(directory)
    envs: list[EnvFile] = []
    if base.is_dir():
        for path in sorted(base.rglob(f"{ENV_PREFIX}*")):
            rel = path.parent.relative_to(base)
            if not path.is_file() or any(p.startswith(".") for p in rel.parts):
                continue
            name = path.name[len(ENV_PREFIX):]
            if valid_env_name(name):
                envs.append(EnvFile(name, "" if rel.as_posix() == "." else rel.as_posix()))
    return envs


def list_folder_envs(directory: str | Path, folder: str) -> list[str]:
    """Noms des environnements du dossier `folder` (relu sur disque, trié)."""
    path = Path(directory) / folder
    if not path.is_dir():
        return []
    return sorted(
        p.name[len(ENV_PREFIX):]
        for p in path.glob(f"{ENV_PREFIX}*")
        if p.is_file() and valid_env_name(p.name[len(ENV_PREFIX):])
    )


def _unquote(raw: str) -> str:
    """Valeur d'une ligne ; ce qui suit le guillemet fermant (` # commentaire`) est ignoré."""
    raw = raw.strip()
    if raw.startswith("'"):
        end = raw.find("'", 1)
        if end > 0:
            return raw[1:end]
    elif raw.startswith('"'):
        match = _DQ_VALUE_RE.match(raw)
        if match:
            return re.sub(
                r"\\(.)", lambda m: _DQ_ESCAPES.get(m.group(1), m.group(0)), match.group(1)
            )
    # valeur nue (ou guillemet non fermé) : ` #` introduit un commentaire
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def parse_env(text: str) -> dict[str, str]:
    variables: dict[str, str] = {}
    # split("\n") et non splitlines() : ce dernier coupe aussi sur \x85, \u2028…
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if match:
            variables[match.group(1)] = _unquote(match.group(2))
    return variables


def read_env(directory: str | Path, env: EnvFile) -> dict[str, str]:
    """Variables d'un environnement (OSError si le fichier est illisible)."""
    text = env_path(directory, env).read_text(encoding="utf-8", errors="replace")
    return parse_env(text)


def serialize_env(variables: dict[str, str]) -> str:
    """Écrit des variables au format dotenv (valeurs entre guillemets si besoin)."""
    lines = []
    for key, value in variables.items():
        if value and not re.search(r"[\s#\"'\\]", value):
            lines.append(f"{key}={value}")
        else:
            escaped = (
                value.replace("\\", "\\\\").replace('"', '\\"')
                .replace("\n", "\\n").replace("\r", "\\r")
            )
            lines.append(f'{key}="{escaped}"')
    return "\n".join(lines) + "\n"


def write_env_text(path: Path, text: str) -> None:
    """Écrit un `.env` ; un nouveau fichier est créé en 0600 (secrets)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def used_variables(request: HttpRequest) -> list[str]:
    """Noms des balises `{{NOM}}` de la requête, dans l'ordre, sans doublon."""
    blob = " ".join([request.url, request.headers_text(), request.body])
    return list(dict.fromkeys(m.group(1) for m in VAR_RE.finditer(blob)))


def substitute(request: HttpRequest, variables: dict[str, str]) -> HttpRequest:
    """Remplace les `{{NOM}}` connus ; les inconnus sont laissés tels quels."""
    def sub(text: str) -> str:
        return VAR_RE.sub(lambda m: variables.get(m.group(1), m.group(0)), text)

    return replace(
        request,
        url=sub(request.url),
        headers={sub(k): sub(v) for k, v in request.headers.items()},
        body=sub(request.body),
    )
