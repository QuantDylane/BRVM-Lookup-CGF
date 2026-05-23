# Setup GARCH complet : modèles courants + cache historique
# Usage :
#   .\scripts\run_garch_setup.ps1                      # toutes actions, depuis 2020
#   .\scripts\run_garch_setup.ps1 -Ticker ABJC.ci      # une seule action
#   .\scripts\run_garch_setup.ps1 -Since 2018-01       # depuis cette date
#   .\scripts\run_garch_setup.ps1 -SkipCache           # juste train_garch, pas le warmup

param(
    [string]$Ticker = "",
    [string]$Since  = "2025-01",
    [switch]$SkipCache,
    [switch]$SkipTrain
)

$ErrorActionPreference = "Stop"
$StartTime = Get-Date

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Setup GARCH pour le simulateur de stratégie" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Cible          : $(if ($Ticker) { $Ticker } else { 'TOUTES LES ACTIONS' })"
Write-Host "  Cache depuis   : $Since"
Write-Host "  Skip train     : $SkipTrain"
Write-Host "  Skip warmup    : $SkipCache"
Write-Host "  Démarré à      : $($StartTime.ToString('HH:mm:ss'))"
Write-Host ""

# Aller à la racine du projet (le script est dans scripts/)
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

# ============================================================
# ÉTAPE 1 : train_garch (modèle courant, ~5 min pour 48 actions)
# ============================================================
if (-not $SkipTrain) {
    Write-Host "▶ ÉTAPE 1/2 — Entraînement des modèles GARCH courants" -ForegroundColor Yellow
    Write-Host "  (table GarchModel, ré-estimation mensuelle de prod)"
    Write-Host ""

    $T1 = Get-Date
    if ($Ticker) {
        python manage.py train_garch --ticker $Ticker
    } else {
        python manage.py train_garch
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "✗ train_garch a échoué (exit $LASTEXITCODE)" -ForegroundColor Red
        exit 1
    }
    $T1End = Get-Date
    Write-Host ""
    Write-Host "✔ Étape 1 terminée en $([int](($T1End - $T1).TotalSeconds))s" -ForegroundColor Green
    Write-Host ""
}

# ============================================================
# ÉTAPE 2 : warmup_garch_cache (cache historique, ~30 min pour 48 actions)
# ============================================================
if (-not $SkipCache) {
    Write-Host "▶ ÉTAPE 2/2 — Warmup du cache GARCH historique" -ForegroundColor Yellow
    Write-Host "  (table GarchFitHistorique, fit par fin de mois sans look-ahead)"
    Write-Host "  Cette étape est LONGUE. Tu peux suivre la progression en direct."
    Write-Host ""

    $T2 = Get-Date
    if ($Ticker) {
        python manage.py warmup_garch_cache --ticker $Ticker --since $Since
    } else {
        python manage.py warmup_garch_cache --since $Since
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "✗ warmup_garch_cache a échoué (exit $LASTEXITCODE)" -ForegroundColor Red
        exit 1
    }
    $T2End = Get-Date
    Write-Host ""
    Write-Host "✔ Étape 2 terminée en $([int](($T2End - $T2).TotalSeconds))s" -ForegroundColor Green
    Write-Host ""
}

# ============================================================
# RÉCAP FINAL
# ============================================================
$Total = (Get-Date) - $StartTime
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  TERMINÉ — durée totale : $([int]$Total.TotalMinutes) min $([int]$Total.Seconds) s" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "▶ Vérification rapide de l'état du cache :"
Write-Host ""

python manage.py shell -c @"
from dashboard.models import Action, GarchModel, GarchFitHistorique
n_actions = Action.objects.count()
n_gm = GarchModel.objects.exclude(model_type__in=['INSUFFISANT','FAILED']).count()
n_fits = GarchFitHistorique.objects.exclude(model_type__in=['INSUFFISANT','FAILED']).count()
print(f'  Actions totales       : {n_actions}')
print(f'  GarchModel ajustés    : {n_gm}')
print(f'  Fits historiques OK   : {n_fits}')
print(f'  Ratio couverture      : {100.0 * n_gm / n_actions:.1f}%')
"@

Write-Host ""
Write-Host "▶ Lance maintenant le simulateur :" -ForegroundColor Green
Write-Host "  python manage.py runserver" -ForegroundColor White
Write-Host "  → http://127.0.0.1:8000/simulateur-strategie/" -ForegroundColor White
Write-Host ""
