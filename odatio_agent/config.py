"""
Configuration de l'agent : profils d'environnement (profils.toml) et secrets (.env).

Un profil décrit une application Odatio à interroger (ton poste, une recette client…) :
- ses sources : dossiers locaux et/ou dépôts Git (clonés dans .cache/sources) ;
- ses logs : dossiers locaux ou partagés (lus avec Read/Grep) et/ou un filtre groundcover ;
- sa base : connexion en lecture seule (le mot de passe reste dans le .env, désigné par mot_de_passe_env) ;
- ses fichiers générés : dossier où l'agent écrit les fichiers d'interface demandés (.cache/fichiers/<profil>
  par défaut), et le choix d'y garder ou non les données réelles.

Les secrets sont lus une fois, gardés en mémoire, puis retirés de os.environ : le processus Claude Code
lancé par le SDK n'hérite ainsi d'aucun mot de passe ni jeton.
"""
import os
import re
import shutil
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key

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
class Application:
    """L'application web du profil, où l'agent peut naviguer pour prendre des captures d'écran."""
    url: str = ""                 # ex. http://localhost:8888/
    utilisateur: str = ""         # compte applicatif (un compte de test, aux droits réduits)
    mot_de_passe_env: str = ""
    actions: bool = False         # False : navigation seule (les clics qui valident ou enregistrent sont refusés)


@dataclass
class Profil:
    nom: str
    description: str = ""
    sources: list = field(default_factory=list)
    dossiers_logs: list = field(default_factory=list)
    groundcover_filtre: str = ""
    base: Base | None = None
    fichiers_interdits: list = field(default_factory=list)
    dossier_fichiers: str = ""     # fichiers d'interface générés ; vide = .cache/fichiers/<profil>
    donnees_reelles: bool = False  # garder noms, adresses… dans les fichiers générés (base de test seulement)
    application: Application | None = None


@dataclass
class Reglages:
    modele: str = ""          # alias (sonnet, opus, haiku) ou identifiant complet ; vide = défaut de Claude Code
    effort: str = ""          # low, medium, high, xhigh, max ; vide = défaut
    max_tours: int = 40       # outils appelés au plus par question
    budget_usd: float = 0.0   # plafond de coût par question (0 = aucun)
    requetes_base: int = 15   # requêtes base au plus par question
    requetes_base_generation: int = 40  # requêtes base au plus pour une question qui génère un fichier


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


def enregistrer_secrets(valeurs: dict) -> None:
    """Écrit des valeurs dans le .env (une valeur vide supprime la ligne) et les met à jour en mémoire."""
    if not FICHIER_SECRETS.exists():
        FICHIER_SECRETS.touch()
    for nom, valeur in valeurs.items():
        valeur = (valeur or "").strip()
        if valeur:
            set_key(str(FICHIER_SECRETS), nom, valeur, quote_mode="never")
            _SECRETS[nom] = valeur
        else:
            if nom in dotenv_values(FICHIER_SECRETS):
                unset_key(str(FICHIER_SECRETS), nom)
            _SECRETS.pop(nom, None)


def nom_mot_de_passe_application(profil: str) -> str:
    """Nom de la variable du .env qui porte le mot de passe du compte applicatif d'un profil."""
    return "APP_PASSWORD_" + re.sub(r"[^A-Z0-9]+", "_", profil.upper()).strip("_")


def nom_mot_de_passe(profil: str) -> str:
    """Nom de la variable du .env qui porte le mot de passe base d'un profil (ex. DB_PASSWORD_RECETTE_X)."""
    return "DB_PASSWORD_" + re.sub(r"[^A-Z0-9]+", "_", profil.upper()).strip("_")


# ---------------------------------------------------------------------------
# Compte Claude et Jira (réglages du .env)
# ---------------------------------------------------------------------------

def compte_claude() -> dict:
    """{"mode": "compte" ou "cle", "dossier": CLAUDE_CONFIG_DIR, "cle": ANTHROPIC_API_KEY}. Sans choix
    enregistré, la clé est utilisée si elle est renseignée (comportement des premières versions)."""
    cle = secret("ANTHROPIC_API_KEY")
    mode = secret("CLAUDE_AUTH") or ("cle" if cle else "compte")
    return {"mode": mode, "dossier": secret("CLAUDE_CONFIG_DIR"), "cle": cle}


def env_claude() -> dict:
    """Variables à passer au processus Claude Code selon le compte choisi."""
    compte = compte_claude()
    if compte["mode"] == "cle" and compte["cle"]:
        return {"ANTHROPIC_API_KEY": compte["cle"]}
    return {"CLAUDE_CONFIG_DIR": os.path.expanduser(compte["dossier"])} if compte["dossier"] else {}


def jira_configure() -> bool:
    return all(secret(n) for n in ("JIRA_DOMAIN", "JIRA_EMAIL", "JIRA_API_TOKEN"))


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
    if not FICHIER_PROFILS.exists():
        return Reglages()
    brut = _lire_toml().get("agent", {})
    return Reglages(modele=str(brut.get("modele", "")).strip(), effort=str(brut.get("effort", "")).strip(),
                    max_tours=int(brut.get("max_tours", 40)), budget_usd=float(brut.get("budget_usd", 0)),
                    requetes_base=int(brut.get("requetes_base", 15)),
                    requetes_base_generation=int(brut.get("requetes_base_generation", 40)))


def noms_profils() -> list:
    if not FICHIER_PROFILS.exists():
        return []
    return list(_lire_toml().get("profils", {}))


def charger_profil(nom: str, interdits_par_defaut: bool = True) -> Profil:
    profils = _lire_toml().get("profils", {})
    if nom not in profils:
        raise ConfigError(f"Profil « {nom} » inconnu. Profils disponibles : {', '.join(profils) or 'aucun'}.")
    return _profil(nom, profils[nom], interdits_par_defaut)


