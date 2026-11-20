"""
Logs lus sur un serveur par SSH (ex. la production), en LECTURE SEULE.

Le serveur exécute des commandes FIXES, construites ici : ``find`` pour lister les fichiers de logs, ``zgrep`` pour chercher, ``zcat``, ``tail``,
``head`` et ``cut`` pour lire. L'agent ne fournit que des paramètres (heures, texte cherché, fichier), contrôlés
puis passés entre guillemets (``shlex.quote``) : il n'a jamais de shell et ne peut lancer aucune autre commande.
Aucune de ces commandes n'écrit sur le serveur.

Côté serveur, la lecture seule doit aussi être garantie par le compte utilisé : un compte dédié qui ne peut que
lire les dossiers de logs (voir le README).

Connexion, deux façons :
- **mot de passe** (le plus simple) : la bibliothèque ``paramiko``, avec l'utilisateur et le mot de passe du
  projet (le mot de passe reste dans le .env). Un mot de passe refusé n'est jamais réessayé avec la même valeur,
  pour ne pas verrouiller le compte ;
- **clé** : le client OpenSSH du poste (``ssh``, fourni avec Windows), en ``BatchMode`` (clé ou agent SSH).
Dans les deux cas, la clé du serveur est vérifiée (``~/.ssh/known_hosts`` : serveur inconnu refusé, se connecter
une fois à la main pour l'enregistrer) et rien n'est redirigé.

Heures : celles des logs, c'est-à-dire l'heure locale du serveur, supposée être celle du poste. Une heure avec
fuseau (``Z``, ``+01:00``) est convertie dans l'heure du poste.
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import re
import shlex
import shutil
import socket
import subprocess
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from fnmatch import fnmatch

FENETRE_RECHERCHE_MAX = timedelta(hours=24)
MINUTES_LECTURE_MAX = 60
LIGNES_MAX = 200                 # lignes rendues à l'agent au plus
LIGNES_BRUTES_MAX = 5000         # lignes rapatriées au plus par une recherche, avant le filtre sur l'heure
FICHIERS_LISTE_MAX = 100
FICHIERS_RECHERCHE_MAX = 20      # fichiers parcourus au plus par une recherche
LONGUEUR_LIGNE_MAX = 2000
RECHERCHE_MAX = 200
DELAI_SECONDES = 90

# Seuls programmes lancés sur le serveur (vérifié par les tests).
PROGRAMMES = ("find", "zgrep", "zcat", "tail", "head", "cut", "test")

_CHEMIN = re.compile(r"^/[A-Za-z0-9._/+-]*$")
_MOTIF = re.compile(r"^[A-Za-z0-9._*?+-]+$")
_HOTE = re.compile(r"^[A-Za-z0-9._-]+$")
_UTILISATEUR = re.compile(r"^[A-Za-z0-9._@\\-]+$")    # accepte DOMAINE\\utilisateur
_REBOND = re.compile(r"^[A-Za-z0-9._@:,-]+$")
_SPECIAUX_ERE = set(".[]()*+?^$|{}\\")


class LogsSshErreur(Exception):
    """Configuration, paramètre ou connexion invalide, avec un message lisible."""


@dataclass
class LogsSsh:
    """Section ``[profils.<p>.logs.ssh]`` de profils.toml."""
    hote: str = ""                   # nom d'hôte, ou alias de ~/.ssh/config
    dossiers: list = field(default_factory=list)   # dossiers de logs sur le serveur (chemins absolus)
    utilisateur: str = ""            # vide : celui de ~/.ssh/config ou du poste
    port: str = ""                   # vide : 22 ou ~/.ssh/config
    cle: str = ""                    # chemin de la clé privée sur le poste ; vide : agent SSH ou ~/.ssh/config
    rebond: str = ""                 # serveur de rebond (ProxyJump), facultatif
    motif: str = "*.log*"            # fichiers retenus dans les dossiers (inclut les .log.gz de la rotation)
    horodatage: str = "%Y-%m-%d %H:%M:%S"   # format de l'heure en début de ligne (après un « [ » éventuel)
    mot_de_passe_env: str = ""       # variable du .env qui porte le mot de passe (vide : connexion par clé)
    mot_de_passe: str = field(default="", repr=False, compare=False)   # rempli au chargement du profil, jamais enregistré


@dataclass
class Fichier:
    chemin: str
    taille: int
    modifie: datetime                # heure du poste


# ---------------------------------------------------------------------------
# Contrôle de la configuration et des paramètres
# ---------------------------------------------------------------------------

def erreurs(c: LogsSsh) -> list:
    """Problèmes de configuration (liste vide si tout va bien)."""
    problemes = []
    if not _HOTE.match(c.hote or ""):
        problemes.append("hôte manquant ou invalide (lettres, chiffres, point, tiret)")
    if c.utilisateur and not _UTILISATEUR.match(c.utilisateur):
        problemes.append(f"utilisateur invalide : « {c.utilisateur} »")
    if c.port and not (str(c.port).isdigit() and 0 < int(c.port) < 65536):
        problemes.append(f"port invalide : « {c.port} »")
    if c.rebond and not _REBOND.match(c.rebond):
        problemes.append(f"serveur de rebond invalide : « {c.rebond} »")
    if not c.dossiers:
        problemes.append("au moins un dossier de logs est nécessaire")
    for d in c.dossiers:
        if not _CHEMIN.match(d) or posixpath.normpath(d) != d or d == "/":
            problemes.append(f"dossier invalide : « {d} » (chemin absolu, sans « .. », ni espace, ni caractère spécial)")
    if not _MOTIF.match(c.motif or ""):
        problemes.append(f"motif de fichiers invalide : « {c.motif} »")
    try:
        _largeur(c.horodatage)
    except LogsSshErreur as e:
        problemes.append(str(e))
    return problemes


def _verifier(c: LogsSsh) -> None:
    problemes = erreurs(c)
    if problemes:
        raise LogsSshErreur("Logs SSH mal configurés : " + " ; ".join(problemes) + ".")


def _largeur(format_heure: str) -> int:
    """Largeur fixe de l'horodatage ; le format doit contenir jour, heure et minute, à largeur fixe."""
    if not all(x in (format_heure or "") for x in ("%d", "%H", "%M")):
        raise LogsSshErreur(f"format d'horodatage invalide : « {format_heure} » (il faut au moins %d, %H et %M)")
    exemples = {datetime(2026, 1, 2, 3, 4, 5).strftime(format_heure),
                datetime(2026, 12, 28, 23, 59, 59).strftime(format_heure)}
    if len({len(e) for e in exemples}) != 1:
        raise LogsSshErreur(f"format d'horodatage à largeur variable : « {format_heure} »")
    return len(exemples.pop())


