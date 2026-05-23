from django.urls import path
from . import views, views_strategie, views_simulateur

app_name = "dashboard"

urlpatterns = [
    # Pages principales
    path("", views.accueil, name="accueil"),
    path("actions/", views.analyse_actions, name="analyse_actions"),
    path("indices/", views.analyse_indices, name="analyse_indices"),
    path("nouvelles/", views.analyse_nouvelles, name="analyse_nouvelles"),
    path("performances/", views.performances, name="performances"),
    path("risque/", views.risque_volatilite, name="risque_volatilite"),
    path("actualisation/", views.actualisation, name="actualisation"),
    path("export/", views.export_factsheet, name="export_factsheet"),
    path("portefeuille/", views.simulation_portefeuille, name="simulation_portefeuille"),
    path("simulateur-strategie/", views_simulateur.simulateur_strategie, name="simulateur_strategie"),

    # API endpoints pour HTMX / AJAX
    path("api/action-data/<str:ticker>/", views.api_action_data, name="api_action_data"),
    path("api/indice-data/<str:ticker>/", views.api_indice_data, name="api_indice_data"),
    path("api/market-fear-greed/", views.api_market_fear_greed, name="api_market_fear_greed"),
    path("api/market-regime/", views.api_market_regime, name="api_market_regime"),
    path("api/market-regime-all/", views.api_market_regime_all, name="api_market_regime_all"),
    path("api/market-summary/", views.api_market_summary, name="api_market_summary"),
    path("api/performers/", views.api_performers, name="api_performers"),
    path("api/correlation-matrix/", views.api_correlation_matrix, name="api_correlation_matrix"),
    path("api/performances-table/", views.api_performances_table, name="api_performances_table"),
    path("api/export-csv/", views.api_export_csv, name="api_export_csv"),
    path("api/run-scraper/", views.api_run_scraper, name="api_run_scraper"),
    path("api/scraping-status/", views.api_scraping_status, name="api_scraping_status"),
    path("api/news-search/", views.api_news_search, name="api_news_search"),

    # Factsheet & Commentaires IA
    path("api/factsheet/data/", views.api_factsheet_data, name="api_factsheet_data"),
    path("api/factsheet/generate-comments/", views.api_factsheet_generate_comments, name="api_factsheet_generate_comments"),
    path("api/factsheet/generate-pdf/", views.api_factsheet_generate_pdf, name="api_factsheet_generate_pdf"),
    path("api/factsheet/market-report-data/", views.api_market_report_data, name="api_market_report_data"),
    path("api/factsheet/market-report-pdf/", views.api_market_report_pdf, name="api_market_report_pdf"),
    path("api/factsheet/market-report-comment/", views.api_market_report_comment, name="api_market_report_comment"),
    path("api/config/save/", views.api_save_config, name="api_save_config"),
    path("api/comment/save/", views.api_save_comment, name="api_save_comment"),
    path("api/comment/<int:comment_id>/delete/", views.api_delete_comment, name="api_delete_comment"),
    path("api/comment/<int:comment_id>/toggle-favorite/", views.api_toggle_favorite_comment, name="api_toggle_favorite_comment"),
    
    # Indicateurs Techniques & Signaux IA
    path("api/calculate-indicators/", views.api_calculate_indicators, name="api_calculate_indicators"),
    path("api/talib-indicators/", views.api_talib_indicators, name="api_talib_indicators"),
    path("api/validate-claude-key/", views.api_validate_claude_key, name="api_validate_claude_key"),
    path("api/estimate-claude-cost/", views.api_estimate_claude_cost, name="api_estimate_claude_cost"),
    path("api/generate-trading-signal/", views.api_generate_trading_signal, name="api_generate_trading_signal"),
    path("api/save-signal/", views.api_save_trading_signal, name="api_save_trading_signal"),
    path("api/signal-history/<str:ticker>/", views.api_get_signal_history, name="api_get_signal_history"),
    path("api/signal/<int:signal_id>/toggle-favorite/", views.api_toggle_signal_favorite, name="api_toggle_signal_favorite"),
    path("api/signal/<int:signal_id>/delete/", views.api_delete_signal, name="api_delete_signal"),

    # Simulation Portefeuille
    path("api/portefeuille/creer/", views.api_portefeuille_creer, name="api_portefeuille_creer"),
    path("api/portefeuille/<int:pf_id>/supprimer/", views.api_portefeuille_supprimer, name="api_portefeuille_supprimer"),
    path("api/portefeuille/ajouter-ligne/", views.api_portefeuille_ajouter_ligne, name="api_portefeuille_ajouter_ligne"),
    path("api/portefeuille/ligne/<int:ligne_id>/supprimer/", views.api_portefeuille_supprimer_ligne, name="api_portefeuille_supprimer_ligne"),
    path("api/portefeuille/dates/<str:ticker>/", views.api_portefeuille_dates_disponibles, name="api_portefeuille_dates"),
    path("api/portefeuille/prix/<str:ticker>/<str:date>/", views.api_portefeuille_prix, name="api_portefeuille_prix"),
    path("api/portefeuille/vendre/", views.api_portefeuille_vendre, name="api_portefeuille_vendre"),
    path("api/portefeuille/comparer/", views.api_portefeuille_comparer, name="api_portefeuille_comparer"),
    path("api/strategies/list/", views.api_strategies_list, name="api_strategies_list"),
    path("api/strategies/<int:alloc_id>/detail/", views.api_strategie_detail, name="api_strategie_detail"),
    path("api/strategies/appliquer/", views.api_strategie_appliquer, name="api_strategie_appliquer"),
    path("api/strategies/backtest/", views.api_strategie_backtest, name="api_strategie_backtest"),

    # Indicateurs cachés & Changements de signaux
    path("api/indicateurs-cache/<str:ticker>/", views.api_indicateurs_cache, name="api_indicateurs_cache"),
    path("api/signal-changements/<str:ticker>/", views.api_signal_changements, name="api_signal_changements"),

    # Stratégie HMM — pages
    path("strategie-hmm/", views_strategie.strategie_dashboard, name="strategie_dashboard"),
    path("strategie-hmm/regime/", views_strategie.strategie_regime, name="strategie_regime"),
    path("strategie-hmm/historique/", views_strategie.strategie_historique, name="strategie_historique"),

    # Stratégie HMM — API
    path("api/strategie/regime-courant/", views_strategie.api_strategie_regime_courant, name="api_strategie_regime_courant"),
    path("api/strategie/allocation-courante/", views_strategie.api_strategie_allocation_courante, name="api_strategie_allocation_courante"),
    path("api/strategie/historique-regimes/", views_strategie.api_strategie_historique_regimes, name="api_strategie_historique_regimes"),
    path("api/strategie/declencher-reallocation/", views_strategie.api_strategie_declencher_reallocation, name="api_strategie_declencher_reallocation"),
    path("api/strategie/toggle-facteur/", views_strategie.api_strategie_toggle_facteur, name="api_strategie_toggle_facteur"),
]