def tous_les_profils() -> list:
    """Profils tels qu'écrits (sans les fichiers interdits par défaut), pour l'écran de configuration."""
    if not FICHIER_PROFILS.exists():
        return []
    return [_profil(nom, brut, False) for nom, brut in _lire_toml().get("profils", {}).items()]


def _profil(nom: str, brut: dict, interdits_par_defaut: bool) -> Profil:

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

    logs, fichiers = brut.get("logs", {}), brut.get("fichiers", {})
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

    application = None
    if str(brut.get("application", {}).get("url", "")).strip():
        a = brut["application"]
        application = Application(url=str(a.get("url", "")).strip(), utilisateur=str(a.get("utilisateur", "")).strip(),
                                  mot_de_passe_env=str(a.get("mot_de_passe_env", "")).strip(),
                                  actions=bool(a.get("actions", False)))

    return Profil(nom=nom, description=str(brut.get("description", "")).strip(), sources=sources,
                  application=application,
                  dossiers_logs=[str(d) for d in logs.get("dossiers", [])],
                  groundcover_filtre=str(logs.get("groundcover_filtre", "")).strip(), base=base,
                  fichiers_interdits=(INTERDITS_PAR_DEFAUT if interdits_par_defaut else [])
                  + [str(m) for m in brut.get("fichiers_interdits", [])],
                  dossier_fichiers=str(fichiers.get("dossier", "")).strip(),
                  donnees_reelles=bool(fichiers.get("donnees_reelles", False)))


def dossier_fichiers(profil: Profil) -> Path:
    """Dossier où l'agent écrit les fichiers d'interface de ce profil (chemin absolu, pas forcément créé)."""
    if profil.dossier_fichiers:
        return Path(profil.dossier_fichiers).expanduser().resolve()
    return (CACHE / "fichiers" / profil.nom).resolve()


def _dans(chemin: Path, dossier: Path) -> bool:
    """chemin est-il dossier ou un de ses sous-dossiers ? (insensible à la casse sous Windows)"""
    a, b = os.path.normcase(str(chemin)), os.path.normcase(str(dossier))
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep)


def erreur_dossier_fichiers(profil: Profil) -> str:
    """Message si le dossier des fichiers générés est mal placé, sinon '' : il ne doit être ni sous une source
    ou un dossier de logs (l'application pourrait le surveiller, l'agent écrirait dans le code), ni les
    contenir, ni contenir le dossier de l'agent (.env, profils.toml)."""
    sortie = dossier_fichiers(profil)
    interdits = [(f"la source « {s.nom} »", Path(s.chemin).expanduser().resolve() if s.chemin
                  else (CACHE / "sources" / profil.nom / s.nom).resolve()) for s in profil.sources]
    interdits += [(f"le dossier de logs {d}", Path(d).expanduser().resolve()) for d in profil.dossiers_logs]
    for libelle, dossier in interdits:
        if _dans(sortie, dossier) or _dans(dossier, sortie):
            return (f"le dossier des fichiers générés ({sortie}) ne doit pas être dans {libelle} ni la contenir : "
                    f"choisis un dossier à part, que l'application ne surveille pas.")
    if _dans(RACINE, sortie) or (_dans(sortie, RACINE) and not _dans(sortie, CACHE / "fichiers")):
        return (f"le dossier des fichiers générés ({sortie}) ne doit pas être dans le dossier de l'agent "
                f"(sauf .cache/fichiers) ni le contenir.")
    return ""


NOM_PROFIL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$")


def enregistrer(reglages_agent: Reglages, profils: list) -> None:
    """Réécrit profils.toml (l'ancienne version est gardée en profils.toml.bak). Les commentaires du fichier
    ne sont pas conservés : l'écran de configuration devient la référence."""
    import tomli_w

    for p in profils:
        if not NOM_PROFIL.match(p.nom):
            raise ConfigError(f"Nom de profil invalide : « {p.nom} » (lettres, chiffres, - et _ ; sans espace).")
        if not p.sources:
            raise ConfigError(f"Profil « {p.nom} » : au moins une source est nécessaire.")
    if len({p.nom for p in profils}) != len(profils):
        raise ConfigError("Deux profils portent le même nom.")
    for p in profils:
        erreur = erreur_dossier_fichiers(p)
        if erreur:
            raise ConfigError(f"Profil « {p.nom} » : {erreur}")

    donnees = {"agent": {k: v for k, v in asdict(reglages_agent).items()}, "profils": {}}
    for p in profils:
        brut = {"description": p.description} if p.description else {}
        if p.fichiers_interdits:
            brut["fichiers_interdits"] = list(p.fichiers_interdits)
        brut["sources"] = [{k: v for k, v in asdict(s).items() if v} for s in p.sources]
        logs = {}
        if p.dossiers_logs:
            logs["dossiers"] = list(p.dossiers_logs)
        if p.groundcover_filtre:
            logs["groundcover_filtre"] = p.groundcover_filtre
        if logs:
            brut["logs"] = logs
        if p.base:
            brut["base"] = {k: v for k, v in asdict(p.base).items() if v}
        if p.dossier_fichiers or p.donnees_reelles:
            brut["fichiers"] = {"dossier": p.dossier_fichiers, "donnees_reelles": p.donnees_reelles}
        if p.application and p.application.url:
            brut["application"] = {k: v for k, v in asdict(p.application).items() if v}
        donnees["profils"][p.nom] = brut

    if FICHIER_PROFILS.exists():
        shutil.copyfile(FICHIER_PROFILS, FICHIER_PROFILS.with_suffix(".toml.bak"))
    with open(FICHIER_PROFILS, "wb") as f:
        tomli_w.dump(donnees, f)
