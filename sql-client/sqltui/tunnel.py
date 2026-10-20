"""Tunnel SSH (redirection de port locale) via paramiko.

Pour joindre une base accessible seulement depuis un serveur SSH (bastion) :
une écoute sur 127.0.0.1:<port libre> relaie chaque connexion, à travers la
session SSH, vers l'hôte/port de la base tels que vus depuis ce serveur.
"""

from __future__ import annotations

import select
import socket
import socketserver
import threading
from pathlib import Path

import paramiko

from .connections import Connection, config_path


def known_hosts_path() -> Path:
    """Clés d'hôtes SSH acceptées par sqltui (à côté de connections.json)."""
    return config_path().with_name("known_hosts")


def resolve_key_path(raw: str) -> str:
    """Chemin de clé utilisable ici : `~` développé ; si absent, même nom dans ~/.ssh.

    Le repli couvre une clé désignée par un chemin de l'hôte, lue dans Docker
    où seul ~/.ssh est monté.
    """
    path = Path(raw).expanduser()
    if not path.is_file():
        fallback = Path.home() / ".ssh" / path.name
        if fallback.is_file():
            return str(fallback)
    return str(path)


class _ForwardHandler(socketserver.BaseRequestHandler):
    """Relaie une connexion locale vers la base, à travers un canal SSH."""

    def handle(self) -> None:
        server: _ForwardServer = self.server
        try:
            chan = server.transport.open_channel(
                "direct-tcpip", server.remote, self.request.getpeername()
            )
        except Exception:
            # refus du serveur SSH : le client SQL verra la connexion fermée
            return
        # source -> destination, pour chaque sens encore ouvert
        pending = {self.request: chan, chan: self.request}
        try:
            while pending:
                readable, _, _ = select.select(list(pending), [], [])
                for source in readable:
                    target = pending[source]
                    data = source.recv(32768)
                    if data:
                        target.sendall(data)
                    else:
                        # fin d'émission d'un côté : transmise, l'autre sens continue
                        del pending[source]
                        target.shutdown(socket.SHUT_WR)
        except (OSError, EOFError):
            pass
        finally:
            chan.close()


class _ForwardServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, transport: paramiko.Transport, remote: tuple[str, int]):
        # port 0 : le système choisit un port libre
        super().__init__(("127.0.0.1", 0), _ForwardHandler)
        self.transport = transport
        self.remote = remote


class SshTunnel:
    """Session SSH + écoute locale ; `start()` renvoie l'adresse locale à utiliser."""

    def __init__(self, conn: Connection):
        self._conn = conn
        self._client: paramiko.SSHClient | None = None
        self._server: _ForwardServer | None = None

    def start(self) -> tuple[str, int]:
        conn = self._conn
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        known = known_hosts_path()
        known.parent.mkdir(parents=True, exist_ok=True)
        known.touch(exist_ok=True)
        # hôte inconnu : accepté et mémorisé dans `known` à la 1re connexion ;
        # clé différente d'une clé connue → BadHostKeyException (refus)
        client.load_host_keys(str(known))
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        key = resolve_key_path(conn.ssh_key) if conn.ssh_key else None
        try:
            client.connect(
                conn.ssh_host, port=conn.ssh_port, username=conn.ssh_user or None,
                password=conn.ssh_password or None, passphrase=conn.ssh_password or None,
                key_filename=key, allow_agent=not conn.ssh_password,
                # sans clé ni mot de passe : agent + clés par défaut ; sinon on évite
                # d'épuiser les essais autorisés (MaxAuthTries) avec d'autres clés
                look_for_keys=not (key or conn.ssh_password),
                timeout=10, banner_timeout=10, auth_timeout=10,
            )
        except Exception as exc:
            client.close()
            raise ConnectionError(
                f"Tunnel SSH {conn.ssh_host}:{conn.ssh_port} : {exc}"
            ) from exc
        transport = client.get_transport()
        transport.set_keepalive(30)
        self._client = client
        self._server = _ForwardServer(transport, (conn.host, conn.port))
        threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
        ).start()
        return self._server.server_address[:2]

    def close(self) -> None:
        # session d'abord : ferme les canaux, donc les relais en cours
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