def heure(valeur: str) -> datetime:
    """Heure ISO 8601 → heure du poste, sans fuseau."""
    try:
        moment = datetime.fromisoformat(str(valeur).strip().replace("Z", "+00:00"))
    except ValueError:
        raise LogsSshErreur(f"Heure illisible : « {valeur} » (ISO 8601 attendu, ex. 2026-11-18T09:41:00).")
    return moment.astimezone().replace(tzinfo=None) if moment.tzinfo else moment


def _fichier_autorise(c: LogsSsh, chemin: str) -> str:
    chemin = str(chemin).strip()
    if not _CHEMIN.match(chemin) or posixpath.normpath(chemin) != chemin:
        raise LogsSshErreur(f"Fichier refusé : « {chemin} » (chemin absolu complet attendu, tel que listé par "
                            f"logs_ssh_fichiers).")
    if not any(posixpath.dirname(chemin) == d for d in c.dossiers):
        raise LogsSshErreur(f"Fichier refusé : « {chemin} » n'est pas dans un dossier de logs du projet.")
    if not fnmatch(posixpath.basename(chemin), c.motif):
        raise LogsSshErreur(f"Fichier refusé : « {chemin} » ne correspond pas au motif {c.motif}.")
    return chemin


def _echapper_ere(texte: str) -> str:
    return "".join("\\" + x if x in _SPECIAUX_ERE else x for x in texte)


