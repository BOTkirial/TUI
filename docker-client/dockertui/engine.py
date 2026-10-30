"""Accès au démon Docker via le CLI `docker` (sorties JSON).

Le CLI plutôt que le SDK Python : il est de toute façon nécessaire pour
`docker compose`, et une seule voie d'accès évite les écarts de comportement.
Tous les appels sont bloquants : à exécuter dans des workers thread.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass, field

# labels posés par docker compose sur ses conteneurs
LABEL_PROJECT = "com.docker.compose.project"
LABEL_SERVICE = "com.docker.compose.service"
LABEL_WORKDIR = "com.docker.compose.project.working_dir"
LABEL_CONFIG = "com.docker.compose.project.config_files"

# réseaux créés par Docker lui-même, non supprimables
BUILTIN_NETWORKS = {"bridge", "host", "none"}

_TIMEOUT = 120

# processus docker en cours, tués à la sortie de l'app : sinon la fermeture attend
# la fin des workers thread (un `compose up` peut durer plusieurs minutes)
_processes: set[subprocess.Popen] = set()
_processes_lock = threading.Lock()
_shutting_down = False


class DockerError(Exception):
    """Commande docker en échec (message = stderr du CLI)."""

    def tail(self, lines: int = 1) -> str:
        """Dernières lignes non vides du message : l'erreur réelle, après les
        lignes de progression que `compose up` écrit aussi sur stderr."""
        kept = [line.strip() for line in str(self).splitlines() if line.strip()]
        return "\n".join(kept[-lines:])


def _popen(args: list[str], **kwargs) -> subprocess.Popen:
    """Lance `docker <args>` dans son propre groupe de processus, suivi par `_processes`.

    Groupe dédié : le plugin compose est un processus fils du CLI, tuer le CLI
    seul le laisserait orphelin ; `_kill` vise tout le groupe.
    """
    with _processes_lock:
        if _shutting_down:
            raise DockerError("fermeture en cours")
        try:
            proc = subprocess.Popen(
                ["docker", *args], stdin=subprocess.DEVNULL, start_new_session=True, **kwargs
            )
        except FileNotFoundError as exc:
            raise DockerError("CLI `docker` introuvable dans le PATH") from exc
        _processes.add(proc)
    return proc


def _forget(proc: subprocess.Popen) -> None:
    with _processes_lock:
        _processes.discard(proc)


def _kill(proc: subprocess.Popen, sig: int = signal.SIGKILL) -> None:
    """Signal à tout le groupe du processus (CLI + plugin compose)."""
    if proc.returncode is not None:
        return  # déjà attendu : son PID a pu être réattribué
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass  # déjà terminé


def kill_all() -> None:
    """Tue les commandes docker en cours et refuse les suivantes (sortie de l'app).

    Les opérations déjà transmises au démon (téléchargement, arrêt…) continuent
    côté Docker ; seul le client est interrompu.
    """
    global _shutting_down
    with _processes_lock:
        _shutting_down = True
        running = list(_processes)
    for proc in running:
        _kill(proc)


def _exec(args: tuple[str, ...], timeout: int, env: dict[str, str] | None = None
          ) -> tuple[int, str, str]:
    """Exécute `docker <args>` : (code de retour, stdout, stderr)."""
    proc = _popen(
        list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        # décodage explicite : ne dépend pas de la locale du conteneur
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill(proc)
        proc.communicate()
        raise DockerError(f"délai dépassé : docker {' '.join(args)}") from exc
    finally:
        _forget(proc)
    return proc.returncode, stdout, stderr


def run(*args: str, timeout: int = _TIMEOUT, env: dict[str, str] | None = None) -> str:
    """Exécute `docker <args>` et renvoie stdout ; lève DockerError en cas d'échec."""
    code, stdout, stderr = _exec(args, timeout, env)
    if code != 0:
        raise DockerError(stderr.strip() or f"docker {args[0]} : code {code}")
    return stdout


def _json_lines(output: str) -> list[dict]:
    """Sortie `--format '{{json .}}'` : un objet JSON par ligne."""
    return [json.loads(line) for line in output.splitlines() if line.strip()]


@dataclass
class Mount:
    """Montage d'un conteneur (volume nommé, bind ou tmpfs)."""

    type: str
    source: str  # nom du volume, ou chemin hôte pour un bind
    destination: str  # point de montage dans le conteneur


@dataclass
class Container:
    id: str
    name: str
    image: str
    image_id: str
    state: str  # running, exited, paused, created…
    status: str  # état détaillé : « running (healthy) », « exited (0) »…
    ports: str
    labels: dict[str, str]
    mounts: list[Mount] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)

    @property
    def project(self) -> str:
        """Stack compose ("" = conteneur lancé hors compose)."""
        return self.labels.get(LABEL_PROJECT, "")

    @property
    def service(self) -> str:
        return self.labels.get(LABEL_SERVICE, "")


