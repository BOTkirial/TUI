"""Fuzzing via balises `{{fuzz:liste}}`.

`{{fuzz:numbers}}` = point de fuzzing utilisant la liste `numbers.txt`
du répertoire de listes. Toute autre accolade est laissée telle quelle.
"""

from __future__ import annotations

import itertools
import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from .httpfile import HttpRequest

LIST_NAME_RE = re.compile(r"[A-Za-z0-9_\-]+")
FUZZ_RE = re.compile(r"\{\{\s*fuzz:([A-Za-z0-9_\-]+)\s*\}\}")

# Nombre max de requêtes générées pour éviter l'explosion combinatoire.
MAX_COMBINATIONS = 500


# Marqueur pour la chaîne vide dans un fichier de liste (les lignes vides
# sont ignorées, sinon une ligne blanche accidentelle deviendrait une valeur).
EMPTY_MARKER = "<empty>"


def read_list_file(path: str | Path) -> list[str]:
    """Lit une liste : une valeur par ligne, espaces conservés tels quels."""
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # listes de mots classiques (rockyou…) en latin-1 : chaque octet est conservé
        text = raw.decode("latin-1")
    values = []
    for line in text.splitlines():
        if line == "":
            continue
        values.append("" if line == EMPTY_MARKER else line)
    return values


@dataclass
class Fuzzer:
    """Listes d'un répertoire (un `*.txt` par liste) et génération des requêtes.

    Une liste n'est lue que si une requête la référence, puis gardée en cache
    tant que son fichier n'est pas modifié : une grosse liste (rockyou…) posée
    dans le répertoire ne ralentit pas chaque envoi.
    """

    directory: Path
    # nom -> (mtime du fichier, valeurs)
    _cache: dict[str, tuple[float, list[str]]] = field(default_factory=dict)

    @classmethod
    def from_directory(cls, directory: str | Path) -> "Fuzzer":
        return cls(Path(directory))

    def available(self) -> list[str]:
        """Listes du répertoire (noms utilisables dans une balise {{fuzz:…}})."""
        if not self.directory.is_dir():
            return []
        return sorted(
            p.stem for p in self.directory.glob("*.txt") if LIST_NAME_RE.fullmatch(p.stem)
        )

    def _values(self, name: str) -> list[str] | None:
        """Valeurs de la liste `name` (relue si modifiée), None si illisible."""
        path = self.directory / f"{name}.txt"
        try:
            mtime = path.stat().st_mtime
            cached = self._cache.get(name)
            if cached is None or cached[0] != mtime:
                self._cache[name] = (mtime, read_list_file(path))
        except OSError:
            self._cache.pop(name, None)
            return None
        return self._cache[name][1]

    def missing_lists(self, request: HttpRequest) -> list[str]:
        """Listes référencées par la requête mais introuvables ou vides."""
        return [name for name in self._fuzz_points(request) if not self._values(name)]

    def count(self, request: HttpRequest) -> int:
        """Nombre total de combinaisons, avant la borne `MAX_COMBINATIONS`."""
        return math.prod(len(self._values(n) or [""]) for n in self._fuzz_points(request))

    def _fuzz_points(self, request: HttpRequest) -> list[str]:
        """Liste ordonnée et dédoublonnée des listes de fuzz utilisées."""
        blob = " ".join([request.url, request.headers_text(), request.body])
        seen: list[str] = []
        for match in FUZZ_RE.finditer(blob):
            name = match.group(1)
            if name not in seen:
                seen.append(name)
        return seen

    def expand(self, request: HttpRequest) -> list[tuple[dict[str, str], HttpRequest]]:
        """Produit les requêtes concrètes.

        Renvoie une liste de (labels, requête) où `labels` indique la valeur
        tirée pour chaque balise de fuzz.
        """
        points = self._fuzz_points(request)
        if not points:
            return [({}, request)]

        # produit cartésien borné des valeurs de chaque liste
        value_lists = [self._values(name) or [""] for name in points]
        combos = itertools.islice(
            itertools.product(*value_lists), MAX_COMBINATIONS
        )

        results: list[tuple[dict[str, str], HttpRequest]] = []
        for combo in combos:
            mapping = dict(zip(points, combo))
            results.append((mapping, self._materialize(request, mapping)))
        return results

    def _materialize(self, request: HttpRequest, fuzz: dict[str, str]) -> HttpRequest:
        def sub(text: str) -> str:
            return FUZZ_RE.sub(lambda m: fuzz.get(m.group(1), m.group(0)), text)

        return replace(
            request,
            url=sub(request.url),
            headers={k: sub(v) for k, v in request.headers.items()},
            body=sub(request.body),
        )
