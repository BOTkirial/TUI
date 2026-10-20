"""Import des connexions DBeaver.

DBeaver stocke les métadonnées dans `data-sources.json` et les identifiants
(chiffrés) dans `credentials-config.json`. On importe toujours les
métadonnées ; le déchiffrement des mots de passe est tenté en « best effort »
(clé locale historique de DBeaver) et échoue silencieusement si indisponible.

Ce sont les propres connexions de l'utilisateur, sur sa machine — comme un
export depuis son gestionnaire de mots de passe.
"""

from __future__ import annotations

import base64
import glob
import json
from pathlib import Path

from .connections import DEFAULT_PORTS, Connection

# clé AES-128 locale utilisée par DBeaver pour le secure storage "par défaut"
_DBEAVER_KEY = bytes.fromhex("babb4a9f774ab853c96c2d653dfe544a")

# providers DBeaver supportés -> notre pilote (les autres sont ignorés)
_PROVIDER_MAP = {
    "postgresql": "postgres",
    "postgres": "postgres",
    "mysql": "mysql",
    "mariadb": "mariadb",
}


def _map_provider(provider: str) -> str | None:
    provider = (provider or "").lower()
    for key, driver in _PROVIDER_MAP.items():
        if key in provider:
            return driver
    return None


def default_workspace_paths() -> list[Path]:
    """Emplacements habituels du workspace DBeaver selon l'OS."""
    home = Path.home()
    candidates = [
        home / ".local/share/DBeaverData/workspace6/General/.dbeaver",
        home / "Library/DBeaverData/workspace6/General/.dbeaver",  # macOS
        home / "AppData/Roaming/DBeaverData/workspace6/General/.dbeaver",  # Windows
    ]
    # workspaces versionnés (workspace6, workspace7…)
    for pattern in glob.glob(str(home / ".local/share/DBeaverData/workspace*/General/.dbeaver")):
        candidates.append(Path(pattern))
    return [c for c in candidates if c.is_dir()]


def _decrypt_blob(blob: bytes) -> dict:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    iv, ciphertext = blob[:16], blob[16:]
    cipher = Cipher(algorithms.AES(_DBEAVER_KEY), modes.CBC(iv))
    decryptor = cipher.decryptor()
    plain = decryptor.update(ciphertext) + decryptor.finalize()
    plain = plain[: -plain[-1]]  # retire le padding PKCS7
    # DBeaver préfixe parfois par des octets nuls
    text = plain.decode("utf-8", errors="ignore").lstrip("\x00").strip()
    return json.loads(text)


def _decrypt_credentials(path: Path) -> dict:
    """Déchiffre credentials-config.json. Renvoie {} en cas d'échec."""
    try:
        raw = path.read_bytes()
    except OSError:
        return {}
    # binaire brut (DBeaver actuel) ; base64 gardé en repli (anciens formats)
    for decode in (bytes, base64.b64decode):
        try:
            return _decrypt_blob(decode(raw))
        except Exception:
            continue
    return {}


def _portable_key_path(path: str) -> str:
    """`/home/moi/.ssh/cle` -> `~/.ssh/cle` : reste valable dans Docker (~/.ssh monté)."""
    _, sep, tail = path.replace("\\", "/").partition("/.ssh/")
    return f"~/.ssh/{tail}" if sep and tail else path


def _ssh_settings(cfg: dict, cred: dict) -> dict:
    """Champs `ssh_*` d'une Connection d'après le handler `ssh_tunnel` de DBeaver.

    `cred` = identifiants déchiffrés de l'entrée `network/ssh_tunnel`.
    Les serveurs de rebond (jumpServer) ne sont pas repris.
    """
    handler = cfg.get("handlers", {}).get("ssh_tunnel") or {}
    props = handler.get("properties", {})
    if not props.get("host"):
        return {}
    try:
        # DBeaver écrit parfois les entiers en flottant (22.0)
        port = int(float(props.get("port") or 22))
    except (TypeError, ValueError):
        port = 22
    key = props.get("keyPath", "") if props.get("authType") == "PUBLIC_KEY" else ""
    return dict(
        ssh_enabled=bool(handler.get("enabled")),
        ssh_host=props["host"],
        ssh_port=port,
        ssh_user=cred.get("user", props.get("user", "")),
        ssh_password=cred.get("password", ""),
        ssh_key=_portable_key_path(key),
    )


def import_connections(workspace: str | Path | None = None) -> list[Connection]:
    """Importe les connexions depuis un workspace DBeaver."""
    roots = [Path(workspace)] if workspace else default_workspace_paths()
    for root in roots:
        sources = root / "data-sources.json"
        if sources.is_file():
            creds = _decrypt_credentials(root / "credentials-config.json")
            return _parse_sources(sources, creds)
    return []


def _parse_sources(sources_path: Path, creds: dict) -> list[Connection]:
    data = json.loads(sources_path.read_text(encoding="utf-8"))
    out: list[Connection] = []

    for conn_id, conn in data.get("connections", {}).items():
        cfg = conn.get("configuration", {})
        driver = _map_provider(conn.get("provider", "") or cfg.get("type", ""))
        if driver is None:
            continue  # on ne garde que pg / mysql / maria

        conn_creds = creds.get(conn_id, {})
        cred = conn_creds.get("#connection", {})
        try:
            port = int(cfg.get("port") or DEFAULT_PORTS[driver])
        except (TypeError, ValueError):
            port = DEFAULT_PORTS[driver]

        out.append(
            Connection(
                name=conn.get("name", conn_id),
                driver=driver,
                host=cfg.get("host", "localhost"),
                port=port,
                database=cfg.get("database", ""),
                user=cred.get("user", cfg.get("user", "")),
                password=cred.get("password", ""),
                **_ssh_settings(cfg, conn_creds.get("network/ssh_tunnel", {})),
            )
        )
    return out
