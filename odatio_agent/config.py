"""
Configuration de l'agent : profils d'environnement (profils.toml) et secrets (.env).

Un profil décrit une application Odatio à interroger (ton poste, une recette client…) :
- ses sources : dossiers locaux et/ou dépôts Git (clonés dans .cache/sources) ;
- ses logs : dossiers locaux ou partagés (lus avec Read/Grep), un filtre groundcover et/ou des dossiers sur un
  serveur lus par SSH en lecture seule (ex. la production, voir logs_ssh.py) ;
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
from urllib.parse import parse_qs, unquote

from dotenv import dotenv_values, set_key, unset_key

from .logs_ssh import LogsSsh, erreurs as erreurs_logs_ssh

RACINE = Path(__file__).resolve().parent.parent
FICHIER_PROFILS = RACINE / "profils.toml"
FICHIER_SECRETS = RACINE / ".env"
CACHE = RACINE / ".cache"

# Fichiers que l'agent ne lit jamais, même dans les sources (complétés par fichiers_interdits du profil)
INTERDITS_PAR_DEFAUT = [".env", ".env.*", "*.pem", "*.key", "*.jks", "*.p12", "*.pfx", "id_rsa*", "*.kdbx",
                        "credentials*", "secrets*"]

_NOM_SECRET = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY)$", re.I)
_SECRETS: dict = {}

# Configuration commune tenue par agent-manager-savoye. Quand le manager lance l'agent, il lui transmet en
# variables d'environnement les accès et les secrets qui lui reviennent (liste dans SAVOYE_VARIABLES) et le dossier
# de commun.toml (SAVOYE_CONFIG_DIR). Ces variables l'emportent sur le .env ; les applications de commun.toml
# deviennent des projets. Lancé seul, l'agent garde son .env et son profils.toml.
VARIABLE_DOSSIER_COMMUN = "SAVOYE_CONFIG_DIR"
VARIABLE_GEREES = "SAVOYE_VARIABLES"
_GEREES: dict = {}       # variables transmises par le manager, relevées une fois (os.environ est vidé ensuite)
_COMMUN: dict = {}       # {"dossier": Path} si l'agent a été lancé par le manager


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
    logs_ssh: LogsSsh | None = None   # logs d'un serveur lus par SSH, en lecture seule
    gere: bool = False                # application du référentiel commun du manager (non modifiable ici)


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

def relever_variables_du_manager() -> None:
    """Relève les variables transmises par le manager, AVANT que les secrets ne soient retirés de os.environ.

    Appelé à l'import de ce module : outils.py retire les secrets de l'environnement dès son import (après le
    load_dotenv de db_tools et jira_tools), avant le premier charger_secrets() de l'IHM."""
    if _GEREES or _COMMUN:
        return
    noms = [n.strip() for n in os.environ.get(VARIABLE_GEREES, "").split(",") if n.strip()]
    _GEREES.update({n: os.environ[n] for n in noms if os.environ.get(n)})
    if os.environ.get(VARIABLE_DOSSIER_COMMUN):
        _COMMUN["dossier"] = Path(os.environ[VARIABLE_DOSSIER_COMMUN])


def charger_secrets() -> None:
    """Lit le .env et les secrets déjà présents dans l'environnement, puis les retire de os.environ."""
    relever_variables_du_manager()
    valeurs = {k: v for k, v in dotenv_values(FICHIER_SECRETS).items() if v is not None} \
        if FICHIER_SECRETS.exists() else {}
    for nom, valeur in os.environ.items():
        if _NOM_SECRET.search(nom) and nom not in valeurs:
            valeurs[nom] = valeur
    valeurs.update(_GEREES)                    # le manager l'emporte sur le .env
    _SECRETS.update(valeurs)
    retirer_secrets_environnement()


def geree(nom: str) -> bool:
    """Vrai si la variable est transmise par le manager (elle ne se modifie alors que dans le manager)."""
    return nom in _GEREES


def gerees() -> list:
    return sorted(_GEREES)


def dossier_commun() -> Path | None:
    """Dossier de la configuration commune du manager, si l'agent a été lancé par lui."""
    return _COMMUN.get("dossier")


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
        if geree(nom):
            continue                           # géré par le manager : la valeur se change dans le manager
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


def nom_mot_de_passe_ssh(profil: str) -> str:
    """Nom de la variable du .env qui porte le mot de passe SSH des logs d'un profil."""
    return "SSH_PASSWORD_" + re.sub(r"[^A-Z0-9]+", "_", profil.upper()).strip("_")


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


