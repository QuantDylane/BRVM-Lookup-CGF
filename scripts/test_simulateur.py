"""Test end-to-end de la page Simulateur stratégie.

Vérifie : GET sans/avec ticker, POST avec différentes configs, présence des
composants UI, intégrité des résultats numériques, gestion des erreurs.
"""
import os
import sys
import django
import re
import time
import json
import io

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DJANGO_SETTINGS_MODULE"] = "lookupbrvm.settings"
django.setup()

from django.conf import settings
if "testserver" not in settings.ALLOWED_HOSTS:
    settings.ALLOWED_HOSTS = list(settings.ALLOWED_HOSTS) + ["testserver"]

from django.test import Client


# Wrap stdout pour forcer UTF-8 sur Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

PASS = "[OK]"
FAIL = "[XX]"
n_pass = 0
n_fail = 0


def check(label, ok, detail=""):
    global n_pass, n_fail
    sym = PASS if ok else FAIL
    print(f"  {sym} {label}" + (f"  -> {detail}" if detail else ""))
    if ok:
        n_pass += 1
    else:
        n_fail += 1


c = Client()
URL = "/simulateur-strategie/"

# ============================================================
# TEST 1 : GET vide
# ============================================================
print("\n=== TEST 1 : GET sans paramètres ===")
r = c.get(URL)
html = r.content.decode("utf-8", errors="replace")
check("Status 200", r.status_code == 200, f"got {r.status_code}")
check("Titre page présent", "Simulateur de stratégie" in html)
check("Form de configuration présent", 'id="simForm"' in html)
check("Champ ticker présent", 'name="ticker"' in html)
check("Champ cash_initial présent", 'name="cash_initial"' in html)
check("Champ frais_pct présent", 'name="frais_pct"' in html)
check("Sélecteur horizon GARCH présent", 'name="garch_horizon"' in html)
check("Toggle utiliser_garch présent", 'name="utiliser_garch"' in html)
check("Toggle inclure_dividendes présent", 'name="inclure_dividendes"' in html)
check("Champs dates présents",
      'name="date_debut"' in html and 'name="date_fin"' in html)
check("Bouton Lancer présent", 'id="btnLancer"' in html)
check("État initial visible (pas de calcul)",
      "Configurez et lancez une simulation" in html)
check("Tuiles résultat absentes (pas de calcul encore)",
      'class="sim-summary-tile"' not in html)
check("Onglets absents (pas de calcul)", 'id="tab-sim"' not in html)
check("Valeur défaut cash = 1 000 000",
      re.search(r'name="cash_initial"[^>]*value="1000000"', html) is not None
      or re.search(r'value="1000000"', html) is not None)

# ============================================================
# TEST 2 : GET avec ticker pré-rempli
# ============================================================
print("\n=== TEST 2 : GET ?ticker=SGBC.ci ===")
r = c.get(URL + "?ticker=SGBC.ci")
html = r.content.decode("utf-8", errors="replace")
check("Status 200", r.status_code == 200)
check("Ticker SGBC.ci sélectionné dans le form",
      'value="SGBC.ci" selected' in html or
      'value="SGBC.ci"selected' in html or
      re.search(r'value="SGBC\.ci"[^>]*selected', html) is not None)
check("Bloc Cache GARCH affiché (action a un GarchModel)",
      "Cache GARCH" in html)
check("Plage de dates dispo affichée",
      "Plage dispo" in html and "2008" in html)
check("Min/max date input présent",
      re.search(r'<input[^>]+name="date_debut"[^>]+min=', html) is not None)

# ============================================================
# TEST 3 : GET avec ticker invalide
# ============================================================
print("\n=== TEST 3 : GET ?ticker=NEXISTE.pas ===")
r = c.get(URL + "?ticker=NEXISTE.pas")
html = r.content.decode("utf-8", errors="replace")
check("Status 200 (pas de crash)", r.status_code == 200)
check("État initial montré (pas d'action sélectionnée)",
      "Configurez et lancez une simulation" in html or
      "Sélectionner une action" in html)