def horodatage(ligne: str, format_heure: str, largeur: int) -> datetime | None:
    debut = ligne[1:] if ligne.startswith("[") else ligne
    try:
        return datetime.strptime(debut[:largeur], format_heure)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Commandes (construites ici uniquement)
# ---------------------------------------------------------------------------

def commande_ssh(c: LogsSsh, distante: str) -> list:
    """Ligne de commande du client ssh du poste."""
    ssh = shutil.which("ssh")
    if not ssh:
        raise LogsSshErreur("Client ssh introuvable sur ce poste (Windows : fonctionnalité facultative "
                            "« Client OpenSSH »).")
    arguments = [ssh, "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15",
                 "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
                 "-o", "RequestTTY=no"]
    if c.port:
        arguments += ["-p", str(c.port)]
    if c.utilisateur:
        arguments += ["-l", c.utilisateur]
    if c.cle:
        arguments += ["-i", os.path.expanduser(c.cle), "-o", "IdentitiesOnly=yes"]
    if c.rebond:
        arguments += ["-J", c.rebond]
    return arguments + ["--", c.hote, distante]


def commande_lister(c: LogsSsh) -> str:
    dossiers = " ".join(shlex.quote(d) for d in c.dossiers)
    return (f"LC_ALL=C find {dossiers} -maxdepth 1 -type f -name {shlex.quote(c.motif)} "
            f"-printf {shlex.quote('%T@ %s %p' + chr(92) + 'n')}")


def commande_chercher(fichiers: list, texte: str, contexte: int) -> str:
    apres = f"-A {int(contexte)} " if contexte else ""
    liste = " ".join(shlex.quote(f) for f in fichiers)
    return (f"LC_ALL=C zgrep -h -i -F {apres}-e {shlex.quote(texte)} -- {liste} "
            f"| head -n {LIGNES_BRUTES_MAX} | cut -c 1-{LONGUEUR_LIGNE_MAX}")


def commande_lire(fichier: str, motif_ere: str, lignes: int) -> str:
    f, m = shlex.quote(fichier), shlex.quote(motif_ere)
    return (f"N=$(LC_ALL=C zgrep -n -m 1 -E -e {m} -- {f} | head -n 1 | cut -d : -f 1); "
            f"test -n \"$N\" && zcat -f -- {f} | tail -n +\"$N\" | head -n {int(lignes)} "
            f"| cut -c 1-{LONGUEUR_LIGNE_MAX}")


KNOWN_HOSTS = os.path.join("~", ".ssh", "known_hosts")
_REFUSES: set = set()        # (hôte, port, utilisateur, empreinte du mot de passe) refusés : jamais réessayés


def _empreinte(c: LogsSsh) -> tuple:
    return (c.hote, str(c.port or 22), c.utilisateur, hashlib.sha256(c.mot_de_passe.encode("utf-8")).hexdigest())


def _paramiko():
    try:
        import paramiko
    except ImportError:
        raise LogsSshErreur("Connexion par mot de passe : installe la bibliothèque paramiko "
                            "(venv\\Scripts\\python.exe -m pip install -r requirements.txt).")
    return paramiko


