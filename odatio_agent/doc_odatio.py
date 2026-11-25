"""
Documentation Odatio (produit Standard), en lecture seule, lue dans ses SOURCES sur Bitbucket, sans clone.

Le site publié est derrière une connexion Microsoft : l'agent lit donc les sources de la documentation, avec son
client Bitbucket (bitbucket.py : GET et recherche de code, projets autorisés, fichiers interdits, réponses bornées et
masquées, jeton jamais dans l'environnement de Claude Code).

Configuration, tenue par agent-manager-savoye (Configuration › Accès › Documentation Odatio) et transmise au
lancement, ou à défaut dans le .env de l'agent lancé seul :
  ODATIO_DOC_DEPOT    dépôt des sources : « PROJET/depot » ou URL Bitbucket
  ODATIO_DOC_BRANCHE  branche ou tag (vide = branche par défaut du dépôt)
  ODATIO_DOC_DOSSIER  sous-dossier de la documentation dans le dépôt (vide = tout le dépôt)
"""
import posixpath
import re
from pathlib import PurePosixPath
from urllib.parse import quote, urlsplit

from . import bitbucket, config

PAGES = (".md", ".rst", ".adoc", ".asciidoc", ".html", ".txt")    # sources possibles d'une page publiée


def configuree(profil=None) -> bool:
    return bool(config.secret("ODATIO_DOC_DEPOT")) and bitbucket.configure(profil)


def _cadre() -> tuple:
    """(dépôt, branche, dossier) de la documentation."""
    depot = config.secret("ODATIO_DOC_DEPOT")
    if not depot:
        raise bitbucket.BitbucketErreur("Documentation Odatio non configurée (ODATIO_DOC_DEPOT, dans le manager).")
    dossier = _propre(config.secret("ODATIO_DOC_DOSSIER"))
    return depot, config.secret("ODATIO_DOC_BRANCHE"), dossier


def _propre(chemin: str) -> str:
    """Chemin relatif sans remontée ni chemin absolu."""
    brut = (chemin or "").replace("\\", "/").strip()
    if brut.startswith("/") or re.match(r"^[A-Za-z]:", brut) or ".." in PurePosixPath(brut).parts:
        raise bitbucket.BitbucketErreur(f"Chemin refusé « {chemin} » : relatif à la documentation, sans « .. ».")
    propre = posixpath.normpath(brut) if brut else ""
    return "" if propre == "." else propre


def _dans_la_doc(chemin: str, dossier: str) -> str:
    """Chemin dans le dépôt : relatif au dossier de la doc, ou déjà complet (tel que doc_sommaire l'affiche)."""
    chemin = _propre(chemin)
    if not dossier or chemin == dossier or chemin.startswith(dossier + "/"):
        return chemin
    return posixpath.join(dossier, chemin) if chemin else dossier


def _fichiers(profil, depot: str, branche: str, dossier: str) -> list:
    """Tous les fichiers du dossier de la doc, chemins complets dans le dépôt."""
    p, d, _ = bitbucket.resoudre(profil, depot)
    chemin = bitbucket._chemin_depot(p, d) + "/files" + (("/" + quote(dossier)) if dossier else "")
    valeurs = bitbucket._pages(profil, chemin, {"at": bitbucket._ref(branche)} if branche else {},
                               maximum=5000, taille=1000)
    return [(dossier + "/" + v) if dossier else v for v in valeurs]


def resoudre(profil, chemin: str) -> str:
    """Fichier source d'un chemin de la doc, ou de l'adresse / du chemin d'une page du site publié
    (…/development/index.html → …/development/index.rst)."""
    depot, branche, dossier = _cadre()
    brut = (chemin or "").strip()
    if re.match(r"^https?://", brut):
        brut = urlsplit(brut).path
        for marqueur in ("/doc/", "/docs/"):          # site publié : ce qui suit le nom du site
            if marqueur in brut:
                brut = brut.split(marqueur, 1)[1].split("/", 1)[-1]
                break
    brut = brut.split("#", 1)[0].split("?", 1)[0].lstrip("/")
    complet = _dans_la_doc(brut, dossier)
    suffixe = PurePosixPath(complet).suffix.lower()
    if suffixe and suffixe not in (".html", ".htm"):
        return complet                                 # source déjà désignée : lue telle quelle
    tous = set(_fichiers(profil, depot, branche, dossier))
    if complet in tous:
        return complet
    souche = re.sub(r"\.html?$", "", complet)
    candidats = [souche + e for e in PAGES] + [f"{souche}/index{e}" for e in PAGES]
    for c in candidats:
        if c in tous:
            return c
    relatif = [c[len(dossier) + 1:] if dossier else c for c in candidats]   # site publié dans un sous-dossier
    proches = sorted(f for f in tous if f.endswith(tuple("/" + r for r in relatif)))
    if len(proches) == 1:
        return proches[0]
    raise bitbucket.BitbucketErreur(f"« {chemin} » " + (f"ambigu ({', '.join(proches[:8])})" if proches else
                                                         "introuvable dans la documentation")
                                    + " : utilise doc_sommaire ou doc_chercher.")


def sommaire(profil, dossier: str = "", motif: str = "") -> str:
    depot, branche, racine = _cadre()
    return "Documentation Odatio — " + bitbucket.fichiers(profil, depot, branche, _dans_la_doc(dossier, racine), motif)


def lire(profil, chemin: str, debut: int = 1, nombre: int = bitbucket.LIGNES_MAX) -> str:
    depot, branche, _ = _cadre()
    return "Documentation Odatio — " + bitbucket.lire(profil, depot, resoudre(profil, chemin), branche, debut, nombre)


def chercher(profil, texte: str) -> str:
    depot, _, _ = _cadre()
    return "Documentation Odatio — " + bitbucket.chercher(profil, texte, depot=depot)
