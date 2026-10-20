"""Fuzzing via balises `{{fuzz:liste}}`.

`{{fuzz:numbers}}` = point de fuzzing utilisant la liste `numbers.txt`
du répertoire de listes. Toute autre accolade est laissée telle quelle.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, replace
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
    """Détient les listes disponibles et génère les requêtes concrètes."""

    lists: dict[str, list[str]]

    @classmethod
    def from_directory(cls, directory: str | Path) -> "Fuzzer":
        """Charge chaque `*.txt` du répertoire ; le nom du fichier = nom de liste."""
        lists: dict[str, list[str]] = {}
        folder = Path(directory)
        if folder.is_dir():
            for path in sorted(folder.glob("*.txt")):
                # seuls les noms utilisables dans une balise {{fuzz:…}}
                if LIST_NAME_RE.fullmatch(path.stem):
                    try:
                        lists[path.stem] = read_list_file(path)
                    except OSError:
                        continue  # illisible : signalée comme absente à l'envoi
        return cls(lists=lists)

    def missing_lists(self, request: HttpRequest) -> list[str]:
        """Listes référencées par la requête mais introuvables."""
        return [name for name in self._fuzz_points(request) if name not in self.lists]

    def available(self) -> list[str]:
        return sorted(self.lists)

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
        value_lists = [self.lists.get(name, [""]) for name in points]
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
