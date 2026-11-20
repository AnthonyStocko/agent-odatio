"""
Sources d'un profil : dossiers locaux utilisés tels quels, dépôts Git clonés (ou mis à jour) dans
.cache/sources/<profil>/<nom>. L'agent ne lit que ces dossiers, jamais il n'y écrit.

Une source peut aussi désigner un projet Bitbucket entier (adresse de la page du projet, …/projects/PROJ) : tous
ses dépôts sont alors clonés dans .cache/sources/<profil>/<nom>/<depot>, et un dépôt ajouté au projet est
récupéré à la mise à jour suivante. La branche de la source s'applique aux dépôts qui l'ont ; les autres
suivent leur branche par défaut.

Le Standard n'est jamais cloné : une source dépôt dont le nom contient « standard » est lue par l'agent sur
Bitbucket (outils bitbucket_*), sans copie locale. Un Standard en dossier local reste lu tel quel.

Authentification Git : jeton HTTP Bitbucket (BITBUCKET_HTTP_TOKEN dans le .env), passé en en-tête à chaque
commande et jamais écrit dans l'URL ni dans la configuration du clone.
"""
import subprocess
from pathlib import Path

from . import bitbucket, config
from tools.secret_tools import mask_secrets


class SourceError(Exception):
    pass


def _git(args: list, cwd: Path | None = None) -> str:
    jeton = config.secret("BITBUCKET_HTTP_TOKEN")
    entete = ["-c", f"http.extraHeader=Authorization: Bearer {jeton}"] if jeton else []
    try:
        resultat = subprocess.run(["git", *entete, *args], cwd=cwd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=600)
    except FileNotFoundError:
        raise SourceError("git introuvable : installe Git et vérifie qu'il est dans le PATH.")
    except subprocess.TimeoutExpired:
        raise SourceError(f"git {args[0]} : délai dépassé (10 min).")
    if resultat.returncode != 0:
        message = mask_secrets((resultat.stderr or resultat.stdout).strip())[0]
        raise SourceError(f"git {args[0]} a échoué : {message[-500:]}")
    return resultat.stdout


def est_standard(source: config.Source) -> bool:
    """Source Standard : son nom contient « standard » (même règle que la synthèse client)."""
    return "standard" in source.nom.lower()


def distantes(profil: config.Profil) -> list:
    """Sources lues sur Bitbucket sans être clonées : le Standard quand il est donné par un dépôt ou un projet."""
    return [s for s in profil.sources if s.depot and not s.chemin and est_standard(s)]


def verifier_distante(profil: config.Profil, source: config.Source) -> str:
    """Constat d'accès à une source lue sur Bitbucket (lève SourceError si elle n'est pas lisible)."""
    if not bitbucket.configure(profil):
        raise SourceError(f"Source « {source.nom} » : lue sur Bitbucket, mais Bitbucket n'est pas configuré "
                          "(jeton dans l'onglet Configuration, Accès).")
    try:
        return f"lue sur Bitbucket, non clonée ({bitbucket.acces(profil, source.depot)})"
    except bitbucket.BitbucketErreur as e:
        raise SourceError(f"Source « {source.nom} » (Bitbucket) : {e}")


def _depot(cible: Path, url: str, branche: str, mettre_a_jour: bool, journal, libelle: str) -> None:
    """Clone le dépôt dans cible, ou le met à jour (dernier commit de la branche, sans historique)."""
    if (cible / ".git").is_dir():
        if mettre_a_jour:
            journal(f"Mise à jour de « {libelle} »…")
            branche = branche or _git(["rev-parse", "--abbrev-ref", "HEAD"], cible).strip()
            _git(["fetch", "--depth", "1", "origin", branche], cible)
            _git(["reset", "--hard", "FETCH_HEAD"], cible)
    else:
        journal(f"Clonage de « {libelle} » (première fois, peut prendre quelques minutes)…")
        cible.parent.mkdir(parents=True, exist_ok=True)
        _git(["clone", "--depth", "1"] + (["--branch", branche] if branche else []) + [url, str(cible)])


def _projet(profil: config.Profil, source: config.Source, projet: str, mettre_a_jour: bool, journal) -> Path:
    """Clone ou met à jour tous les dépôts du projet Bitbucket dans le dossier de la source."""
    dossier = config.CACHE / "sources" / profil.nom / source.nom
    deja = dossier.is_dir() and any((d / ".git").is_dir() for d in dossier.iterdir())
    if deja and not mettre_a_jour:
        return dossier.resolve()
    try:
        depots = bitbucket.depots_du_projet(profil, projet)
    except bitbucket.BitbucketErreur as e:
        raise SourceError(f"Source « {source.nom} » (projet Bitbucket {projet}) : {e}")
    if not depots:
        raise SourceError(f"Source « {source.nom} » : le projet Bitbucket {projet} n'a aucun dépôt lisible.")
    faits, echecs = 0, []
    for nom, url in depots:
        libelle = f"{source.nom}/{nom}"
        try:
            tetes = _git(["ls-remote", "--heads", url])
            if not tetes.strip():
                journal(f"« {libelle} » : dépôt vide, ignoré.")
                continue
            branche = source.branche if f"refs/heads/{source.branche}" in tetes.split() else ""
            if source.branche and not branche:
                journal(f"« {libelle} » : pas de branche « {source.branche} », branche par défaut.")
            _depot(dossier / nom, url, branche, mettre_a_jour, journal, libelle)
            faits += 1
        except SourceError as e:
            echecs.append(f"{nom} : {e}")
            journal(f"« {libelle} » non récupéré : {e}")
    if not faits:
        raise SourceError(f"Source « {source.nom} » : aucun dépôt du projet {projet} n'a pu être récupéré ("
                          + "; ".join(echecs[:3]) + ").")
    return dossier.resolve()


def preparer(profil: config.Profil, mettre_a_jour: bool = True, journal=print) -> list:
    """Renvoie [(nom, chemin absolu)] des sources locales du profil, en clonant ou mettant à jour les dépôts.
    Le Standard en dépôt (voir distantes) n'y figure pas : il n'est jamais cloné."""
    prets = []
    a_distance = distantes(profil)
    for source in profil.sources:
        if source in a_distance:
            continue
        if source.chemin:
            chemin = Path(source.chemin).expanduser().resolve()
            if not chemin.is_dir():
                raise SourceError(f"Source « {source.nom} » : dossier introuvable ({chemin}).")
            prets.append((source.nom, chemin))
            continue

        projet = bitbucket.projet_depuis_url(source.depot)
        if projet:
            prets.append((source.nom, _projet(profil, source, projet, mettre_a_jour, journal)))
            continue

        cible = config.CACHE / "sources" / profil.nom / source.nom
        _depot(cible, source.depot, source.branche, mettre_a_jour, journal, source.nom)
        prets.append((source.nom, cible.resolve()))
    return prets


def dossiers_logs(profil: config.Profil) -> list:
    """Dossiers de logs existants du profil (chemins absolus) ; un dossier absent est signalé, pas bloquant."""
    presents = []
    for dossier in profil.dossiers_logs:
        chemin = Path(dossier).expanduser()
        if chemin.is_dir():
            presents.append(chemin.resolve())
        else:
            print(f"⚠ Dossier de logs introuvable, ignoré : {chemin}")
    return presents
