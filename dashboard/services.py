"""
Couche de services partagée entre les vues et les agents.
Source unique de vérité pour toute la logique métier.
"""

import subprocess
import sys
import json
import os

import numpy as np
from django.conf import settings
from django.core.management import call_command
from django.utils import timezone


# ============================================================
# Service de Scraping
# ============================================================

def run_scraping(scraper_type="actions"):
    """Lance un scraper et importe les données.
    Utilisé par la page Actualisation ET par le ScraperAgent.

    Returns: dict avec statut, message, nb_elements
    """
    from dashboard.models import ScrapingLog, HistoriqueAction, HistoriqueIndice, News

    log = ScrapingLog.objects.create(
        type_scraping=scraper_type,
        statut="en_cours",
        message=f"Lancement du scraping {scraper_type}..."
    )

    try:
        base_dir = settings.BASE_DIR
        if scraper_type == "actions":
            script = base_dir / "scraper_brvm.py"
        elif scraper_type == "news":
            script = base_dir / "scraper_news_brvm.py"
        else:
            script = base_dir / "scraper_brvm.py"

        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(base_dir),
        )

        if result.returncode != 0:
            log.statut = "erreur"
            log.message = result.stderr[-1000:] if result.stderr else "Code retour non nul"
            log.date_fin = timezone.now()
            log.save()
            return {
                "statut": "erreur",
                "message": log.message,
                "log_id": log.id,
                "nb_elements": 0,
            }

        if scraper_type == "actions":
            call_command("import_data", "--only", "actions")
            call_command("import_data", "--only", "indices")
            call_command("import_data", "--only", "societes")
            nb = HistoriqueAction.objects.count() + HistoriqueIndice.objects.count()
        elif scraper_type == "news":
            call_command("import_data", "--only", "news")
            nb = News.objects.count()
        else:
            call_command("import_data")
            nb = HistoriqueAction.objects.count()

        log.statut = "succes"
        log.message = result.stdout[-1000:] if result.stdout else "OK"
        log.nb_elements = nb
        log.date_fin = timezone.now()
        log.save()

        return {
            "statut": "succes",
            "message": f"Import OK — {nb} éléments",
            "log_id": log.id,
            "nb_elements": nb,
        }

    except subprocess.TimeoutExpired:
        log.statut = "erreur"
        log.message = "Timeout après 10 minutes"
        log.date_fin = timezone.now()
        log.save()
        return {"statut": "erreur", "message": log.message, "log_id": log.id, "nb_elements": 0}

    except Exception as e:
        log.statut = "erreur"
        log.message = str(e)
        log.date_fin = timezone.now()
        log.save()
        return {"statut": "erreur", "message": str(e), "log_id": log.id, "nb_elements": 0}


# ============================================================
# Service d'Analyse Technique
# ============================================================

def calculer_indicateurs_action(action):
    """Calcule tous les indicateurs techniques pour une action.
    Utilisé par l'AnalyseAgent ET par les vues d'analyse.

    Returns: dict d'indicateurs ou None si données insuffisantes
    """
    from dashboard.models import HistoriqueAction

    hists = list(
        HistoriqueAction.objects.filter(action=action)
        .order_by("date")
        .values_list("date", "ouverture", "plus_haut", "plus_bas", "cloture", "volume_titres")
    )

    closes = [h[4] for h in hists if h[4] is not None]
    if len(closes) < 50:
        return None

    arr = np.array(closes, dtype=float)
    indicateurs = {}

    # RSI 14
    deltas = np.diff(arr)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[-14:])
    avg_loss = np.mean(losses[-14:])
    if avg_loss > 0:
        rs = avg_gain / avg_loss
        indicateurs["RSI"] = round(100 - (100 / (1 + rs)), 2)
    else:
        indicateurs["RSI"] = 100.0

    # SMA 20 / 50
    indicateurs["SMA20"] = round(float(np.mean(arr[-20:])), 2)
    indicateurs["SMA50"] = round(float(np.mean(arr[-50:])), 2)

    # EMA 12 / 26
    ema12 = _ema(arr, 12)
    ema26 = _ema(arr, 26)

    # MACD
    if ema12 is not None and ema26 is not None:
        indicateurs["MACD"] = round(ema12 - ema26, 2)

    # Bollinger Bands
    sma20 = np.mean(arr[-20:])
    std20 = np.std(arr[-20:])
    indicateurs["BB_upper"] = round(sma20 + 2 * std20, 2)
    indicateurs["BB_lower"] = round(sma20 - 2 * std20, 2)

    # ATR 14
    highs = [h[2] for h in hists if h[2] is not None]
    lows = [h[3] for h in hists if h[3] is not None]
    if len(highs) >= 15 and len(lows) >= 15:
        h_arr = highs[-15:]
        l_arr = lows[-15:]
        c_arr = closes[-15:]
        trs = []
        for i in range(1, min(len(h_arr), len(l_arr), len(c_arr))):
            tr = max(
                h_arr[i] - l_arr[i],
                abs(h_arr[i] - c_arr[i - 1]),
                abs(l_arr[i] - c_arr[i - 1])
            )
            trs.append(tr)
        if trs:
            indicateurs["ATR"] = round(np.mean(trs[-14:]), 2)

    indicateurs["dernier_cours"] = closes[-1]
    indicateurs["variation_1j"] = round((closes[-1] / closes[-2] - 1) * 100, 2) if len(closes) >= 2 else 0

    return indicateurs


