"""
LOOK UP BRVM - Lanceur de l'application
Double-cliquer sur ce fichier ou l'exécutable pour démarrer l'application.
Le navigateur s'ouvre automatiquement sur le dashboard.
"""
import os
import sys
import threading
import webbrowser
import time
import socket

# Configurer l'environnement Django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "lookupbrvm.settings")

# Quand packagé avec PyInstaller, ajuster les chemins
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
    os.chdir(BASE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    os.chdir(BASE_DIR)

# Ajouter au path
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def find_free_port(start=8000, end=8100):
    """Trouve un port disponible."""
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 8000


def open_browser(port):
    """Ouvre le navigateur après un court délai."""
    time.sleep(2)
    webbrowser.open(f"http://127.0.0.1:{port}")


def main():
    import django
    django.setup()

    # Appliquer les migrations automatiquement
    from django.core.management import call_command
    print("Vérification de la base de données...")
    call_command("migrate", "--run-syncdb", verbosity=0)

    # Importer les données si la base est vide
    from dashboard.models import HistoriqueAction
    if HistoriqueAction.objects.count() == 0:
        print("Import des données en cours...")
        call_command("import_data")
        print("Import terminé !")

    # Trouver un port libre
    port = find_free_port()
    print(f"\n{'='*50}")
    print(f"  LOOK UP BRVM - Dashboard")
    print(f"  http://127.0.0.1:{port}")
    print(f"  Appuyez sur Ctrl+C pour arrêter")
    print(f"{'='*50}\n")

    # Ouvrir le navigateur automatiquement
    threading.Thread(target=open_browser, args=(port,), daemon=True).start()

    # Lancer le serveur Django
    from django.core.management import execute_from_command_line
    execute_from_command_line(["manage.py", "runserver", f"127.0.0.1:{port}", "--noreload"])


if __name__ == "__main__":
    main()