@dataclass
class Stack:
    """Projet docker compose, reconstitué depuis les labels de ses conteneurs."""

    name: str
    working_dir: str
    config_files: list[str]
    containers: list[Container]

    @property
    def running(self) -> int:
        return sum(c.state == "running" for c in self.containers)


@dataclass
class Image:
    id: str
    repository: str
    tag: str
    size: str
    created: str  # « 2 weeks ago »
    created_at: str  # « 2024-05-02 10:11:12 +0200 CEST », sert au tri
    used_by: list[str]  # noms des conteneurs (même arrêtés)

    @property
    def ref(self) -> str:
        """Référence à supprimer : le tag seul si l'image en a un, sinon l'ID."""
        if self.repository != "<none>" and self.tag != "<none>":
            return f"{self.repository}:{self.tag}"
        return self.id


@dataclass
class Network:
    id: str
    name: str
    driver: str
    scope: str
    containers: list[str]

    @property
    def builtin(self) -> bool:
        return self.name in BUILTIN_NETWORKS


@dataclass
class VolumeUse:
    container: str
    destination: str


@dataclass
class Volume:
    name: str
    driver: str
    mountpoint: str  # chemin sur le disque de l'hôte
    project: str
    uses: list[VolumeUse]


@dataclass
class Snapshot:
    """État complet du démon, lu en une fois (rafraîchissement F5)."""

    containers: list[Container]
    images: list[Image]
    networks: list[Network]
    volumes: list[Volume]

    def stacks(self) -> list[Stack]:
        """Stacks compose triées par nom ; les conteneurs hors compose sont exclus."""
        grouped: dict[str, list[Container]] = {}
        for c in self.containers:
            if c.project:
                grouped.setdefault(c.project, []).append(c)
        stacks = []
        for name, containers in sorted(grouped.items()):
            labels = containers[0].labels
            files = [f for f in labels.get(LABEL_CONFIG, "").split(",") if f]
            stacks.append(Stack(name, labels.get(LABEL_WORKDIR, ""), files, containers))
        return stacks


# --- lecture -------------------------------------------------------------
def _inspect_containers() -> list[Container]:
    ids = run("ps", "-a", "-q", "--no-trunc").split()
    if not ids:
        return []
    # conteneur supprimé entre `ps` et `inspect` : code 1 (« No such container »),
    # mais les autres sont bien décrits sur stdout
    code, stdout, stderr = _exec(("container", "inspect", *ids), _TIMEOUT)
    try:
        inspected = json.loads(stdout)
    except ValueError:
        inspected = None
    if not isinstance(inspected, list):
        raise DockerError(stderr.strip() or f"docker container inspect : code {code}")
    containers = []
    for data in inspected:
        state = data.get("State") or {}
        config = data.get("Config") or {}
        net = data.get("NetworkSettings") or {}
        containers.append(Container(
            id=data["Id"][:12],
            name=data.get("Name", "").lstrip("/"),
            image=config.get("Image", ""),
            image_id=data.get("Image", ""),
            state=state.get("Status", ""),
            status=_status(state),
            ports=_ports(net.get("Ports") or {}),
            labels=config.get("Labels") or {},
            mounts=[
                Mount(
                    m.get("Type", ""),
                    m.get("Name") or m.get("Source", ""),
                    m.get("Destination", ""),
                )
                for m in data.get("Mounts") or []
            ],
            networks=sorted((net.get("Networks") or {}).keys()),
        ))
    containers.sort(key=lambda c: (c.project, c.service, c.name))
    return containers