def calculer_tous_indicateurs(persist=False):
    """Calcule les indicateurs pour toutes les actions.
    Si persist=True, stocke les résultats dans IndicateurCache.
    Returns: dict {ticker: indicateurs}
    """
    from dashboard.models import Action, IndicateurCache

    resultats = {}
    nb_ok = 0
    nb_skip = 0

    for action in Action.objects.all():
        indics = calculer_indicateurs_action(action)
        if indics:
            resultats[action.ticker] = indics
            nb_ok += 1

            if persist:
                IndicateurCache.objects.update_or_create(
                    action=action,
                    defaults={"indicateurs_json": indics},
                )
        else:
            nb_skip += 1

    return {
        "resultats": resultats,
        "nb_ok": nb_ok,
        "nb_skip": nb_skip,
    }


def get_indicateurs_cache(action):
    """Récupère les indicateurs depuis le cache.
    Returns: dict d'indicateurs ou None si cache vide/périmé.
    """
    from dashboard.models import IndicateurCache

    try:
        cache = IndicateurCache.objects.get(action=action)
        return {
            "indicateurs": cache.indicateurs_json,
            "date_calcul": cache.date_calcul.isoformat(),
        }
    except IndicateurCache.DoesNotExist:
        return None


# ============================================================
# Service de Signaux IA
# ============================================================

def get_api_key():
    """Récupère la clé API Claude depuis la config unique."""
    from dashboard.models import ApiConfig
    return ApiConfig.get("ANTHROPIC_API_KEY")


def generer_signal_ia(ticker, indicateurs, api_key=None, modele="sonnet"):
    """Génère un signal de trading IA pour une action.
    Utilisé par le SignalAgent ET par la page Analyse Actions.

    Returns: dict signal_data ou None
    """
    if not api_key:
        api_key = get_api_key()
    if not api_key:
        return None

    model_map = {
        "haiku": "claude-haiku-4-5-20251001",
        "sonnet": "claude-sonnet-4-20250514",
        "opus": "claude-opus-4-0-20250514",
    }
    model_id = model_map.get(modele, model_map["sonnet"])

    system_prompt = """Tu es un analyste technique senior spécialisé sur la BRVM (Bourse Régionale des Valeurs Mobilières).
Tu dois analyser les données techniques fournies et produire une recommandation de trading structurée.

IMPORTANT: Tu dois répondre UNIQUEMENT en JSON valide, sans aucun texte avant ou après.
Le JSON doit suivre exactement cette structure:

{
    "signal": "ACHAT" ou "NEUTRE" ou "VENTE",
    "confiance": nombre entre 0 et 100,
    "supports": [liste des niveaux de support identifiés],
    "resistances": [liste des niveaux de résistance identifiés],
    "justification": "Explication technique détaillée de 3-5 phrases",
    "prix_entree": prix d'entrée recommandé ou null,
    "stop_loss": niveau de stop loss recommandé ou null,
    "take_profit": niveau de take profit recommandé ou null,
    "risk_reward": ratio risque/rendement calculé ou null
}

Règles:
- Signal ACHAT: indicateurs haussiers dominants, RSI non suracheté, MACD positif ou croisement haussier
- Signal VENTE: indicateurs baissiers dominants, RSI non survendu, MACD négatif ou croisement baissier
- Signal NEUTRE: signaux mixtes ou manque de données
- Confiance: 0-30 faible, 31-60 moyenne, 61-100 forte
- Stop loss: généralement sous le support le plus proche pour un achat
- Take profit: basé sur les niveaux de résistance (achat) ou support (vente)"""

    context = f"Analyse technique pour {ticker} sur la BRVM.\nIndicateurs actuels: {json.dumps(indicateurs, ensure_ascii=False)}"

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        response = client.messages.create(
            model=model_id,
            max_tokens=2000,
            temperature=0,
            system=system_prompt,
            messages=[{"role": "user", "content": context}],
        )

        text = response.content[0].text.strip()

        json_start = text.find("{")
        json_end = text.rfind("}") + 1
        if json_start != -1 and json_end > json_start:
            text = text[json_start:json_end]

        return json.loads(text)

    except Exception:
        return None