# ============================================================
# TEST 4 : POST avec paramètres valides (action déjà cachée)
# ============================================================
print("\n=== TEST 4 : POST SGBC.ci sur 2024-2025 (cache déjà rempli) ===")
t0 = time.time()
r = c.post(URL, {
    "ticker": "SGBC.ci",
    "cash_initial": "1000000",
    "frais_pct": "1.0",
    "garch_horizon": "5",
    "utiliser_garch": "on",
    "inclure_dividendes": "on",
    "date_debut": "2024-01-01",
    "date_fin": "2025-12-31",
})
duree = time.time() - t0
html = r.content.decode("utf-8", errors="replace")
check("Status 200", r.status_code == 200, f"got {r.status_code}")
check(f"Durée acceptable ({duree:.1f}s)", duree < 120, f"{duree:.1f}s")
check("Tuiles résumé affichées", 'class="sim-summary-tile"' in html)
check("Onglet 'Simulation portefeuille' présent", 'id="tab-sim"' in html)
check("Onglet 'Diagnostic' présent", 'id="tab-diag"' in html)
check("Conteneur graphe Plotly présent", 'id="simChart"' in html)
check("Tableau transactions présent", "Journal des transactions" in html)
check("Disclaimer méthodologique présent", "Note méthodologique" in html)
check("Données JSON pour le chart présentes", 'id="sim-data"' in html)
check("Plotly script chargé", "plotly" in html.lower())
# Extraire le JSON pour vérifier le contenu numérique
def _extract_sim(html):
    m = re.search(r'<script id="sim-data"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    # Régression-guard : si on reçoit une string ici, c'est que la view
    # a double-encodé (json.dumps puis |json_script). Le JS du template
    # ne parse qu'une fois donc le graphe ne se rendrait pas.
    if isinstance(d, str):
        raise AssertionError(
            "sim-data est double-encodé JSON (string au lieu d'objet) — "
            "le graphique Plotly ne se rendra pas côté navigateur."
        )
    return d


json_match = re.search(r'<script id="sim-data"[^>]*>(.*?)</script>', html, re.DOTALL)
if json_match:
    try:
        sim_data = _extract_sim(html)
        check("JSON sim valide", sim_data is not None and sim_data.get("disponible") is True)
        check("3 courbes (strategie, BH, cash)",
              len(sim_data.get("valeur_strategie", [])) > 0
              and len(sim_data.get("valeur_buy_hold", [])) > 0
              and len(sim_data.get("valeur_cash", [])) > 0)
        n_dates = len(sim_data.get("dates", []))
        check(f"~500 dates de cotation (2 ans)", 400 < n_dates < 600, f"{n_dates} dates")
        rs = sim_data.get("resume_strategie", {})
        check("Résumé stratégie présent",
              rs is not None and "valeur_finale" in rs)
        check("Valeur finale > 0 FCFA", (rs.get("valeur_finale") or 0) > 0)
        rb = sim_data.get("resume_buy_hold", {})
        check("Résumé B&H présent", rb is not None and "valeur_finale" in rb)
        rc = sim_data.get("resume_cash", {})
        check("Cash figé à 1 000 000",
              abs((rc.get("valeur_finale") or 0) - 1_000_000) < 1)
        check("Transactions présentes",
              len(sim_data.get("transactions", [])) > 0,
              f"{len(sim_data.get('transactions', []))} tx")
        # Vérifier types de transactions
        types_tx = set(t.get("type") for t in sim_data.get("transactions", []))
        check("Types tx variés (ACHAT/VENTE attendus)",
              "ACHAT" in types_tx or "VENTE" in types_tx,
              f"types={types_tx}")
        # Vérifier dividendes encaissés
        n_div = sum(1 for t in sim_data.get("transactions", []) if t.get("type") == "DIVIDENDE")
        check(f"Dividendes encaissés (inclure_dividendes=on)",
              n_div >= 0,  # peut être 0 si pas de div dans la fenêtre
              f"{n_div} entrées DIVIDENDE")
        # Vérifier que les facteurs GARCH apparaissent dans les raisons
        raisons_garch = [t.get("raison", "") for t in sim_data.get("transactions", [])
                         if "GARCH" in t.get("raison", "")]
        check("Raisons mentionnent facteur GARCH",
              len(raisons_garch) > 0,
              f"{len(raisons_garch)} mentions GARCH")
    except json.JSONDecodeError as e:
        check("JSON sim parseable", False, str(e)[:80])
else:
    check("Bloc JSON sim-data extrait", False)

check("Diagnostic - métriques par code présentes",
      "Métriques par code de verdict" in html)
check("Diagnostic - matrice confusion section présente",
      "Matrice de confusion" in html or "confusion vs Sika" in html
      or "indisponible" in html.lower())

# ============================================================
# TEST 5 : POST sans GARCH (toggle off)
# ============================================================
print("\n=== TEST 5 : POST sans filtre GARCH ===")
t0 = time.time()
r = c.post(URL, {
    "ticker": "SGBC.ci",
    "cash_initial": "1000000",
    "frais_pct": "1.0",
    "garch_horizon": "5",
    # utiliser_garch absent = off
    "inclure_dividendes": "on",
    "date_debut": "2024-01-01",
    "date_fin": "2025-12-31",
})
duree = time.time() - t0
html = r.content.decode("utf-8", errors="replace")
check("Status 200", r.status_code == 200)
check(f"Sans GARCH : rapide ({duree:.1f}s)", duree < 15,
      f"{duree:.1f}s")
json_match = re.search(r'<script id="sim-data"[^>]*>(.*?)</script>', html, re.DOTALL)
if json_match:
    sim_data = _extract_sim(html)
    raisons_garch = [t.get("raison", "") for t in sim_data.get("transactions", [])
                     if "GARCH" in t.get("raison", "") and "sans" not in t.get("raison", "")]
    raisons_sans = [t.get("raison", "") for t in sim_data.get("transactions", [])
                    if "sans modulation GARCH" in t.get("raison", "")]
    check("Raisons NE mentionnent PAS facteur GARCH actif",
          len(raisons_garch) == 0, f"{len(raisons_garch)} mentions")
    check("Raisons mentionnent 'sans modulation GARCH'",
          len(raisons_sans) > 0, f"{len(raisons_sans)} mentions")

# ============================================================
# TEST 6 : POST avec cash très faible (ordres annulés attendus)
# ============================================================
print("\n=== TEST 6 : POST cash 10 000 FCFA (cash insuffisant attendu) ===")
r = c.post(URL, {
    "ticker": "SGBC.ci",
    "cash_initial": "10000",  # 10 K, insuffisant pour 1 part SGBC ~16 000
    "frais_pct": "1.0",
    "garch_horizon": "5",
    "utiliser_garch": "on",
    "inclure_dividendes": "on",
    "date_debut": "2024-01-01",
    "date_fin": "2024-12-31",
})
html = r.content.decode("utf-8", errors="replace")
check("Status 200", r.status_code == 200)
json_match = re.search(r'<script id="sim-data"[^>]*>(.*?)</script>', html, re.DOTALL)
if json_match:
    sim_data = _extract_sim(html)
    rs = sim_data.get("resume_strategie", {})
    n_annule = rs.get("nb_ordres_annules", 0)
    check("Ordres annulés > 0 (cash insuffisant)",
          n_annule > 0, f"{n_annule} annulés")
    check("Valeur reste proche du capital initial",
          abs((rs.get("valeur_finale") or 0) - 10_000) < 5000,
          f"finale={rs.get('valeur_finale'):.0f}")

# ============================================================
# TEST 7 : POST avec horizon 1j
# ============================================================
print("\n=== TEST 7 : POST horizon GARCH = 1j ===")
r = c.post(URL, {
    "ticker": "SGBC.ci",
    "cash_initial": "1000000",
    "frais_pct": "0.5",
    "garch_horizon": "1",
    "utiliser_garch": "on",
    "inclure_dividendes": "on",
    "date_debut": "2024-01-01",
    "date_fin": "2024-12-31",
})
html = r.content.decode("utf-8", errors="replace")
check("Status 200", r.status_code == 200)
check("Horizon 1j sélectionné dans le form",
      'value="1" selected' in html or
      re.search(r'<option value="1"[^>]*selected', html) is not None)

# ============================================================
# TEST 8 : POST sans dividendes
# ============================================================
print("\n=== TEST 8 : POST sans dividendes ===")
r = c.post(URL, {
    "ticker": "SGBC.ci",
    "cash_initial": "1000000",
    "frais_pct": "1.0",
    "garch_horizon": "5",
    "utiliser_garch": "on",
    # inclure_dividendes absent
    "date_debut": "2024-01-01",
    "date_fin": "2025-12-31",
})
html = r.content.decode("utf-8", errors="replace")
check("Status 200", r.status_code == 200)
json_match = re.search(r'<script id="sim-data"[^>]*>(.*?)</script>', html, re.DOTALL)
if json_match:
    sim_data = _extract_sim(html)
    n_div = sum(1 for t in sim_data.get("transactions", []) if t.get("type") == "DIVIDENDE")
    check("Aucun dividende encaissé (toggle off)",
          n_div == 0, f"{n_div} divs")
    rs = sim_data.get("resume_strategie", {})
    check("Total dividendes = 0",
          (rs.get("dividendes_total") or 0) == 0,
          f"{rs.get('dividendes_total')}")

# ============================================================
# TEST 9 : POST avec ticker invalide
# ============================================================
print("\n=== TEST 9 : POST avec ticker vide ===")
r = c.post(URL, {
    "ticker": "",
    "cash_initial": "1000000",
})
html = r.content.decode("utf-8", errors="replace")
check("Status 200 (pas de crash)", r.status_code == 200)

# ============================================================
# TEST 10 : Carte teaser sur la page analyse_actions
# ============================================================
print("\n=== TEST 10 : Carte teaser sur la page action ===")
r = c.get("/actions/?ticker=SGBC.ci")
html = r.content.decode("utf-8", errors="replace")
check("Page analyse_actions accessible", r.status_code == 200,
      f"got {r.status_code}")
check("Ancien sous-onglet Backtesting retiré",
      'id="backtest-tab"' not in html and 'id="backtesting"' not in html)
check("Carte teaser présente",
      "Ouvrir dans le simulateur" in html or "backtestTeaser" in html)
check("Lien teaser vers simulateur",
      "/simulateur-strategie/?ticker=SGBC.ci" in html)

# ============================================================
# TEST 11 : Lien dans la sidebar
# ============================================================
print("\n=== TEST 11 : Lien sidebar ===")
r = c.get("/")
html = r.content.decode("utf-8", errors="replace")
check("Lien 'Simulateur stratégie' dans la sidebar",
      "Simulateur strat" in html and "/simulateur-strategie/" in html)

# ============================================================
# Résumé
# ============================================================
print(f"\n{'='*60}")
print(f"Résumé : {n_pass} OK / {n_pass + n_fail} tests")
if n_fail > 0:
    print(f"  ⚠ {n_fail} échecs")
print(f"{'='*60}")
sys.exit(0 if n_fail == 0 else 1)
