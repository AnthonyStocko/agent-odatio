"""
Fichiers d'interface générés par l'agent (outil fichier_ecrire), à intégrer ensuite À LA MAIN par le testeur
(écran Intégration de fichier…).

Seule écriture permise à l'agent, et dans un seul dossier : celui du profil (config.dossier_fichiers), jamais
sous une source ni un dossier de logs. Le nom est un simple nom de fichier (ni chemin, ni sous-dossier), avec
une extension autorisée ; un fichier existant n'est jamais écrasé. Les secrets reconnus sont masqués et
l'écriture est journalisée (nom, taille, encodage ; jamais le contenu).
"""
import re
from datetime import datetime
from pathlib import Path

from tools.secret_tools import mask_secrets

from . import config
from .outils import OutilErreur, journal

EXTENSIONS = (".xml", ".csv", ".txt", ".json", ".dat", ".edi")
ENCODAGES = ("utf-8", "iso-8859-1", "cp1252")
FINS_LIGNE = {"lf": "\n", "crlf": "\r\n"}
TAILLE_MAX = 5 * 1024 * 1024          # octets, après encodage
NOM_FICHIER = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
RESERVES = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}


def verifier_nom(nom: str) -> str:
    """Nom de fichier accepté (sans espace autour), ou OutilErreur expliquant le refus."""
    nom = (nom or "").strip()
    if "/" in nom or "\\" in nom or ":" in nom:
        raise OutilErreur(f"Nom refusé « {nom} » : donne un simple nom de fichier, sans chemin ni sous-dossier.")
    if ".." in nom or nom.startswith("."):
        raise OutilErreur(f"Nom refusé « {nom} » : « .. » et les noms commençant par un point sont interdits.")
    if not NOM_FICHIER.match(nom):
        raise OutilErreur(f"Nom refusé « {nom} » : lettres, chiffres, point, tiret et souligné uniquement "
                          f"(120 caractères au plus).")
    if nom.split(".")[0].upper() in RESERVES:
        raise OutilErreur(f"Nom refusé « {nom} » : nom réservé par Windows.")
    if Path(nom).suffix.lower() not in EXTENSIONS:
        raise OutilErreur(f"Extension refusée pour « {nom} » : {', '.join(EXTENSIONS)} uniquement.")
    return nom


def encoder(contenu: str, encodage: str = "utf-8", fins_ligne: str = "lf") -> bytes:
    """Contenu avec les fins de ligne demandées, encodé sans BOM ; un caractère que l'encodage ne connaît pas
    est refusé plutôt que remplacé."""
    encodage, fins_ligne = (encodage or "utf-8").lower(), (fins_ligne or "lf").lower()
    if encodage not in ENCODAGES:
        raise OutilErreur(f"Encodage « {encodage} » non pris en charge : {', '.join(ENCODAGES)}.")
    if fins_ligne not in FINS_LIGNE:
        raise OutilErreur(f"Fins de ligne « {fins_ligne} » inconnues : lf ou crlf.")
    texte = contenu.replace("\r\n", "\n").replace("\r", "\n").replace("\n", FINS_LIGNE[fins_ligne])
    try:
        return texte.encode(encodage)
    except UnicodeEncodeError as e:
        raise OutilErreur(f"Le caractère « {e.object[e.start:e.end]} » (position {e.start}) n'existe pas en "
                          f"{encodage} : remplace-le ou choisis un autre encodage.")


def _chemin_libre(dossier: Path, nom: str) -> Path:
    """dossier/nom, ou dossier/<nom>_<horodatage>[_n].<ext> si le nom est déjà pris."""
    chemin = dossier / nom
    if not chemin.exists():
        return chemin
    base, ext = Path(nom).stem, Path(nom).suffix
    horodatage = datetime.now().strftime("%Y%m%d-%H%M%S")
    chemin, n = dossier / f"{base}_{horodatage}{ext}", 1
    while chemin.exists():
        n += 1
        chemin = dossier / f"{base}_{horodatage}_{n}{ext}"
    return chemin


def ecrire(profil: config.Profil, nom: str, contenu: str, encodage: str = "utf-8", fins_ligne: str = "lf") -> dict:
    """Écrit un fichier d'interface dans le dossier du profil :
    {"nom", "chemin", "taille", "encodage", "fins_ligne", "renomme", "secrets_masques"}."""
    details = {"fichier": str(nom)[:120], "encodage": encodage, "fins_ligne": fins_ligne}
    try:
        erreur = config.erreur_dossier_fichiers(profil)
        if erreur:
            raise OutilErreur(erreur[0].upper() + erreur[1:])
        nom = verifier_nom(nom)
        if len(contenu or "") > TAILLE_MAX:
            raise OutilErreur(f"Contenu trop gros : {TAILLE_MAX // (1024 * 1024)} Mo au plus.")
        contenu, masques = mask_secrets(contenu or "")
        donnees = encoder(contenu, encodage, fins_ligne)
        if len(donnees) > TAILLE_MAX:
            raise OutilErreur(f"Contenu trop gros : {TAILLE_MAX // (1024 * 1024)} Mo au plus.")
        dossier = config.dossier_fichiers(profil)
        dossier.mkdir(parents=True, exist_ok=True)
        while True:
            chemin = _chemin_libre(dossier, nom)
            try:
                with open(chemin, "xb") as f:   # "x" : échoue plutôt que d'écraser
                    f.write(donnees)
                break
            except FileExistsError:
                continue
    except OutilErreur as e:
        journal(profil, "fichier_ecrire", details, str(e))
        raise
    except OSError as e:
        message = f"Écriture impossible : {type(e).__name__} : {e.strerror or e}"
        journal(profil, "fichier_ecrire", details, message)
        raise OutilErreur(message)
    resultat = {"nom": chemin.name, "chemin": str(chemin), "taille": len(donnees), "encodage": encodage.lower(),
                "fins_ligne": fins_ligne.lower(), "renomme": chemin.name != nom,
                "secrets_masques": sum(masques.values())}
    journal(profil, "fichier_ecrire", {**details, "fichier": chemin.name, "taille": len(donnees),
                                       "secrets_masques": resultat["secrets_masques"]})
    return resultat