def sauvegarder_signal(action, signal_data, indicateurs, modele="sonnet"):
    """Sauvegarde un signal dans la base de données.
    Point unique de sauvegarde pour les agents ET les vues.
    Détecte et enregistre les changements de signal.
    """
    from dashboard.models import TradingSignal, SignalChangement

    nouveau_signal_type = signal_data.get("signal", "NEUTRE")
    nouveau_confiance = signal_data.get("confiance", 50)

    # Chercher le dernier signal pour cette action
    dernier = TradingSignal.objects.filter(action=action).first()

    # Créer le nouveau signal
    nouveau = TradingSignal.objects.create(
        action=action,
        signal=nouveau_signal_type,
        confiance=nouveau_confiance,
        prix_entree=signal_data.get("prix_entree"),
        stop_loss=signal_data.get("stop_loss"),
        take_profit=signal_data.get("take_profit"),
        risk_reward=signal_data.get("risk_reward"),
        supports=signal_data.get("supports", []),
        resistances=signal_data.get("resistances", []),
        justification=signal_data.get("justification", ""),
        indicateurs_utilises=list(indicateurs.keys()),
        valeurs_indicateurs=indicateurs,
        modele_ia=modele,
        prix_actuel=indicateurs.get("dernier_cours"),
    )

    # Détecter le changement de signal
    if dernier and dernier.signal != nouveau_signal_type:
        SignalChangement.objects.create(
            action=action,
            ancien_signal=dernier.signal,
            nouveau_signal=nouveau_signal_type,
            ancien_confiance=dernier.confiance,
            nouveau_confiance=nouveau_confiance,
            signal_precedent=dernier,
            signal_nouveau=nouveau,
            justification=signal_data.get("justification", ""),
        )

    return nouveau


# ============================================================
# Service de Détection d'Alertes
# ============================================================

def detecter_alertes_action(action, lookback=30, seuil_variation=5.0, seuil_volume=3.0):
    """Détecte les anomalies pour une action donnée.
    Returns: liste de dicts d'alertes
    """
    from dashboard.models import HistoriqueAction

    hists = list(
        HistoriqueAction.objects.filter(action=action)
        .order_by("-date")
        .values("date", "cloture", "volume_titres", "variation_pct")[:lookback + 1]
    )

    if len(hists) < 2:
        return []

    alertes = []
    dernier = hists[0]

    # Variation extrême
    if dernier["variation_pct"] and abs(dernier["variation_pct"]) >= seuil_variation:
        direction = "hausse" if dernier["variation_pct"] > 0 else "baisse"
        alertes.append({
            "type": "variation_extreme",
            "niveau": "warning",
            "titre": f"{action.ticker}: variation extrême ({dernier['variation_pct']:+.1f}%)",
            "message": f"{action.ticker} en forte {direction} de {dernier['variation_pct']:+.1f}% "
                       f"le {dernier['date']}. Cours: {dernier['cloture']} FCFA.",
            "donnees": {"ticker": action.ticker, "valeur": dernier["variation_pct"]},
        })

    # Volume anormal
    volumes = [h["volume_titres"] for h in hists[1:] if h["volume_titres"] and h["volume_titres"] > 0]
    if volumes and dernier["volume_titres"]:
        vol_moyen = np.mean(volumes)
        if vol_moyen > 0 and dernier["volume_titres"] / vol_moyen >= seuil_volume:
            ratio = dernier["volume_titres"] / vol_moyen
            alertes.append({
                "type": "volume_anormal",
                "niveau": "info",
                "titre": f"{action.ticker}: volume anormal (x{ratio:.1f})",
                "message": f"Volume de {dernier['volume_titres']:,} titres, soit {ratio:.1f}x "
                           f"la moyenne sur {len(volumes)} jours.",
                "donnees": {"ticker": action.ticker, "ratio": round(ratio, 1)},
            })

    # Cassure SMA20
    closes = [h["cloture"] for h in reversed(hists) if h["cloture"]]
    if len(closes) >= 20:
        sma20 = np.mean(closes[-20:])
        cours = closes[-1]
        cours_prec = closes[-2]

        if cours_prec < sma20 and cours > sma20:
            alertes.append({
                "type": "cassure_haussiere",
                "niveau": "info",
                "titre": f"{action.ticker}: cassure haussière SMA20",
                "message": f"{action.ticker} franchit la SMA20 ({sma20:.0f}) par le haut. Cours: {cours} FCFA.",
                "donnees": {"ticker": action.ticker, "sma20": round(sma20, 2)},
            })
        elif cours_prec > sma20 and cours < sma20:
            alertes.append({
                "type": "cassure_baissiere",
                "niveau": "warning",
                "titre": f"{action.ticker}: cassure baissière SMA20",
                "message": f"{action.ticker} passe sous la SMA20 ({sma20:.0f}). Cours: {cours} FCFA.",
                "donnees": {"ticker": action.ticker, "sma20": round(sma20, 2)},
            })

    return alertes


# ============================================================
# Helpers internes
# ============================================================

def _ema(arr, period):
    if len(arr) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = float(np.mean(arr[:period]))
    for price in arr[period:]:
        ema = (float(price) - ema) * multiplier + ema
    return ema