def _executer_mot_de_passe(c: LogsSsh, distante: str) -> str:
    """Connexion par utilisateur et mot de passe (paramiko), une commande, puis fermeture."""
    if c.rebond:
        raise LogsSshErreur("Le serveur de rebond n'est pas pris en charge avec un mot de passe : utilise une clé.")
    if not c.utilisateur:
        raise LogsSshErreur("Connexion par mot de passe : l'utilisateur SSH est obligatoire.")
    if _empreinte(c) in _REFUSES:
        raise LogsSshErreur(f"Utilisateur ou mot de passe refusé par {c.hote} lors d'un essai précédent : corrige-le "
                            f"dans Configuration > Projets (aucun nouvel essai, pour ne pas verrouiller le compte).")
    paramiko = _paramiko()
    client = paramiko.SSHClient()
    connus = os.path.expanduser(KNOWN_HOSTS)
    if os.path.exists(connus):
        client.load_host_keys(connus)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(c.hote, port=int(c.port or 22), username=c.utilisateur, password=c.mot_de_passe,
                       key_filename=os.path.expanduser(c.cle) if c.cle else None, look_for_keys=False,
                       allow_agent=False, timeout=15, banner_timeout=15, auth_timeout=15)
        _, stdout, stderr = client.exec_command(distante, timeout=DELAI_SECONDES)
        sortie = stdout.read()
        erreur = stderr.read()
        code = stdout.channel.recv_exit_status()
    except paramiko.AuthenticationException:
        _REFUSES.add(_empreinte(c))
        raise LogsSshErreur(f"Utilisateur ou mot de passe refusé par {c.hote} : vérifie-les dans Configuration > "
                            f"Projets (aucun nouvel essai avec ce mot de passe, pour ne pas verrouiller le compte).")
    except paramiko.BadHostKeyException:
        raise LogsSshErreur(f"La clé du serveur {c.hote} a changé depuis son enregistrement : vérifie-la avec "
                            f"l'équipe du serveur avant de corriger {connus}.")
    except (socket.timeout, TimeoutError):
        raise LogsSshErreur(f"Le serveur n'a pas répondu en {DELAI_SECONDES} s : resserre la recherche.")
    except paramiko.SSHException as e:
        if "known_hosts" in str(e) or "not found" in str(e):
            raise LogsSshErreur(f"Clé du serveur inconnue : connecte-toi une fois à la main (ssh {c.utilisateur}@"
                                f"{c.hote}) pour la vérifier et l'enregistrer.")
        raise LogsSshErreur(f"Connexion SSH impossible : {str(e)[:300]}")
    except OSError as e:
        raise LogsSshErreur(f"Connexion SSH impossible : {str(e)[:300]}")
    finally:
        client.close()
    texte, message = sortie.decode("utf-8", errors="replace"), erreur.decode("utf-8", errors="replace").strip()
    if code not in (0, 1) and not texte.strip():
        raise LogsSshErreur(f"Erreur sur le serveur (code {code}) : {message[:300]}")
    return texte


def avec_mot_de_passe(c: LogsSsh, mot_de_passe: str) -> LogsSsh:
    return replace(c, mot_de_passe=mot_de_passe or "")


def _executer(c: LogsSsh, distante: str, executer=None) -> str:
    """Lance la commande distante ; renvoie sa sortie. ``executer`` remplace subprocess.run dans les tests."""
    if c.mot_de_passe and executer is None:
        return _executer_mot_de_passe(c, distante)
    arguments = commande_ssh(c, distante)
    try:
        r = (executer or subprocess.run)(arguments, capture_output=True, stdin=subprocess.DEVNULL,
                                         timeout=DELAI_SECONDES)
    except subprocess.TimeoutExpired:
        raise LogsSshErreur(f"Le serveur n'a pas répondu en {DELAI_SECONDES} s : resserre la recherche.")
    except OSError as e:
        raise LogsSshErreur(f"Lancement de ssh impossible : {e}")
    sortie = r.stdout.decode("utf-8", errors="replace")
    erreur = r.stderr.decode("utf-8", errors="replace").strip()
    if r.returncode == 255:
        if "Host key verification failed" in erreur or "No ED25519 host key" in erreur or "host key" in erreur.lower():
            raise LogsSshErreur(f"Clé du serveur inconnue ou modifiée : connecte-toi une fois à la main "
                                f"(ssh {c.hote}) pour la vérifier et l'enregistrer. Détail : {erreur[:300]}")
        raise LogsSshErreur(f"Connexion SSH impossible : {erreur[:300] or 'code 255'}")
    if r.returncode not in (0, 1) and not sortie.strip():
        raise LogsSshErreur(f"Erreur sur le serveur (code {r.returncode}) : {erreur[:300]}")
    return sortie


