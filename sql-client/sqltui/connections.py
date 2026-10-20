"""Modèle de connexion et persistance (fichier JSON dans le dossier config)."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, fields
from pathlib import Path

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

