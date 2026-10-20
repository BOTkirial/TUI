"""Accès au démon Docker via le CLI `docker` (sorties JSON).

Le CLI plutôt que le SDK Python : il est de toute façon nécessaire pour
`docker compose`, et une seule voie d'accès évite les écarts de comportement.
Tous les appels sont bloquants : à exécuter dans des workers thread.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field

# labels posés par docker compose sur ses conteneurs
LABEL_PROJECT = "com.docker.compose.project"
LABEL_SERVICE = "com.docker.compose.service"
LABEL_WORKDIR = "com.docker.compose.project.working_dir"
LABEL_CONFIG = "com.docker.compose.project.config_files"

# réseaux créés par Docker lui-même, non supprimables
BUILTIN_NETWORKS = {"bridge", "host", "none"}

_TIMEOUT = 120


class DockerError(Exception):
    """Commande docker en échec (message = stderr du CLI)."""


def run(*args: str, timeout: int = _TIMEOUT) -> str:
    """Exécute `docker <args>` et renvoie stdout ; lève DockerError en cas d'échec."""
    try:
        proc = subprocess.run(
            ["docker", *args], capture_output=True, timeout=timeout,
            # décodage explicite : ne dépend pas de la locale du conteneur
            text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError as exc:
        raise DockerError("CLI `docker` introuvable dans le PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise DockerError(f"délai dépassé : docker {' '.join(args)}") from exc
    if proc.returncode != 0:
        raise DockerError(proc.stderr.strip() or f"docker {args[0]} : code {proc.returncode}")
    return proc.stdout


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
    status: str  # « Up 3 hours », « Exited (0) 2 days ago »…
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
    containers = []
    for data in json.loads(run("container", "inspect", *ids)):
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


def snapshot() -> Snapshot:
    containers = _inspect_containers()
    return Snapshot(
        containers=containers,
        images=_images(containers),
        networks=_networks(containers),
        volumes=_volumes(containers),
    )


# --- actions -------------------------------------------------------------
def compose(stack: Stack, *args: str) -> str:
    """`docker compose` sur une stack existante.

    `up` a besoin des fichiers compose (chemins de l'hôte, lus tels quels :
    le dossier doit être monté au même chemin dans le conteneur) ;
    `stop`/`down` se contentent du nom de projet (conteneurs retrouvés par label).
    """
    cmd = ["compose", "-p", stack.name]
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
    # up/down : construction d'images ou téléchargements possibles, délai large
    return run(*cmd, *args, timeout=600)


def follow_logs(target: Container | Stack, tail: int = 500) -> subprocess.Popen:
    """Flux `docker logs -f` d'un conteneur (`docker compose logs -f` pour une stack).

    stderr est fusionné dans stdout : beaucoup de conteneurs y écrivent leurs logs.
    Sortie en octets (décodage laissé à l'appelant) ; l'appelant termine le processus.
    """
    if isinstance(target, Stack):
        cmd = ["compose", "-p", target.name, "logs", "-f", "--tail", str(tail)]
    else:
        cmd = ["logs", "-f", "--tail", str(tail), target.id]
    try:
        return subprocess.Popen(
            ["docker", *cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise DockerError("CLI `docker` introuvable dans le PATH") from exc


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