def _status(state: dict) -> str:
    """Résumé court de l'état (le texte « Up 3 hours » n'existe que dans `docker ps`)."""
    status = state.get("Status", "")
    if status == "exited":
        return f"exited ({state.get('ExitCode', '?')})"
    health = (state.get("Health") or {}).get("Status")
    return f"{status} ({health})" if health else status


def _ports(ports: dict) -> str:
    """Ports publiés « 8080→80/tcp », dédoublonnés (IPv4 et IPv6)."""
    shown: list[str] = []
    for container_port, bindings in ports.items():
        for b in bindings or []:
            text = f"{b.get('HostPort')}→{container_port}"
            if text not in shown:
                shown.append(text)
    return ", ".join(shown)


def _images(containers: list[Container]) -> list[Image]:
    users: dict[str, list[str]] = {}
    for c in containers:
        # ID court de `image ls` = 12 premiers caractères du sha256
        users.setdefault(c.image_id.removeprefix("sha256:")[:12], []).append(c.name)
    return [
        Image(
            id=d["ID"],
            repository=d.get("Repository", ""),
            tag=d.get("Tag", ""),
            size=d.get("Size", ""),
            created=d.get("CreatedSince", ""),
            created_at=d.get("CreatedAt", ""),
            used_by=users.get(d["ID"], []),
        )
        for d in _json_lines(run("image", "ls", "--format", "{{json .}}"))
    ]


def _networks(containers: list[Container]) -> list[Network]:
    users: dict[str, list[str]] = {}
    for c in containers:
        for name in c.networks:
            users.setdefault(name, []).append(c.name)
    networks = [
        Network(
            id=d["ID"],
            name=d["Name"],
            driver=d.get("Driver", ""),
            scope=d.get("Scope", ""),
            containers=users.get(d["Name"], []),
        )
        for d in _json_lines(run("network", "ls", "--format", "{{json .}}"))
    ]
    networks.sort(key=lambda n: (not n.builtin, n.name))
    return networks


def _volumes(containers: list[Container]) -> list[Volume]:
    uses: dict[str, list[VolumeUse]] = {}
    for c in containers:
        for m in c.mounts:
            if m.type == "volume":
                uses.setdefault(m.source, []).append(VolumeUse(c.name, m.destination))
    volumes = []
    for d in _json_lines(run("volume", "ls", "--format", "{{json .}}")):
        # `volume ls` renvoie les labels en une seule chaîne « k=v,k=v »
        labels = dict(
            item.partition("=")[::2] for item in (d.get("Labels") or "").split(",") if item
        )
        volumes.append(Volume(
            name=d["Name"],
            driver=d.get("Driver", ""),
            mountpoint=d.get("Mountpoint", ""),
            project=labels.get(LABEL_PROJECT, ""),
            uses=uses.get(d["Name"], []),
        ))
    volumes.sort(key=lambda v: (v.project == "", v.project, v.name))
    return volumes


_CONTAINER_ID = re.compile(r"/containers/([0-9a-f]{64})/")


def own_container_id() -> str:
    """ID (12 caractères) du conteneur qui exécute la TUI, "" hors Docker.

    Lu dans les montages de /etc/hostname, /etc/hosts… que Docker fait depuis
    `/var/lib/docker/containers/<id>/` (le nom d'hôte ne sert à rien : le
    conteneur `tui` est en `network_mode: host`, il a celui de la machine).
    """
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as f:
            m = _CONTAINER_ID.search(f.read())
    except OSError:
        return ""
    return m[1][:12] if m else ""


