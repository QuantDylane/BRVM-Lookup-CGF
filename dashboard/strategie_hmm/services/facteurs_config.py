"""Gestion de la liste des facteurs exclus du pipeline HMM.

Permet à l'utilisateur de retirer ou réactiver un facteur depuis l'onglet
« Robustesse statistique ». La configuration est persistée dans ``ApiConfig``
(clé ``FACTEURS_EXCLUS`` = JSON list).
"""
from __future__ import annotations

import json

from dashboard.models import ApiConfig


CONFIG_KEY = "FACTEURS_EXCLUS"


def get_facteurs_exclus() -> set[str]:
    """Retourne l'ensemble des codes facteurs actuellement exclus (forme canonique)."""
    raw = ApiConfig.get(CONFIG_KEY, default="")
    if not raw:
        return set()
    try:
        data = json.loads(raw)
        return {str(c) for c in data if isinstance(c, str)}
    except (ValueError, TypeError):
        return set()


def set_facteur_exclu(code: str, exclu: bool) -> set[str]:
    """Ajoute ou retire un code de la liste d'exclusion. Retourne la liste mise à jour."""
    code = (code or "").strip()
    if not code:
        return get_facteurs_exclus()
    exclus = get_facteurs_exclus()
    # Retirer toute forme antérieure du même code (case-insensitive)
    exclus = {c for c in exclus if c.upper() != code.upper()}
    if exclu:
        exclus.add(code)
    ApiConfig.set(
        CONFIG_KEY,
        json.dumps(sorted(exclus)),
        description="Liste des facteurs HMM exclus du pipeline (étape 4 — robustesse)",
    )
    return exclus


def filtrer_facteurs_actifs(codes: list[str]) -> list[str]:
    """Renvoie la liste `codes` privée des facteurs exclus, préservant l'ordre."""
    exclus_upper = {c.upper() for c in get_facteurs_exclus()}
    return [c for c in codes if c.upper() not in exclus_upper]
