"""Historique des requêtes exécutées, un fichier JSON par connexion.

Les fichiers vivent dans `history/` à côté de `connections.json` ; le nom de
fichier est le nom de la connexion encodé (unique, sans caractère interdit).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from .connections import config_path

# entrées conservées au plus par connexion (les plus anciennes sont oubliées)
MAX_ENTRIES = 500


@dataclass
class HistoryEntry:
    sql: str
    at: str  # horodatage ISO local, à la seconde


def history_dir() -> Path:
    return config_path().parent / "history"


def history_path(name: str) -> Path:
    return history_dir() / (quote(name, safe="") + ".json")


class QueryHistory:
    """Historique d'une connexion, la requête la plus récente en tête."""

    def __init__(self, name: str):
        self.path = history_path(name)
        self.entries: list[HistoryEntry] = []

    def load(self) -> None:
        if not self.path.is_file():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.entries = [HistoryEntry(item["sql"], item.get("at", "")) for item in raw]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = [{"sql": e.sql, "at": e.at} for e in self.entries]
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        # les requêtes peuvent contenir des données sensibles
        os.chmod(self.path, 0o600)

    def add(self, sql: str) -> None:
        """Ajoute `sql` en tête ; une requête déjà présente y est remontée."""
        sql = sql.strip()
        if not sql:
            return
        self.entries = [e for e in self.entries if e.sql != sql]
        self.entries.insert(0, HistoryEntry(sql, datetime.now().isoformat(" ", "seconds")))
        del self.entries[MAX_ENTRIES:]
        self.save()

    def remove(self, entry: HistoryEntry) -> None:
        self.entries = [e for e in self.entries if e is not entry]
        self.save()


def rename_history(old: str, new: str) -> None:
    """Suit le renommage d'une connexion (n'écrase pas un historique existant)."""
    source, target = history_path(old), history_path(new)
    if old != new and source.is_file() and not target.exists():
        source.rename(target)


def delete_history(name: str) -> None:
    history_path(name).unlink(missing_ok=True)