def snapshot() -> Snapshot:
    containers = _inspect_containers()
    return Snapshot(
        containers=containers,
        images=_images(containers),
        networks=_networks(containers),
        volumes=_volumes(containers),
    )


# --- actions -------------------------------------------------------------
def _host_env() -> dict[str, str] | None:
    """Environnement de `compose up` : HOME/USER de l'hôte (HOST_HOME/HOST_USER,
    fixées par docker-compose.yml), pour que `${HOME}`, `${USER}` des fichiers
    compose désignent les mêmes chemins que sur l'hôte (sinon /root : montages
    recréés au mauvais endroit). None = environnement courant (hors Docker)."""
    home, user = os.environ.get("HOST_HOME"), os.environ.get("HOST_USER")
    if not home and not user:
        return None
    env = dict(os.environ)
    # la configuration du CLI (~/.docker) reste celle du conteneur : celle de
    # l'hôte peut désigner des credential helpers absents de l'image
    env.setdefault("DOCKER_CONFIG", os.path.expanduser("~/.docker"))
    if home:
        env["HOME"] = home
    if user:
        env["USER"] = user
    return env


def compose(stack: Stack, *args: str) -> str:
    """`docker compose` sur une stack existante.

    `up` a besoin des fichiers compose (chemins de l'hôte, lus tels quels :
    le dossier doit être monté au même chemin dans le conteneur) ;
    `stop`/`down` se contentent du nom de projet (conteneurs retrouvés par label).
    """
    cmd = ["compose", "-p", stack.name]
    env = None
    if args[0] == "up":
        missing = [f for f in stack.config_files if not os.path.isfile(f)]
        if not stack.config_files or missing:
            raise DockerError(
                "fichier compose introuvable : "
                + (", ".join(missing) or "aucun chemin dans les labels")
                + " (dossier non monté dans le conteneur ?)"
            )
        if stack.working_dir:
            cmd += ["--project-directory", stack.working_dir]
        for f in stack.config_files:
            cmd += ["-f", f]
        env = _host_env()
    # up/down : construction d'images ou téléchargements possibles, délai large
    return run(*cmd, *args, timeout=600, env=env)


def follow_logs(target: Container | Stack, tail: int = 500) -> subprocess.Popen:
    """Flux `docker logs -f` d'un conteneur (`docker compose logs -f` pour une stack).

    stderr est fusionné dans stdout : beaucoup de conteneurs y écrivent leurs logs.
    Sortie en octets (décodage laissé à l'appelant) ; l'appelant l'arrête par `stop_logs`.
    """
    if isinstance(target, Stack):
        cmd = ["compose", "-p", target.name, "logs", "-f", "--tail", str(tail)]
    else:
        cmd = ["logs", "-f", "--tail", str(tail), target.id]
    return _popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def stop_logs(proc: subprocess.Popen) -> None:
    """Arrête un flux de `follow_logs` (tout son groupe : CLI et plugin compose)."""
    _kill(proc, signal.SIGTERM)
    _forget(proc)


def inspect(target: Container | Stack) -> str:
    """`docker inspect` d'un conteneur (de tous ceux d'une stack), JSON indenté."""
    containers = target.containers if isinstance(target, Stack) else [target]
    data = json.loads(run("inspect", *(c.id for c in containers)))
    return json.dumps(data, indent=2, ensure_ascii=False)


def start_container(container: Container) -> None:
    run("start", container.id)


def stop_container(container: Container) -> None:
    run("stop", container.id)


def remove_container(container: Container) -> None:
    run("rm", "-f", container.id)


def remove_image(image: Image) -> None:
    run("image", "rm", image.ref)


def remove_network(network: Network) -> None:
    run("network", "rm", network.id)


def remove_volume(volume: Volume) -> None:
    run("volume", "rm", volume.name)
