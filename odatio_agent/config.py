"""
Configuration de l'agent : profils d'environnement (profils.toml) et secrets (.env).

Un profil décrit une application Odatio à interroger (ton poste, une recette client…) :
- ses sources : dossiers locaux et/ou dépôts Git (clonés dans .cache/sources) ;
- ses logs : dossiers locaux ou partagés (lus avec Read/Grep) et/ou un filtre groundcover ;
- sa base : connexion en lecture seule (le mot de passe reste dans le .env, désigné par mot_de_passe_env).

Les secrets sont lus une fois, gardés en mémoire, puis retirés de os.environ : le processus Claude Code
lancé par le SDK n'hérite ainsi d'aucun mot de passe ni jeton.
"""
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

RACINE = Path(__file__).resolve().parent.parent
FICHIER_PROFILS = RACINE / "profils.toml"
FICHIER_SECRETS = RACINE / ".env"
CACHE = RACINE / ".cache"

# Fichiers que l'agent ne lit jamais, même dans les sources (complétés par fichiers_interdits du profil)
INTERDITS_PAR_DEFAUT = [".env", ".env.*", "*.pem", "*.key", "*.jks", "*.p12", "*.pfx", "id_rsa*", "*.kdbx",
                        "credentials*", "secrets*"]

_NOM_SECRET = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY)$", re.I)
_SECRETS: dict = {}


class ConfigError(Exception):
    """Configuration absente ou invalide, avec un message lisible."""


@dataclass
class Source:
    nom: str
    chemin: str = ""     # dossier local (ou partage réseau)
    depot: str = ""      # URL Git, clonée dans .cache/sources/<profil>/<nom>
    branche: str = ""    # branche à suivre pour un dépôt (défaut : branche par défaut du dépôt)


@dataclass
class Base:
    type: str
    hote: str = ""
    port: str = ""
    nom: str = ""
    utilisateur: str = ""
    schema: str = ""
    mot_de_passe_env: str = ""


@dataclass
class Profil:
    nom: str
    description: str = ""
    sources: list = field(default_factory=list)
    dossiers_logs: list = field(default_factory=list)
    groundcover_filtre: str = ""
    base: Base | None = None
    fichiers_interdits: list = field(default_factory=list)


@dataclass
class Reglages:
    modele: str = ""          # alias (sonnet, opus, haiku) ou identifiant complet ; vide = défaut de Claude Code
    effort: str = ""          # low, medium, high, xhigh, max ; vide = défaut
    max_tours: int = 40       # outils appelés au plus par question
    budget_usd: float = 0.0   # plafond de coût par question (0 = aucun)
    requetes_base: int = 15   # requêtes base au plus par question


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

def charger_secrets() -> None:
    """Lit le .env et les secrets déjà présents dans l'environnement, puis les retire de os.environ."""
    valeurs = {k: v for k, v in dotenv_values(FICHIER_SECRETS).items() if v is not None} \
        if FICHIER_SECRETS.exists() else {}
    for nom, valeur in os.environ.items():
        if _NOM_SECRET.search(nom) and nom not in valeurs:
            valeurs[nom] = valeur
    _SECRETS.update(valeurs)
    retirer_secrets_environnement()


def retirer_secrets_environnement() -> None:
    """Retire de os.environ les variables du .env et tout secret (rappelé après l'import de db_tools,
    qui charge le .env dans l'environnement)."""
    for nom in list(os.environ):
        if nom in _SECRETS or _NOM_SECRET.search(nom):
            os.environ.pop(nom, None)


def secret(nom: str) -> str:
    return (_SECRETS.get(nom) or "").strip() if nom else ""


# ---------------------------------------------------------------------------
# Profils
# ---------------------------------------------------------------------------

def _lire_toml() -> dict:
    if not FICHIER_PROFILS.exists():
        raise ConfigError(f"Fichier {FICHIER_PROFILS.name} introuvable : copie profils.example.toml en "
                          f"profils.toml et complète-le.")
    try:
        with open(FICHIER_PROFILS, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{FICHIER_PROFILS.name} illisible : {e}")


def reglages() -> Reglages:
    brut = _lire_toml().get("agent", {})
    return Reglages(modele=str(brut.get("modele", "")).strip(), effort=str(brut.get("effort", "")).strip(),
                    max_tours=int(brut.get("max_tours", 40)), budget_usd=float(brut.get("budget_usd", 0)),
                    requetes_base=int(brut.get("requetes_base", 15)))


def noms_profils() -> list:
    return list(_lire_toml().get("profils", {}))


def charger_profil(nom: str) -> Profil:
    profils = _lire_toml().get("profils", {})
    if nom not in profils:
        raise ConfigError(f"Profil « {nom} » inconnu. Profils disponibles : {', '.join(profils) or 'aucun'}.")
    brut = profils[nom]

    sources = []
    for i, s in enumerate(brut.get("sources", []), start=1):
        chemin, depot = str(s.get("chemin", "")).strip(), str(s.get("depot", "")).strip()
        if bool(chemin) == bool(depot):
            raise ConfigError(f"Profil « {nom} », source n° {i} : renseigne soit « chemin », soit « depot ».")
        defaut = Path(chemin).name if chemin else depot.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        sources.append(Source(nom=str(s.get("nom", "")).strip() or defaut, chemin=chemin, depot=depot,
                              branche=str(s.get("branche", "")).strip()))
    if not sources:
        raise ConfigError(f"Profil « {nom} » : au moins une source (chemin ou depot) est nécessaire.")

    logs = brut.get("logs", {})
    base = None
    if "base" in brut:
        b = brut["base"]
        base = Base(type=str(b.get("type", "")).strip().lower(), hote=str(b.get("hote", "")).strip(),
                    port=str(b.get("port", "")).strip(), nom=str(b.get("nom", "")).strip(),
                    utilisateur=str(b.get("utilisateur", "")).strip(), schema=str(b.get("schema", "")).strip(),
                    mot_de_passe_env=str(b.get("mot_de_passe_env", "")).strip())
        if not base.type:
            raise ConfigError(f"Profil « {nom} », base : « type » est obligatoire (postgresql, oracle, "
                              f"sqlserver, mysql).")

    return Profil(nom=nom, description=str(brut.get("description", "")).strip(), sources=sources,
                  dossiers_logs=[str(d) for d in logs.get("dossiers", [])],
                  groundcover_filtre=str(logs.get("groundcover_filtre", "")).strip(), base=base,
                  fichiers_interdits=INTERDITS_PAR_DEFAUT + [str(m) for m in brut.get("fichiers_interdits", [])])
