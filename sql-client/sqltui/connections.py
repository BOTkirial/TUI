"""Modèle de connexion et persistance (fichier JSON dans le dossier config)."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from tui_common import parse_env

# pilotes supportés -> port par défaut
DEFAULT_PORTS = {"postgres": 5432, "mysql": 3306, "mariadb": 3306}


@dataclass
class Connection:
    name: str
    driver: str  # "postgres" | "mysql" | "mariadb"
    host: str = "localhost"
    port: int = 5432
    database: str = ""
    user: str = ""
    password: str = ""
    # tunnel SSH optionnel : host/port ci-dessus sont alors vus depuis le serveur SSH
    ssh_enabled: bool = False
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    # mot de passe SSH, ou passphrase de la clé privée
    ssh_password: str = ""
    # chemin de la clé privée ; vide = agent SSH / clés par défaut (~/.ssh/id_*)
    ssh_key: str = ""

    @property
    def uses_ssh(self) -> bool:
        return self.ssh_enabled and bool(self.ssh_host)

    def label(self) -> str:
        via = f" · ssh {self.ssh_host}" if self.uses_ssh else ""
        return f"{self.name}  ({self.driver} · {self.host}:{self.port}/{self.database}{via})"


def config_path() -> Path:
    """Chemin du fichier de connexions : $SQL_CONNECTIONS_DIR, sinon $XDG_CONFIG_HOME/sqltui."""
    folder = os.environ.get("SQL_CONNECTIONS_DIR")
    if not folder:
        base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
        folder = Path(base) / "sqltui"
    return Path(folder) / "connections.json"


class ConnectionStore:
    """Charge/sauvegarde la liste des connexions."""

    def __init__(self, path: Path | None = None):
        self.path = path or config_path()
        self.connections: list[Connection] = []

    def load(self) -> None:
        if self.path.is_file():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            # champs inconnus ignorés (fichier écrit par une autre version)
            known = {f.name for f in fields(Connection)}
            self.connections = [
                Connection(**{k: v for k, v in item.items() if k in known}) for item in raw
            ]

    def backup(self) -> Path:
        """Copie le fichier (illisible) avant qu'une sauvegarde ne l'écrase."""
        target = self.path.with_name(self.path.name + ".bak")
        shutil.copyfile(self.path, target)
        return target

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = [asdict(c) for c in self.connections]
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _put(self, old_name: str, connection: Connection) -> None:
        # remplace `old_name` en conservant sa position, sinon ajoute en fin
        for i, existing in enumerate(self.connections):
            if existing.name == old_name:
                self.connections[i] = connection
                return
        self.connections.append(connection)

    def add(self, *connections: Connection) -> None:
        """Ajoute (ou remplace par nom) une ou plusieurs connexions, une seule écriture."""
        for connection in connections:
            self._put(connection.name, connection)
        self.save()

    def replace(self, old_name: str, connection: Connection) -> None:
        """Remplace la connexion `old_name` en conservant sa position (renommage possible)."""
        self._put(old_name, connection)
        self.save()

    def remove(self, name: str) -> None:
        self.connections = [c for c in self.connections if c.name != name]
        self.save()


def connection_from_env(env_path: str | Path) -> Connection | None:
    """Construit une connexion à partir d'un .env.

    Reconnaît soit DATABASE_URL, soit les variables DB_HOST/DB_PORT/etc.
    None si le .env ne décrit pas réellement une base (ni nom de base ni utilisateur).
    """
    env = parse_env(env_path)
    if not env:
        return None

    name = Path(env_path).name

    url = env.get("DATABASE_URL") or env.get("DB_URL")
    if url:
        conn = _connection_from_url(name, url)
    else:
        conn = _connection_from_vars(name, env)
    return conn if conn.database or conn.user else None


def _connection_from_vars(name: str, env: dict[str, str]) -> Connection:
    driver = _normalize_driver(env.get("DB_DRIVER") or env.get("DB_CONNECTION") or "postgres")
    return Connection(
        name=name,
        driver=driver,
        host=env.get("DB_HOST", "localhost"),
        port=int(env.get("DB_PORT", DEFAULT_PORTS[driver])),
        database=env.get("DB_NAME") or env.get("DB_DATABASE", ""),
        user=env.get("DB_USER") or env.get("DB_USERNAME", ""),
        password=env.get("DB_PASSWORD") or env.get("DB_PASS", ""),
    )


def _normalize_driver(value: str) -> str:
    value = value.lower()
    if "postgre" in value or value == "pgsql":
        return "postgres"
    if "maria" in value:
        return "mariadb"
    if "mysql" in value:
        return "mysql"
    return value if value in DEFAULT_PORTS else "postgres"


def _connection_from_url(name: str, url: str) -> Connection:
    """Parse une URL type postgres://user:pass@host:port/db."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    driver = _normalize_driver(parsed.scheme.split("+")[0])
    return Connection(
        name=name,
        driver=driver,
        host=parsed.hostname or "localhost",
        port=parsed.port or DEFAULT_PORTS[driver],
        database=(parsed.path or "").lstrip("/"),
        user=parsed.username or "",
        password=parsed.password or "",
    )