# ---------------------------------------------------------------------------
# Opérations
# ---------------------------------------------------------------------------

def lister(c: LogsSsh, executer=None) -> list:
    """Fichiers de logs des dossiers configurés, du plus récent au plus ancien."""
    _verifier(c)
    fichiers = []
    for ligne in _executer(c, commande_lister(c), executer).splitlines():
        morceaux = ligne.split(" ", 2)
        if len(morceaux) != 3:
            continue
        try:
            fichiers.append(Fichier(chemin=morceaux[2], taille=int(morceaux[1]),
                                    modifie=datetime.fromtimestamp(float(morceaux[0]))))
        except (ValueError, OSError):
            continue
    return sorted(fichiers, key=lambda f: f.modifie, reverse=True)


def texte_fichiers(fichiers: list) -> str:
    if not fichiers:
        return "(aucun fichier de logs)"
    lignes = [f"{f.modifie:%Y-%m-%d %H:%M}  {_taille(f.taille):>9}  {f.chemin}" for f in fichiers[:FICHIERS_LISTE_MAX]]
    if len(fichiers) > FICHIERS_LISTE_MAX:
        lignes.append(f"… {len(fichiers) - FICHIERS_LISTE_MAX} fichier(s) plus ancien(s) non listé(s)")
    return "Dernière modification (heure du poste), taille, fichier :\n" + "\n".join(lignes)


def _taille(octets: int) -> str:
    for unite in ("o", "Ko", "Mo", "Go"):
        if octets < 1024 or unite == "Go":
            return f"{octets:.0f} {unite}" if unite == "o" else f"{octets:.1f} {unite}"
        octets /= 1024
    return ""


def _limiter(lignes: list, limite: int) -> str:
    limite = max(1, min(int(limite), LIGNES_MAX))
    texte = "\n".join(lignes[:limite])
    if len(lignes) > limite:
        texte += f"\n… {len(lignes) - limite} ligne(s) de plus : resserre la fenêtre ou la recherche."
    return texte


