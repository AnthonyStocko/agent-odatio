"""
Sources d'un profil : dossiers locaux utilisés tels quels, dépôts Git clonés (ou mis à jour) dans
.cache/sources/<profil>/<nom>. L'agent ne lit que ces dossiers, jamais il n'y écrit.

Authentification Git : jeton HTTP Bitbucket (BITBUCKET_HTTP_TOKEN dans le .env), passé en en-tête à chaque
commande et jamais écrit dans l'URL ni dans la configuration du clone.
"""
import subprocess
from pathlib import Path

from . import config
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


def preparer(profil: config.Profil, mettre_a_jour: bool = True, journal=print) -> list:
    """Renvoie [(nom, chemin absolu)] des sources du profil, en clonant ou mettant à jour les dépôts."""
    prets = []
    for source in profil.sources:
        if source.chemin:
            chemin = Path(source.chemin).expanduser().resolve()
            if not chemin.is_dir():
                raise SourceError(f"Source « {source.nom} » : dossier introuvable ({chemin}).")
            prets.append((source.nom, chemin))
            continue

        cible = config.CACHE / "sources" / profil.nom / source.nom
        if (cible / ".git").is_dir():
            if mettre_a_jour:
                journal(f"Mise à jour de « {source.nom} »…")
                branche = source.branche or _git(["rev-parse", "--abbrev-ref", "HEAD"], cible).strip()
                _git(["fetch", "--depth", "1", "origin", branche], cible)
                _git(["reset", "--hard", "FETCH_HEAD"], cible)
        else:
            journal(f"Clonage de « {source.nom} » (première fois, peut prendre quelques minutes)…")
            cible.parent.mkdir(parents=True, exist_ok=True)
            args = ["clone", "--depth", "1"] + (["--branch", source.branche] if source.branche else [])
            _git(args + [source.depot, str(cible)])
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
