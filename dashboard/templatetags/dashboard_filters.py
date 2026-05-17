from django import template
import locale

register = template.Library()


PAYS_LABELS = {
    "bf": "Burkina Faso",
    "bj": "Bénin",
    "ci": "Côte d'Ivoire",
    "ml": "Mali",
    "ne": "Niger",
    "sn": "Sénégal",
    "tg": "Togo",
}


@register.filter
def pays_label(value):
    """Convertit un code pays (ci, sn, ...) en nom complet."""
    if not value:
        return "-"
    return PAYS_LABELS.get(str(value).strip().lower(), str(value).upper())


@register.filter
def format_number(value, decimals=0):
    """Formate un nombre avec séparateur de milliers (espace) et virgule décimale."""
    if value is None:
        return "-"
    try:
        value = float(value)
        if decimals == 0:
            formatted = f"{value:,.0f}"
        else:
            formatted = f"{value:,.{int(decimals)}f}"
        # Remplacer . par , et , par espace (format français)
        formatted = formatted.replace(",", " ").replace(".", ",")
        return formatted
    except (ValueError, TypeError):
        return str(value)


@register.filter
def format_pct(value, decimals=2):
    """Formate un pourcentage."""
    if value is None:
        return "-"
    try:
        value = float(value)
        formatted = f"{value:+.{int(decimals)}f}%".replace(".", ",")
        return formatted
    except (ValueError, TypeError):
        return str(value)


@register.filter
def color_variation(value):
    """Retourne une classe CSS en fonction du signe de la variation."""
    if value is None:
        return "text-muted"
    try:
        value = float(value)
        if value > 0:
            return "text-success"
        elif value < 0:
            return "text-danger"
        return "text-muted"
    except (ValueError, TypeError):
        return "text-muted"


@register.filter
def abs_value(value):
    """Valeur absolue."""
    try:
        return abs(float(value))
    except (ValueError, TypeError):
        return value


@register.filter
def get_item(dictionary, key):
    """Accès par clé dans un dictionnaire."""
    if isinstance(dictionary, dict):
        return dictionary.get(key)
    return None


@register.filter
def get_var(indicateurs, period):
    """Récupère la variation pour une période donnée."""
    if isinstance(indicateurs, dict):
        return indicateurs.get(f"var_{period}")
    return None


@register.simple_tag
def periods_list():
    """Retourne la liste des périodes."""
    return [
        ("1j", "1 Jour"),
        ("1s", "1 Semaine"),
        ("1m", "1 Mois"),
        ("3m", "3 Mois"),
        ("6m", "6 Mois"),
        ("1a", "1 An"),
        ("ytd", "YTD"),
    ]


@register.filter
def mul(value, arg):
    """Multiplie value par arg (utile pour convertir une fraction en pourcentage)."""
    try:
        return float(value) * float(arg)
    except (TypeError, ValueError):
        return ""
