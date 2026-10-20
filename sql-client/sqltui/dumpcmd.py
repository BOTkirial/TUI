"""Commandes shell de dump / restauration d'une base, à coller dans un terminal de l'hôte.

Dump au format SQL texte dans ~/Téléchargements (pg_dump, mysqldump / mariadb-dump),
restauration par psql / mysql depuis l'entrée standard. Le mot de passe est inclus
(PGPASSWORD, --password) et la commande commence alors par une espace : bash ne
l'enregistre pas dans l'historique avec HISTCONTROL=ignorespace/ignoreboth.
Connexion via tunnel SSH : l'outil tourne sur le serveur SSH (`ssh … 'cmd' > fichier`),
d'où la base est vue avec le même hôte/port que le tunnel.
"""

from __future__ import annotations

import re
import shlex
from datetime import datetime

from .connections import Connection

# `~` hors guillemets : développé par le shell de l'hôte, pas par le conteneur
DUMP_DIR = "~/Téléchargements"


def dump_command(conn: Connection, database: str) -> str:
    """Commande qui dumpe `database` dans ~/Téléchargements/<base>_<horodatage>.sql."""
    database = _require_database(conn, database)
    name = database or conn.user or conn.name
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = f"{DUMP_DIR}/{_safe_filename(name)}_{stamp}.sql"
    if conn.driver == "postgres":
        # sans propriétaires ni droits : restaurable avec un autre utilisateur
        tool = ["pg_dump", *_pg_args(conn, database), "--no-owner", "--no-privileges"]
    else:
        dump = "mariadb-dump" if conn.driver == "mariadb" else "mysqldump"
        tool = [dump, *_mysql_args(conn), "--single-transaction", "--routines",
                "--triggers", database]
    return _finish(conn, tool, f"> {target}")


def restore_command(conn: Connection, database: str) -> str:
    """Commande qui restaure un dump .sql dans `database`.

    Se termine par le dossier des dumps : il reste à compléter le nom du fichier
    (tabulation), ce qui évite aussi une exécution accidentelle au collage.
    """
    database = _require_database(conn, database)
    if conn.driver == "postgres":
        tool = ["psql", *_pg_args(conn, database), "-v", "ON_ERROR_STOP=1"]
    else:
        client = "mariadb" if conn.driver == "mariadb" else "mysql"
        tool = [client, *_mysql_args(conn), database]
    return _finish(conn, tool, f"< {DUMP_DIR}/")


def _require_database(conn: Connection, database: str) -> str:
    # Postgres : sans nom, la base par défaut de l'utilisateur ; MySQL : obligatoire
    if not database and conn.driver != "postgres":
        raise ValueError("Aucune base sélectionnée")
    return database


def _pg_args(conn: Connection, database: str) -> list[str]:
    args = ["-h", conn.host, "-p", str(conn.port)]
    if conn.user:
        args += ["-U", conn.user]
    if database:
        args += ["-d", database]
    return args


def _mysql_args(conn: Connection) -> list[str]:
    # TCP forcé : sinon « localhost » passe par le socket Unix, pas par host/port
    args = ["-h", conn.host, "-P", str(conn.port), "--protocol=TCP"]
    if conn.user:
        args += ["-u", conn.user]
    if conn.password:
        args.append(f"--password={conn.password}")
    return args


def _finish(conn: Connection, tool: list[str], redirect: str) -> str:
    """Assemble la commande : mot de passe Postgres, enveloppe SSH, redirection."""
    command = shlex.join(tool)
    if conn.driver == "postgres" and conn.password:
        command = f"PGPASSWORD={shlex.quote(conn.password)} {command}"
    if conn.uses_ssh:
        command = f"{_ssh_prefix(conn)} {shlex.quote(command)}"
    command = f"{command} {redirect}"
    # espace en tête : hors de l'historique bash (le mot de passe y figurerait)
    return f" {command}" if conn.password else command


def _ssh_prefix(conn: Connection) -> str:
    args = ["ssh"]
    if conn.ssh_port != 22:
        args += ["-p", str(conn.ssh_port)]
    if conn.ssh_key:
        args += ["-i", _host_key_path(conn.ssh_key)]
    target = f"{conn.ssh_user}@{conn.ssh_host}" if conn.ssh_user else conn.ssh_host
    return " ".join([*args, shlex.quote(target)])


def _host_key_path(path: str) -> str:
    """Chemin de clé pour le shell de l'hôte, `~/` laissé hors guillemets."""
    # chemin vu dans le conteneur (/root/.ssh monté depuis ~/.ssh) -> chemin de l'hôte
    if path.startswith("/root/.ssh/"):
        path = "~/.ssh/" + path.removeprefix("/root/.ssh/")
    if path.startswith("~/"):
        return "~/" + shlex.quote(path[2:])
    return shlex.quote(path)


def _safe_filename(name: str) -> str:
    return re.sub(r"[^\w.-]", "_", name) or "dump"
