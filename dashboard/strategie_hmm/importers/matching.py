"""Mapping entre les noms longs des sociétés Excel et les tickers de la BD."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from dashboard.models import Action


PAYS_SUFFIXES = {
    "cote d ivoire": "ci", "cote d'ivoire": "ci",
    "burkina faso": "bf", "burkina": "bf",
    "benin": "bj", "mali": "ml", "niger": "ne",
    "senegal": "sn", "togo": "tg",
    "guinee bissau": "gw", "guinee": "gw",
}


def _normalize_basic(s: str) -> str:
    s = (s or "").lower().replace("'", " ").replace("`", " ")
    table = str.maketrans({
        "é": "e", "è": "e", "ê": "e", "ë": "e", "à": "a", "â": "a",
        "î": "i", "ï": "i", "ô": "o", "ö": "o", "ù": "u", "û": "u",
        "ç": "c", "�": "e",
    })
    s = s.translate(table)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _extract_pays(norm: str) -> tuple[str, str | None]:
    """Détache le suffixe pays. Retourne (slug_sans_pays, code_pays|None)."""
    for label, code in sorted(PAYS_SUFFIXES.items(), key=lambda x: -len(x[0])):
        if norm.endswith(" " + label) or norm == label:
            return norm[: -len(label)].strip(), code
    return norm, None


def _slug(s: str) -> tuple[str, str | None]:
    return _extract_pays(_normalize_basic(s))


def matcher_societes_excel_vers_actions(noms_excel: list[str]) -> dict[str, str | None]:
    """Match nom Excel → ticker BD en tenant compte du pays via le suffixe ticker."""
    actions = list(Action.objects.values("ticker", "nom"))
    actions_idx = []
    for a in actions:
        slug, pays = _slug(a["nom"])
        # Si le nom BD ne contient pas de pays, on déduit du suffixe ticker
        if not pays and "." in a["ticker"]:
            pays = a["ticker"].rsplit(".", 1)[1].lower()
        actions_idx.append((a["ticker"], slug, pays))

    out: dict[str, str | None] = {}
    for nom in noms_excel:
        slug_excel, pays_excel = _slug(nom)
        if not slug_excel:
            out[nom] = None
            continue
        # 1. match exact slug + pays
        cand_pays = [t for t, s, p in actions_idx if s == slug_excel and p == pays_excel]
        if cand_pays:
            out[nom] = cand_pays[0]
            continue
        # 2. fuzzy avec bonus si le pays correspond
        best_score, best_ticker = 0.0, None
        for t, s, p in actions_idx:
            r = SequenceMatcher(None, slug_excel, s).ratio()
            if pays_excel and p == pays_excel:
                r += 0.15
            if r > best_score:
                best_score, best_ticker = r, t
        out[nom] = best_ticker if best_score >= 0.7 else None
    return out
