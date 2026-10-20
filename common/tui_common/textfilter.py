"""Filtre textuel partagé par les listes filtrables (tables SQL, requêtes HTTP)."""

from __future__ import annotations


def matches(text: str, terms: list[str]) -> bool:
    """Chaque mot de `terms` (déjà en minuscules) doit apparaître dans le texte."""
    lowered = text.lower()
    return all(t in lowered for t in terms)