def chercher(c: LogsSsh, texte: str, debut: str, fin: str, fichier: str = "", contexte: int = 0,
             limite: int = 50, executer=None) -> str:
    """Lignes contenant ``texte`` (sans tenir compte de la casse) entre ``debut`` et ``fin``, avec ``contexte``
    lignes après chacune (pile d'exception)."""
    _verifier(c)
    texte = str(texte or "")
    if not texte.strip() or len(texte) > RECHERCHE_MAX or any(x in texte for x in "\r\n\0"):
        raise LogsSshErreur(f"Texte à chercher obligatoire, sur une ligne, {RECHERCHE_MAX} caractères au plus "
                            f"(pour lire une période sans filtre, utilise logs_ssh_lire).")
    d, f = heure(debut), heure(fin)
    if f <= d or f - d > FENETRE_RECHERCHE_MAX:
        raise LogsSshErreur("Fenêtre invalide : la fin doit suivre le début, 24 heures au plus.")
    contexte = max(0, min(int(contexte or 0), 30))
    notes = []
    if fichier:
        cibles = [_fichier_autorise(c, fichier)]
    else:
        candidats = sorted((x for x in lister(c, executer) if x.modifie >= d), key=lambda x: x.modifie)
        if not candidats:
            return f"(aucun fichier de logs modifié depuis {d:%Y-%m-%d %H:%M})"
        if len(candidats) > FICHIERS_RECHERCHE_MAX:
            notes.append(f"{len(candidats)} fichiers couvrent la période : seuls les {FICHIERS_RECHERCHE_MAX} "
                         f"premiers sont parcourus ; resserre la fenêtre ou précise le fichier.")
        cibles = [x.chemin for x in candidats[:FICHIERS_RECHERCHE_MAX]]

    brutes = _executer(c, commande_chercher(cibles, texte, contexte), executer).splitlines()
    largeur = _largeur(c.horodatage)
    retenues, garder = [], False
    for ligne in brutes:
        if ligne == "--":                       # séparateur de contexte de grep
            continue
        moment = horodatage(ligne, c.horodatage, largeur)
        if moment is None:
            if garder:
                retenues.append(ligne)
            continue
        garder = d <= moment <= f
        if garder:
            retenues.append(ligne)
    if len(brutes) >= LIGNES_BRUTES_MAX:
        notes.append(f"Plus de {LIGNES_BRUTES_MAX} lignes trouvées sur le serveur : résultat incomplet, "
                     f"précise la recherche.")
    corps = _limiter(retenues, limite) if retenues else f"(aucune ligne entre {d:%Y-%m-%d %H:%M:%S} et {f:%H:%M:%S})"
    entete = f"Fichiers parcourus : {', '.join(posixpath.basename(x) for x in cibles)}"
    return "\n".join([entete] + notes + [corps])


def lire(c: LogsSsh, debut: str, minutes: int = 10, fichier: str = "", limite: int = LIGNES_MAX,
         executer=None) -> str:
    """Lignes d'un fichier à partir de ``debut``, sur ``minutes`` minutes au plus (sans filtre de texte)."""
    _verifier(c)
    d = heure(debut)
    minutes = max(1, min(int(minutes or 10), MINUTES_LECTURE_MAX))
    f = d + timedelta(minutes=minutes)
    if fichier:
        cible = _fichier_autorise(c, fichier)
    else:
        candidats = sorted((x for x in lister(c, executer) if x.modifie >= d), key=lambda x: x.modifie)
        if not candidats:
            return f"(aucun fichier de logs modifié depuis {d:%Y-%m-%d %H:%M})"
        cible = candidats[0].chemin
    format_minute = c.horodatage[:c.horodatage.index("%M") + 2]
    debut_minute = d.replace(second=0, microsecond=0)
    prefixes = {(debut_minute + timedelta(minutes=i)).strftime(format_minute) for i in range(minutes + 1)}
    motif = "^\\[?(" + "|".join(_echapper_ere(p) for p in sorted(prefixes)) + ")"
    limite = max(1, min(int(limite), LIGNES_MAX))
    brutes = _executer(c, commande_lire(cible, motif, limite + 1), executer).splitlines()

    largeur = _largeur(c.horodatage)
    retenues = []
    for ligne in brutes:
        moment = horodatage(ligne, c.horodatage, largeur)
        if moment is not None and moment > f:
            break
        if moment is not None and moment < d and not retenues:
            continue                            # même minute que le début, mais avant l'heure demandée
        retenues.append(ligne)
    entete = f"Fichier : {cible}"
    if not retenues:
        return f"{entete}\n(aucune ligne entre {d:%Y-%m-%d %H:%M:%S} et {f:%H:%M:%S})"
    return f"{entete}\n{_limiter(retenues, limite)}"


def tester(c: LogsSsh, executer=None) -> tuple:
    """(ok, message) : connexion et dossiers."""
    try:
        fichiers = lister(c, executer)
    except LogsSshErreur as e:
        return False, str(e)
    if not fichiers:
        return False, f"connexion OK, mais aucun fichier {c.motif} dans {', '.join(c.dossiers)}"
    return True, f"connexion OK, {len(fichiers)} fichier(s) de logs, le plus récent : {fichiers[0].chemin}"