def _identifiant(nom: str) -> str:
    """Nom comparable entre profils.toml et le référentiel commun : « Canson-prod » → « canson-prod »."""
    return re.sub(r"[^a-z0-9]+", "-", nom.lower()).strip("-")


def _suffixe(nom: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", nom.upper()).strip("_")


def applications_communes() -> dict:
    """Applications du référentiel commun du manager, au format d'un profil de profils.toml ({nom: brut})."""
    dossier = dossier_commun()
    fichier = dossier / "commun.toml" if dossier else None
    if not fichier or not fichier.exists():
        return {}
    try:
        donnees = tomllib.loads(fichier.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"Configuration commune du manager illisible ({fichier}) : {e}")
    resultat = {}
    for a in donnees.get("applications", []):
        nom, s = str(a.get("nom", "")).strip(), _suffixe(str(a.get("nom", "")))
        if not nom:
            continue
        brut = {"description": a.get("description", ""),
                "sources": [{k: v for k, v in x.items() if k in ("nom", "depot", "branche", "chemin") and v}
                            for x in a.get("sources", [])]}
        if a.get("base"):
            brut["base"] = {**a["base"], "mot_de_passe_env": f"DB_PASSWORD_{s}"}
        logs = {k: v for k, v in a.get("logs", {}).items() if k in ("dossiers", "groundcover_filtre") and v}
        if a.get("logs", {}).get("ssh", {}).get("hote"):
            logs["ssh"] = {**a["logs"]["ssh"], "mot_de_passe_env": f"SSH_PASSWORD_{s}"}
        if logs:
            brut["logs"] = logs
        if a.get("url"):
            brut["application"] = {"url": a["url"], "utilisateur": a.get("utilisateur_application", ""),
                                   "mot_de_passe_env": f"APP_PASSWORD_{s}"}
        resultat[nom] = brut
    return resultat


def _fusion(local: dict, commun: dict) -> dict:
    """Le référentiel commun l'emporte ; les réglages propres à l'agent (fichiers interdits, dossier des fichiers
    générés, données réelles, actions dans l'application, motif et format des logs SSH…) viennent de profils.toml."""
    resultat = dict(local)
    for cle, valeur in commun.items():
        if isinstance(valeur, dict) and isinstance(local.get(cle), dict):
            resultat[cle] = _fusion(local[cle], valeur)
        else:
            resultat[cle] = valeur
    return resultat


def _profils_bruts() -> dict:
    """{nom: (brut, gere)} : applications du manager puis projets locaux de profils.toml (un projet local de même
    nom qu'une application du manager n'apporte que ses réglages propres)."""
    locaux = _lire_toml().get("profils", {}) if FICHIER_PROFILS.exists() else {}
    communs = applications_communes()
    if not communs and not FICHIER_PROFILS.exists():
        _lire_toml()                           # message « profils.toml introuvable »
    resultat = {}
    par_identifiant = {_identifiant(n): n for n in locaux}
    for nom, brut in communs.items():
        local = locaux.get(par_identifiant.get(_identifiant(nom), ""), {})
        resultat[nom] = (_fusion(local, brut), True)
    pris = {_identifiant(n) for n in communs}
    for nom, brut in locaux.items():
        if _identifiant(nom) not in pris:
            resultat[nom] = (brut, False)
    return resultat


def noms_profils() -> list:
    if not FICHIER_PROFILS.exists() and not applications_communes():
        return []
    return list(_profils_bruts())


def charger_profil(nom: str, interdits_par_defaut: bool = True) -> Profil:
    profils = _profils_bruts()
    if nom not in profils:
        raise ConfigError(f"Profil « {nom} » inconnu. Profils disponibles : {', '.join(profils) or 'aucun'}.")
    brut, gere = profils[nom]
    return _profil(nom, brut, interdits_par_defaut, gere)


def tous_les_profils() -> list:
    """Profils tels qu'écrits (sans les fichiers interdits par défaut), pour l'écran de configuration."""
    if not FICHIER_PROFILS.exists() and not applications_communes():
        return []
    return [_profil(nom, brut, False, gere) for nom, (brut, gere) in _profils_bruts().items()]


_PAGE_BITBUCKET = re.compile(r"^(https?://[^?#]+?)/projects/([^/?#]+)/repos/([^/?#]+)[^?#]*(?:\?([^#]*))?")


def url_clone(depot: str) -> tuple:
    """(adresse de clonage, branche) d'une adresse de dépôt.

    Une adresse copiée depuis la page web d'un dépôt Bitbucket Server (…/projects/PROJ/repos/depot/browse…)
    devient l'adresse de clonage …/scm/proj/depot.git ; la branche affichée (« ?at=refs/heads/dev ») est
    rendue si elle figure dans l'adresse. Toute autre adresse est rendue telle quelle, sans branche."""
    depot = (depot or "").strip()
    m = _PAGE_BITBUCKET.match(depot)
    if not m:
        return depot, ""
    at = parse_qs(m.group(4) or "").get("at", [""])[0]
    branche = unquote(at[len("refs/heads/"):]) if at.startswith("refs/heads/") else ""
    return f"{m.group(1)}/scm/{m.group(2).lower()}/{m.group(3)}.git", branche


def source_depot(nom: str, depot: str, branche: str) -> Source:
    """Source clonée depuis un dépôt Git, adresse normalisée (voir url_clone) ; la branche saisie l'emporte."""
    depot, branche_url = url_clone(depot)
    defaut = depot.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    return Source(nom=nom or defaut, depot=depot, branche=branche or branche_url)


def _profil(nom: str, brut: dict, interdits_par_defaut: bool, gere: bool = False) -> Profil:

    sources = []
    for i, s in enumerate(brut.get("sources", []), start=1):
        chemin, depot = str(s.get("chemin", "")).strip(), str(s.get("depot", "")).strip()
        if bool(chemin) == bool(depot):
            raise ConfigError(f"Profil « {nom} », source n° {i} : renseigne soit « chemin », soit « depot ».")
        nom_source, branche = str(s.get("nom", "")).strip(), str(s.get("branche", "")).strip()
        if depot:
            sources.append(source_depot(nom_source, depot, branche))
        else:
            sources.append(Source(nom=nom_source or Path(chemin).name, chemin=chemin, branche=branche))
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

    logs_ssh = None
    if logs.get("ssh"):
        s = logs["ssh"]
        logs_ssh = LogsSsh(hote=str(s.get("hote", "")).strip(), dossiers=[str(d).strip() for d in s.get("dossiers", [])],
                           utilisateur=str(s.get("utilisateur", "")).strip(), port=str(s.get("port", "")).strip(),
                           cle=str(s.get("cle", "")).strip(), rebond=str(s.get("rebond", "")).strip(),
                           motif=str(s.get("motif", "")).strip() or LogsSsh.motif,
                           horodatage=str(s.get("horodatage", "")) or LogsSsh.horodatage,
                           mot_de_passe_env=str(s.get("mot_de_passe_env", "")).strip())
        problemes = erreurs_logs_ssh(logs_ssh)
        if problemes:
            raise ConfigError(f"Profil « {nom} », logs SSH : " + " ; ".join(problemes) + ".")

    return Profil(nom=nom, description=str(brut.get("description", "")).strip(), sources=sources,
                  application=application, logs_ssh=logs_ssh, gere=gere,
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

    geres = {_identifiant(p.nom) for p in profils if p.gere} | {_identifiant(n) for n in applications_communes()}
    profils = [p for p in profils if not p.gere]
    for p in profils:
        if _identifiant(p.nom) in geres:
            raise ConfigError(f"« {p.nom} » est une application du manager : modifie-la dans le manager.")
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
        if p.logs_ssh and p.logs_ssh.hote:
            problemes = erreurs_logs_ssh(p.logs_ssh)
            if problemes:
                raise ConfigError(f"Profil « {p.nom} », logs SSH : " + " ; ".join(problemes) + ".")
            defauts = LogsSsh()
            logs["ssh"] = {k: v for k, v in asdict(p.logs_ssh).items()
                           if k != "mot_de_passe" and v and (k in ("hote", "dossiers") or v != getattr(defauts, k))}
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
        # Entrées locales des applications du manager : gardées telles quelles (réglages propres à l'agent).
        for nom, brut in _lire_toml().get("profils", {}).items():
            if _identifiant(nom) in geres and nom not in donnees["profils"]:
                donnees["profils"][nom] = brut
        shutil.copyfile(FICHIER_PROFILS, FICHIER_PROFILS.with_suffix(".toml.bak"))
    with open(FICHIER_PROFILS, "wb") as f:
        tomli_w.dump(donnees, f)


relever_variables_du_manager()   # avant tout retrait des secrets de os.environ (voir outils.py)
