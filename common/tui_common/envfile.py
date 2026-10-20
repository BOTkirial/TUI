"""Lecture simple de fichiers .env et découverte automatique."""

from __future__ import annotations

from pathlib import Path


def parse_env(path: str | Path) -> dict[str, str]:
    """Parse un fichier .env (KEY=VALUE).

    Gère les commentaires, le préfixe `export` et les guillemets entourants.
    """
    result: dict[str, str] = {}
    p = Path(path)
    if not p.is_file():
        return result

    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        # retire les guillemets entourants éventuels
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            result[key] = value
    return result


def discover_env_files(root: str | Path = ".") -> list[Path]:
    """Cherche les fichiers .env* dans `root` et dans ses sous-dossiers directs."""
    root = Path(root)
    if not root.is_dir():
        return []

    found: list[Path] = []
    patterns = (".env", ".env.*")

    for pattern in patterns:
        found.extend(sorted(root.glob(pattern)))
    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        for pattern in patterns:
            found.extend(sorted(sub.glob(pattern)))

    # dédoublonnage en conservant l'ordre
    seen: set[Path] = set()
    unique: list[Path] = []
    for f in found:
        resolved = f.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(f)
    return unique
