"""
Synthèse client pour le démarrage et le passage au support édition : ce qui est déployé, ce qui est spécifique,
comment c'est configuré, ce qui semble incohérent, chaque affirmation étant prouvée par une source.

Déroulé (``executer``), tout dans .cache/synthese/<projet>-<date>/ :
  1. inventaire.json  INVENTAIRE DÉTERMINISTE, sans appel au modèle (``inventorier``) : fichiers des sources
                      spécifiques absents du Standard ou différents de lui, classés (surcharge, ajout, paramétrage)
                      et regroupés en éléments fonctionnels ; tickets cités dans le code ; version du Standard lue
                      dans les pom.xml ; commit de chaque source. L'exhaustivité vient de là, pas du modèle.
  2. lots/NNN.json    une session NEUVE de l'agent par lot d'éléments (Reglages.synthese_lot) : il explique chaque
                      élément (objectif, composants, ticket, source, statut vérifié / déduit) ou dit pourquoi il ne
                      le peut pas, avec synthese_demarrer puis synthese_enregistrer.
  3. finale.json      une session finale : contexte, configuration en place (base), audit, points à vérifier.
  4. rapport.md/.docx assemblés par Python, contrôlés (``valider_rapport``) : six sections, source et statut sur
                      chaque ligne de tableau, aucun secret, e-mail, téléphone ni chaîne de connexion.
  etat.json           avancement, enregistré après CHAQUE lot : un arrêt (bouton, onglet fermé, plantage) ne perd
                      que le lot en cours, et ``executer`` sur le même dossier reprend au lot suivant.

Lecture seule : rien n'est écrit ailleurs que dans ce dossier. Les noms de personnes sont écartés par la
consigne de rédaction (un filtre automatique bloquerait des noms de classes ou de clients) : le rapport est à
relire avant envoi.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests

from tools.secret_tools import remaining_secrets

from . import bitbucket, config
from .documentation import _EMAIL, _SECRET_EN_CLAIR, _TELEPHONE

SECTIONS = ("Identité et contexte", "Développements spécifiques", "Configuration en place",
            "Audit et points de vigilance", "Points à vérifier / informations manquantes", "Couverture")
STATUTS = ("vérifié", "déduit")
SEVERITES = ("haute", "moyenne", "faible")
CATEGORIES = ("traitement / règle métier", "processus Studio", "interface / flux", "édition / étiquette", "écran",
              "paramétrage", "script SQL", "libellés", "autre")
FICHIERS_MAX = 30_000          # fichiers parcourus au plus par source spécifique
FICHIERS_PAR_ELEMENT = 15      # chemins montrés au modèle par élément
TAILLE_LECTURE = 300_000       # octets lus au plus par fichier (recherche de tickets)
TEXTE_MAX = 600                # caractères d'une cellule du rapport

DOSSIERS_IGNORES = {".git", ".svn", ".idea", ".vscode", ".settings", ".mvn", "target", "build", "dist", "out",
                    "node_modules", "bower_components", ".gradle", "__pycache__", ".cache"}
FICHIERS_IGNORES = ("pom.xml", "*.md", "*.iml", ".git*", "jenkinsfile", "dockerfile", "*.lock", "package.json",
                    "package-lock.json", "angular.json", "tsconfig*.json", ".editorconfig", ".browserslistrc",
                    "karma.conf.js", "mvnw", "mvnw.cmd", "*.sh", "*.bat", "*.cmd", "*.class", "*.jar", "*.war",
                    "*.zip", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.svg", "*.woff*", "*.ttf", "*.eot",
                    "*.pdf", "*.log", ".project", ".classpath", ".factorypath", "kis.descriptor.yaml")
PARAMETRAGE = ("*.properties", "*.yml", "*.yaml", "*param*.xml", "*parameter*", "*config*.xml", "*.csv")
EDITIONS = (".jrxml", ".jasper", ".zpl", ".prn", ".lbl", ".btw", ".xsl", ".xslt", ".fo", ".rdl", ".rpt", ".nlbl")
ECRANS = (".html", ".htm", ".ts", ".js", ".jsx", ".tsx", ".vue", ".css", ".scss", ".less")
STUDIO = re.compile(r"^(customized\.(xml|json)|original\.xml|i18n\..+\.txt)$")
INTERFACE = re.compile(r"(^|[/_-])(interfaces?|flux|edi|import|export|exchange|echange|connector|connecteur|"
                       r"inbound|outbound|webservices?|ws|xsd)([/_.-]|$)", re.I)
EDITION_DOSSIER = re.compile(r"(^|[/_-])(editions?|reports?|etiquettes?|labels?|documents?|printing|impression)"
                             r"([/_-]|$)", re.I)
TICKET = re.compile(r"\b([A-Z][A-Z0-9_]{1,14})-(\d{1,6})\b")
FAUX_TICKETS = {"UTF", "ISO", "SHA", "AES", "RSA", "HTTP", "TLS", "SSL", "MD", "CP", "WINDOWS", "IBM", "GB", "BIG",
                "KOI", "EUC", "US", "JIS", "ASCII", "LATIN", "UCS", "RFC", "CVE", "JSR", "JDK", "JAVA", "TLSV",
                "X", "V", "LOG4J", "SLF4J", "H2", "PG", "OAUTH", "OIDC", "CRC", "ES", "ECMA", "GMT", "UTC"}
SEGMENTS_GENERIQUES = {
    "action", "actions", "core", "service", "services", "impl", "rest", "api", "internal", "util", "utils", "model",
    "dto", "pojo", "pojos", "repository", "config", "spec", "common", "resources", "main", "java", "src", "com",
    "fr", "org", "net", "odatio", "constant", "constants", "data", "enum", "enums", "exception", "exceptions",
    "mapper", "mappers", "converter", "controllers", "controller", "templates", "template", "views", "components",
    "app", "js", "html", "i18n", "db", "sql", "migration", "kotlin", "domain", "entity", "entities", "web",
}
_CONNEXION = re.compile(r"(?i)(jdbc:[a-z0-9]+:|mongodb(\+srv)?://|redis://|amqps?://|ldaps?://|"
                        r"\b(server|data source)=[^;]+;[^\n]*(password|pwd)=)")
_CLE_PRIVEE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


class SyntheseErreur(Exception):
    pass


# ---------------------------------------------------------------------------
# Inventaire déterministe (0 token)
# ---------------------------------------------------------------------------

def source_standard(sources: list) -> str:
    """Nom de la source Standard d'un profil : la première dont le nom contient « standard » ('' sinon)."""
    return next((nom for nom, _ in sources if "standard" in nom.lower()), "")


def verifier_standard(profil, standard: str = "") -> str:
    """Nom de la source Standard retenue pour la synthèse (choisie, sinon déduite du nom) : dossier local ou
    source Bitbucket, lue sans être clonée. SyntheseErreur si le nom choisi n'est pas une source du projet."""
    if standard and not any(s.nom == standard for s in profil.sources):
        raise SyntheseErreur(f"Source Standard « {standard} » inconnue dans ce projet.")
    return standard or source_standard([(s.nom, "") for s in profil.sources if s.chemin or s.depot])


def _ignore(chemin: str) -> bool:
    nom = chemin.rsplit("/", 1)[-1].lower()
    return any(fnmatch.fnmatchcase(nom, g) for g in FICHIERS_IGNORES)


def _fichiers(racine: Path, limite: int = FICHIERS_MAX) -> tuple[list, bool]:
    """Chemins relatifs (séparateur /) des fichiers d'une source, hors dossiers et fichiers techniques ;
    (liste, tronquée)."""
    resultat = []
    for dossier, sous_dossiers, noms in os.walk(racine):
        sous_dossiers[:] = sorted(d for d in sous_dossiers if d not in DOSSIERS_IGNORES and not d.startswith("."))
        base = Path(dossier).relative_to(racine).as_posix()
        for nom in sorted(noms):
            chemin = nom if base == "." else f"{base}/{nom}"
            if not _ignore(chemin):
                resultat.append(chemin)
                if len(resultat) >= limite:
                    return resultat, True
    return resultat, False


def _empreinte(fichier: Path) -> str:
    """Empreinte du contenu, fins de ligne normalisées (core.autocrlf), '' si illisible."""
    try:
        return hashlib.sha1(fichier.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    except OSError:
        return ""


def _git_info(racine: Path) -> dict:
    """Commit et date du dernier commit d'une source (dépôt Git), {} pour un dossier non versionné."""
    if not (racine / ".git").exists():
        return {}
    try:
        sortie = subprocess.run(["git", "-C", str(racine), "log", "-1", "--format=%H|%cI"], capture_output=True,
                                text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if sortie.returncode != 0 or "|" not in sortie.stdout:
        return {}
    commit, date = sortie.stdout.strip().split("|", 1)
    return {"commit": commit[:12], "date": date[:19]}


def categorie(chemin: str, depot: str = "") -> str:
    """Catégorie fonctionnelle d'un fichier spécifique (voir CATEGORIES)."""
    nom = chemin.rsplit("/", 1)[-1]
    bas, extension = nom.lower(), Path(nom).suffix.lower()
    if STUDIO.match(nom):
        return "libellés" if bas.startswith("i18n") else "processus Studio"
    if extension == ".sql":
        return "script SQL"
    if bas.startswith(("i18n", "messages")) or "/i18n/" in f"/{chemin.lower()}":
        return "libellés"
    if extension in EDITIONS or (EDITION_DOSSIER.search(chemin) and extension not in (".java", ".kt")):
        return "édition / étiquette"
    if any(fnmatch.fnmatchcase(bas, g) for g in PARAMETRAGE):
        return "paramétrage"
    if "-plugin/" in f"{chemin}/" or extension == ".xsd" or INTERFACE.search(chemin):
        return "interface / flux"
    if extension in ECRANS or re.search(r"(-frontend|-front|-ui)$", depot, re.I) or "/webapp/" in f"/{chemin}":
        return "écran"
    if extension in (".java", ".kt", ".groovy"):
        return "traitement / règle métier"
    return "autre"


def _cle_metier(segments: list) -> str:
    for segment in reversed(segments):
        bas = segment.lower()
        if bas not in SEGMENTS_GENERIQUES and not re.fullmatch(r"v\d+(_\d+)*", bas) and len(bas) > 2:
            return segment
    return segments[-1] if segments else "."


def unite(chemin: str) -> str:
    """Unité fonctionnelle d'un fichier : dossier d'un processus Studio, plugin, objet métier (dernier segment
    métier du package ou du dossier de ressources), sinon module (dossier composé) ou dossier."""
    parties = chemin.split("/")
    if STUDIO.match(parties[-1]) and len(parties) >= 2:
        return "/".join(parties[:-1])
    plugin = next((p for p in parties[:-1] if p.endswith("-plugin")), None)
    if plugin:
        return plugin
    for repere in ("src/main/java/", "src/main/kotlin/", "src/main/resources/"):
        if f"/{repere}" in f"/{chemin}":
            return _cle_metier(chemin.split(repere, 1)[1].split("/")[:-1])
    module = next((p for p in reversed(parties[:-1]) if "-" in p), "")
    return module or _cle_metier(parties[:-1])


def _tickets_texte(texte: str) -> list:
    return [f"{p}-{n}" for p, n in TICKET.findall(texte) if p not in FAUX_TICKETS and not p.startswith("X")]


def _tickets(fichier: Path) -> list:
    try:
        with open(fichier, "rb") as f:
            texte = f.read(TAILLE_LECTURE).decode("utf-8", errors="ignore")
    except OSError:
        return []
    return _tickets_texte(texte)


# --- version du Standard (pom.xml) ------------------------------------------------------------------------------

_VERSION = re.compile(r"(?<![\d.])(\d{1,4})\.(\d{1,3})((?:\.\d+)*)")
_PROPRIETE = re.compile(r"\$\{([^}]+)\}")


def _balise(noeud) -> str:
    return noeud.tag.rsplit("}", 1)[-1]


def _enfant(noeud, nom: str):
    return next((n for n in noeud if _balise(n) == nom), None) if noeud is not None else None


def _texte(noeud, nom: str) -> str:
    trouve = _enfant(noeud, nom)
    return (trouve.text or "").strip() if trouve is not None else ""


def _pom(fichier: Path):
    try:
        return ET.fromstring(fichier.read_bytes())
    except (OSError, ET.ParseError):
        return None


def _poms(racine: Path, profondeur: int = 3) -> list:
    return sorted(p for p in racine.rglob("pom.xml") if len(p.relative_to(racine).parts) <= profondeur
                  and not set(p.relative_to(racine).parts) & DOSSIERS_IGNORES)


def _groupes(poms) -> set:
    """groupIds déclarés par des pom.xml déjà lus (racines XML)."""
    groupes = set()
    for racine_pom in poms:
        if racine_pom is not None:
            groupe = _texte(racine_pom, "groupId") or _texte(_enfant(racine_pom, "parent"), "groupId")
            if groupe:
                groupes.add(groupe)
    return groupes


def groupes_standard(racine: Path) -> set:
    """groupIds du Standard : ceux de ses pom.xml (racine et modules), jamais en dur."""
    return _groupes(_pom(f) for f in _poms(racine, 2))


def _citations(relatif: str, pom, groupes: set) -> list:
    """Versions d'artefacts du Standard citées par un pom.xml lu (propriétés ${…} résolues)."""
    trouvees = []
    proprietes = _enfant(pom, "properties")
    props = {_balise(p): (p.text or "").strip() for p in (proprietes if proprietes is not None else [])}

    def ajouter(noeud, emplacement: str) -> None:
        groupe, artefact = _texte(noeud, "groupId"), _texte(noeud, "artifactId")
        if not any(groupe == g or groupe.startswith(g + ".") for g in groupes):
            return
        brute = _texte(noeud, "version")
        noms = _PROPRIETE.findall(brute)
        for _ in range(5):
            brute = _PROPRIETE.sub(lambda m: props.get(m.group(1), m.group(0)), brute)
        m = _VERSION.search(brute)
        if m:
            trouvees.append({"version": f"{m.group(1)}.{m.group(2)}", "complete": brute, "emplacement": emplacement,
                             "artefact": f"{groupe}:{artefact}",
                             "fichier": relatif + (f" (propriété {noms[0]})" if noms else "")})

    if _enfant(pom, "parent") is not None:
        ajouter(_enfant(pom, "parent"), "parent")
    for portee in [pom] + list(_enfant(pom, "profiles") if _enfant(pom, "profiles") is not None else []):
        gestion = _enfant(_enfant(portee, "dependencyManagement"), "dependencies")
        for dep in gestion if gestion is not None else []:
            ajouter(dep, "bom" if _texte(dep, "scope") == "import" else "dépendance")
        deps = _enfant(portee, "dependencies")
        for dep in deps if deps is not None else []:
            ajouter(dep, "dépendance")
    return trouvees


def versions_standard(nom_source: str, racine: Path, groupes: set) -> list:
    """Versions d'artefacts du Standard citées par les pom.xml d'une source spécifique :
    [{version, complete, artefact, emplacement, fichier}] (propriétés ${…} résolues)."""
    trouvees = []
    for fichier in _poms(racine):
        pom = _pom(fichier)
        if pom is not None:
            trouvees += _citations(f"{nom_source}/{fichier.relative_to(racine).as_posix()}", pom, groupes)
    return trouvees


def _version_retenue(groupes: set, citees: list, poms_standard: list) -> dict:
    """La plus citée par les pom.xml du spécifique (parent d'abord) ; à défaut, celle des pom.xml du Standard
    ([(libellé, racine XML)], branche lue), la plus fréquente. {groupes, citations} si introuvable."""
    resultat = {"groupes": sorted(groupes), "citations": citees[:20]}
    if citees:
        comptes = {}
        for v in citees:
            comptes[v["version"]] = comptes.get(v["version"], 0) + 1
        retenue = max(comptes, key=lambda v: (comptes[v], v))
        ordre = {"parent": 0, "bom": 1, "dépendance": 2}
        preuve = min((v for v in citees if v["version"] == retenue), key=lambda v: ordre[v["emplacement"]])
        resultat.update(version=retenue, complete=preuve["complete"], source=preuve["fichier"],
                        detail=f"{preuve['artefact']} ({preuve['emplacement']})",
                        divergentes=sorted(set(comptes) - {retenue}))
        return resultat
    trouvees = []
    for libelle, pom in poms_standard:
        brute = (_texte(pom, "version") or _texte(_enfant(pom, "parent"), "version")) if pom is not None else ""
        m = _VERSION.search(brute)
        if m:
            trouvees.append((f"{m.group(1)}.{m.group(2)}", brute, libelle))
    if trouvees:
        comptes = {}
        for v, _, _ in trouvees:
            comptes[v] = comptes.get(v, 0) + 1
        retenue = max(comptes, key=lambda v: (comptes[v], v))
        _, brute, libelle = next(x for x in trouvees if x[0] == retenue)
        resultat.update(version=retenue, complete=brute, source=libelle,
                        detail="version du pom de la source Standard (pas citée par le spécifique)", divergentes=[])
    return resultat


def version(standard: tuple | None, specifiques: list) -> dict:
    """Version du Standard déployée : la plus citée par les pom.xml des sources spécifiques (parent d'abord) ;
    à défaut, celle du pom.xml de la source Standard (branche suivie par le profil). {} si introuvable."""
    if standard is None:
        return {}
    nom_std, racine_std = standard
    groupes = groupes_standard(racine_std)
    citees = [v for nom, racine in specifiques for v in versions_standard(nom, racine, groupes)]
    return _version_retenue(groupes, citees, [(f"{nom_std}/pom.xml", _pom(racine_std / "pom.xml"))])


def inventorier(projet: str, sources: list, standard: str = "") -> dict:
    """Inventaire des éléments spécifiques d'un projet : [(nom, chemin)] des sources, ``standard`` = nom de la
    source Standard (déduit du nom à défaut). Un fichier identique à celui du Standard au même chemin n'est pas
    spécifique ; même chemin ou même nom de classe Java dans le Standard, ou processus Studio avec original.* :
    surcharge ; sinon ajout ; paramétrage à part."""
    standard = standard or source_standard(sources)
    std = next(((n, Path(c)) for n, c in sources if n == standard), None)
    specifiques = [(n, Path(c)) for n, c in sources if n != standard]
    limites = []
    if std is None:
        limites.append("Aucune source Standard désignée : impossible de distinguer surcharges et ajouts ; tous les "
                       "fichiers des sources sont traités comme spécifiques.")
    if not specifiques:
        raise SyntheseErreur("Le profil n'a aucune source spécifique (seulement le Standard) : rien à inventorier.")

    index_classes, std_fichiers = {}, set()
    if std is not None:
        tous, tronque = _fichiers(std[1], FICHIERS_MAX * 3)
        if tronque:
            limites.append(f"Standard : plus de {FICHIERS_MAX * 3} fichiers, index des classes incomplet.")
        std_fichiers = set(tous)
        for chemin in tous:
            if chemin.endswith((".java", ".kt")) and "/src/main/" in f"/{chemin}":
                index_classes.setdefault(chemin.rsplit("/", 1)[-1], chemin)

    groupes, identiques, references = {}, 0, []
    for nom, racine in specifiques:
        references.append({"source": nom, **_git_info(racine)})
        chemins, tronque = _fichiers(racine)
        if tronque:
            limites.append(f"{nom} : plus de {FICHIERS_MAX} fichiers, inventaire tronqué.")
        presents = set(chemins)
        for chemin in chemins:
            fichier_nom = chemin.rsplit("/", 1)[-1]
            if fichier_nom.startswith("original."):        # référence Standard d'un processus Studio
                continue
            entree = {"chemin": chemin}
            if std is not None and chemin in std_fichiers:
                if _empreinte(racine / chemin) == _empreinte(std[1] / chemin):
                    identiques += 1
                    continue
                entree.update(type="surcharge", standard=f"{std[0]}/{chemin}")
            elif fichier_nom.startswith("customized.") and \
                    chemin[:-len(fichier_nom)] + "original" + fichier_nom[10:] in presents:
                entree.update(type="surcharge", standard=chemin[:-len(fichier_nom)] + "original" + fichier_nom[10:])
            elif std is not None and fichier_nom in index_classes and "/src/main/" in f"/{chemin}":
                entree.update(type="surcharge", standard=f"{std[0]}/{index_classes[fichier_nom]}")
            else:
                entree["type"] = "ajout"
            cat = categorie(chemin, nom)
            if cat == "paramétrage" and entree["type"] == "ajout":
                entree["type"] = "paramétrage"
            cle = (nom, cat, unite(chemin))
            groupe = groupes.setdefault(cle, {"fichiers": [], "tickets": {}})
            groupe["fichiers"].append(entree)
            for t in _tickets(racine / chemin):
                groupe["tickets"][t] = groupe["tickets"].get(t, 0) + 1

    elements = []
    for (source, cat, unit), groupe in sorted(groupes.items(), key=lambda g: (CATEGORIES.index(g[0][1]), g[0])):
        types = {f["type"] for f in groupe["fichiers"]}
        nature = "surcharge" if "surcharge" in types else ("paramétrage" if types == {"paramétrage"} else "ajout")
        elements.append({
            "id": "e-" + hashlib.sha1(f"{source}:{cat}:{unit}".encode()).hexdigest()[:8],
            "source": source, "categorie": cat, "unite": unit, "nature": nature,
            "nb_fichiers": len(groupe["fichiers"]),
            "fichiers": sorted(groupe["fichiers"], key=lambda f: (f["type"] != "surcharge", f["chemin"])),
            "tickets": [t for t, _ in sorted(groupe["tickets"].items(), key=lambda x: (-x[1], x[0]))][:5],
        })
    return {"projet": projet, "genere_le": datetime.now().isoformat(timespec="seconds"),
            "standard": {"source": std[0], **_git_info(std[1])} if std else {}, "specifiques": references,
            "version": version(std, specifiques), "elements": elements, "fichiers_identiques": identiques,
            "limites": limites}


# ---------------------------------------------------------------------------
# Inventaire lu sur Bitbucket (0 token, sans clonage)
# ---------------------------------------------------------------------------

BB_PARALLELE = 8                # appels Bitbucket simultanés au plus
BB_DELAI = 20                   # secondes au plus par requête Bitbucket
TAILLE_COMPARAISON = 5_000_000  # octets lus au plus par fichier comparé
PROGRESSION = 250               # une ligne de progression toutes les N opérations
_PRIORITE_LECTURE = (".java", ".kt", ".groovy", ".xml", ".sql", ".properties", ".yml", ".yaml", ".ts", ".js",
                     ".html", ".json", ".txt", ".csv")


def sur_bitbucket(profil) -> bool:
    """La synthèse de ce profil lit-elle ses sources sur Bitbucket ? Oui dès qu'une source est un dépôt ou un
    projet Bitbucket : ces sources ne sont alors jamais clonées pour la synthèse."""
    from . import sources as depots
    return bool(depots.lues_sur_bitbucket(profil))


class _Lecteur:
    """Appels Bitbucket de l'inventaire : comptés, délai par requête, connexions réutilisées (une session HTTP
    par fil), parallélisme borné."""

    def __init__(self, profil, delai: float = BB_DELAI, parallele: int = BB_PARALLELE):
        self.profil, self.delai, self.parallele = profil, delai, max(1, int(parallele))
        self.appels = 0
        self._verrou = threading.Lock()
        self._local = threading.local()
        self._sessions = []

    def _session(self):
        if getattr(self._local, "session", None) is None:
            self._local.session = requests.Session()
            with self._verrou:
                self._sessions.append(self._local.session)
        return self._local.session

    def get(self, profil, chemin: str, params: dict | None = None, brut: bool = False, flux: bool = False):
        with self._verrou:
            self.appels += 1
        return bitbucket._get(self.profil, chemin, params, brut=brut, delai=self.delai, session=self._session(),
                              flux=flux)

    def pages(self, chemin: str, params: dict | None = None, maximum: int = 500, taille: int = 100) -> list:
        return bitbucket._pages(self.profil, chemin, params, maximum=maximum, taille=taille, get=self.get)

    def brut(self, projet: str, depot: str, chemin: str, branche: str, maximum: int) -> tuple:
        return bitbucket.contenu_brut(self.profil, projet, depot, chemin, branche, maximum, get=self.get)

    def en_parallele(self, fonction, elements: list, sur_avance=None) -> list:
        """[(élément, résultat ou exception)] dans l'ordre des éléments ; ``sur_avance(faits, total)`` est appelé
        dans le fil principal (Streamlit peut y interrompre : les appels en attente sont alors annulés)."""
        if not elements:
            return []
        resultats = [None] * len(elements)
        executeur = ThreadPoolExecutor(max_workers=self.parallele)
        try:
            futurs = {executeur.submit(fonction, e): i for i, e in enumerate(elements)}
            for faits, futur in enumerate(as_completed(futurs), start=1):
                i = futurs[futur]
                try:
                    resultats[i] = (elements[i], futur.result())
                except Exception as e:     # BitbucketErreur, fichier illisible… : noté par l'appelant
                    resultats[i] = (elements[i], e)
                if sur_avance and (faits % PROGRESSION == 0 or faits == len(elements)):
                    sur_avance(faits, len(elements))
        finally:
            executeur.shutdown(wait=False, cancel_futures=True)
        return resultats

    def fermer(self) -> None:
        for s in self._sessions:
            s.close()


class _Depot:
    """Un dépôt lu pendant l'inventaire : Bitbucket (projet, slug, branche) ou dossier local (racine)."""

    def __init__(self, source: str, projet: str = "", slug: str = "", voulue: str = "", racine: Path | None = None):
        self.source, self.projet, self.slug, self.voulue, self.racine = source, projet, slug, voulue, racine
        self.depot = f"{projet}/{slug}" if projet else ""
        self.branche, self.commit, self.date, self.erreur = "", "", "", ""
        self.repli = False           # branche voulue absente : branche par défaut lue
        self.fichiers, self.poms, self.interdits, self.tronque = [], [], 0, False

    @property
    def libelle(self) -> str:
        return self.depot or self.source

    def info(self) -> dict:
        if self.racine is not None:
            return {"source": self.source, **_git_info(self.racine)}
        return {k: v for k, v in (("depot", self.depot), ("branche", self.branche), ("commit", self.commit),
                                  ("date", self.date), ("erreur", self.erreur)) if v}

    def lire(self, lecteur: _Lecteur, chemin: str, maximum: int) -> bytes:
        if self.racine is not None:
            with open(self.racine / chemin, "rb") as f:
                return f.read(maximum)
        return lecteur.brut(self.projet, self.slug, chemin, self.branche, maximum)[0]

    def reference(self, chemin: str) -> str:
        """Où lire un fichier : « PROJET/depot:chemin » sur Bitbucket, « source/chemin » en local."""
        return f"{self.depot}:{chemin}" if self.depot else f"{self.source}/{chemin}"

    def reference_lue(self, chemin: str) -> str:
        """Référence avec la branche lue (« PROJET/depot:chemin @ branche ») : fichier d'un autre dépôt."""
        return self.reference(chemin) + (f" @ {self.branche}" if self.depot and self.branche else "")


def _garder(chemin: str) -> bool:
    """Mêmes exclusions que l'inventaire local : dossiers techniques ou cachés, fichiers techniques."""
    parties = chemin.split("/")
    return not any(p in DOSSIERS_IGNORES or p.startswith(".") for p in parties[:-1]) and not _ignore(chemin)


def _depots_bitbucket(lecteur: _Lecteur, profil, source) -> list:
    """[(PROJET, slug)] d'une source : tous les dépôts d'un projet, ou le dépôt indiqué."""
    projet = bitbucket.projet_depuis_url(source.depot)
    if projet:
        autorises = bitbucket.projets_autorises()
        if autorises and projet not in autorises:
            raise bitbucket.BitbucketErreur(f"Projet Bitbucket {projet} non autorisé.")
        return sorted((projet, v["slug"]) for v in lecteur.pages(f"{bitbucket.API}/projects/{quote(projet)}/repos",
                                                                 maximum=1000))
    p, d, _ = bitbucket.resoudre(profil, source.nom)
    return [(p, d)]


def _preparer_depot(lecteur: _Lecteur, d: _Depot, candidates: list, limite: int) -> _Depot:
    """Branche (première candidate présente, sinon branche par défaut), dernier commit et liste des fichiers."""
    if d.racine is not None:
        tous, d.tronque = _fichiers(d.racine, limite)
        d.fichiers = [c for c in tous if not bitbucket._interdit(c, lecteur.profil)]
        d.interdits = len(tous) - len(d.fichiers)
        d.poms = [p.relative_to(d.racine).as_posix() for p in _poms(d.racine)]
        return d
    base = bitbucket._chemin_depot(d.projet, d.slug)
    voulues = [b for b in candidates if b]
    commit = None
    for branche in voulues:
        try:
            commit = lecteur.get(None, base + "/commits", {"until": bitbucket._ref(branche), "limit": 1})
            d.branche = branche
            break
        except bitbucket.BitbucketErreur:
            continue
    if commit is None:
        d.repli = bool(voulues)
        try:
            reponse = lecteur.get(None, base + "/branches/default", brut=True)
        except bitbucket.BitbucketErreur as e:
            d.erreur = f"branche par défaut introuvable : {e}"
            return d
        try:     # dépôt vide : Bitbucket répond 204, sans contenu
            d.branche = reponse.json().get("displayId", "") if reponse.status_code != 204 and reponse.content else ""
        except ValueError:
            d.branche = ""
        if not d.branche:
            d.erreur = "dépôt vide"
            return d
        commit = lecteur.get(None, base + "/commits", {"until": bitbucket._ref(d.branche), "limit": 1})
    valeurs = commit.get("values") or []
    if not valeurs:
        d.erreur = "dépôt vide"
        return d
    d.commit = str(valeurs[0].get("id") or valeurs[0].get("displayId") or "")[:12]
    quand = valeurs[0].get("committerTimestamp") or valeurs[0].get("authorTimestamp")
    if quand:
        d.date = datetime.fromtimestamp(int(quand) / 1000).isoformat(timespec="seconds")
    tous = lecteur.pages(base + "/files", {"at": bitbucket._ref(d.branche)}, maximum=limite + 1, taille=1000)
    d.tronque = len(tous) > limite
    tous = tous[:limite]
    d.poms = [c for c in tous if c.rsplit("/", 1)[-1] == "pom.xml" and c.count("/") <= 2      # comme _poms
              and not any(p in DOSSIERS_IGNORES or p.startswith(".") for p in c.split("/")[:-1])]
    for chemin in tous:
        if not _garder(chemin):
            continue
        if bitbucket._interdit(chemin, lecteur.profil):
            d.interdits += 1
            continue
        d.fichiers.append(chemin)
    return d


def _xml(octets: bytes):
    try:
        return ET.fromstring(octets)
    except ET.ParseError:
        return None


def inventorier_bitbucket(profil, standard: str = "", reglages=None, sur_etape=lambda texte: None) -> dict:
    """Inventaire des éléments spécifiques quand des sources du profil sont des dépôts ou projets Bitbucket : le
    code est lu par l'API (liste des fichiers, contenu brut), SANS cloner, sans appel au modèle. Mêmes structures
    de sortie que ``inventorier`` ; chaque élément porte en plus son dépôt et sa branche, et l'inventaire le
    nombre d'appels et la durée (« bitbucket »).

    Surcharge : même dépôt et même chemin que le Standard au contenu différent (fins de ligne normalisées ;
    identique = ignoré), même nom de classe Java ou Kotlin sous src/main dans un dépôt du Standard, ou
    customized.* avec original.* (Studio) ; sinon ajout ; paramétrage à part. Branche du Standard : celle de la
    source, sinon « v<version> » d'après les pom.xml du spécifique, sinon la branche par défaut de chaque dépôt.
    Plafonds (Reglages.synthese_depots, synthese_comparaisons, synthese_lectures), dépassements et erreurs de
    dépôt : notés dans « limites », jamais ignorés. Une source en dossier local est lue telle quelle. Rien n'est
    écrit : l'appelant enregistre l'inventaire (``creer``)."""
    from . import sources as depots
    reglages = reglages or config.reglages()
    if not bitbucket.configure(profil):
        raise SyntheseErreur("Les sources de ce projet sont sur Bitbucket, mais Bitbucket n'est pas configuré "
                             "(jeton dans la page Configuration, Accès).")
    debut = time.monotonic()
    standard = verifier_standard(profil, standard)
    lecteur = _Lecteur(profil)
    limites = []
    try:
        # 1. Dépôts : spécifiques d'abord, pour que le plafond écarte plutôt des dépôts du Standard
        sur_etape("Inventaire Bitbucket : liste des dépôts…")
        dep_std, dep_spe, ecartes = [], [], []
        for s in sorted(profil.sources, key=lambda s: s.nom == standard):
            cible = dep_std if s.nom == standard else dep_spe
            if s.chemin:
                try:
                    cible.append(_Depot(s.nom, racine=dict(depots.locales(config.Profil(nom=profil.nom,
                                                                                         sources=[s])))[s.nom]))
                except depots.SourceError as e:
                    limites.append(f"Source {s.nom} : {e} ; aucun de ses fichiers n'est inventorié.")
                continue
            if not s.depot:
                continue
            try:
                trouves = _depots_bitbucket(lecteur, profil, s)
            except bitbucket.BitbucketErreur as e:
                limites.append(f"Source {s.nom} : illisible sur Bitbucket ({e}) ; aucun de ses fichiers n'est "
                               "inventorié.")
                continue
            for projet, slug in trouves:
                if len(dep_std) + len(dep_spe) >= reglages.synthese_depots:
                    ecartes.append(f"{projet}/{slug}")
                    continue
                cible.append(_Depot(s.nom, projet, slug, s.branche))
        if ecartes:
            limites.append(f"Plafond de {reglages.synthese_depots} dépôts atteint : {len(ecartes)} dépôt(s) non "
                           f"parcouru(s) ({', '.join(ecartes[:8])}{'…' if len(ecartes) > 8 else ''}).")
        if not dep_spe:
            raise SyntheseErreur("Le profil n'a aucune source spécifique lisible (seulement le Standard) : rien à "
                                 "inventorier." + (f" {limites[0]}" if limites else ""))
        if not dep_std:
            limites.append("Aucune source Standard lisible : impossible de distinguer surcharges et ajouts ; tous "
                           "les fichiers des sources sont traités comme spécifiques.")

        def avance(libelle):
            return lambda faits, total: sur_etape(f"Inventaire Bitbucket : {libelle} {faits}/{total}…")

        # 2. Dépôts spécifiques : branche, commit, fichiers
        sur_etape(f"Inventaire Bitbucket : {len(dep_spe)} dépôt(s) spécifique(s)…")
        for d, r in lecteur.en_parallele(lambda d: _preparer_depot(lecteur, d, [d.voulue], FICHIERS_MAX), dep_spe,
                                         avance("dépôts spécifiques")):
            if isinstance(r, Exception):
                d.erreur = str(r) or type(r).__name__

        # 3. groupIds du Standard (pom.xml à la racine de chaque dépôt), puis version citée par le spécifique
        sur_etape(f"Inventaire Bitbucket : pom.xml du Standard ({len(dep_std)} dépôt(s))…")

        def poms_racine(d: _Depot) -> list:
            if d.racine is not None:
                return [(f"{d.source}/{p.relative_to(d.racine).as_posix()}", _pom(p)) for p in _poms(d.racine, 2)]
            try:
                brut = lecteur.brut(d.projet, d.slug, "pom.xml", d.voulue, TAILLE_LECTURE)[0]
            except bitbucket.BitbucketErreur:
                if not d.voulue:
                    return []
                try:
                    brut = lecteur.brut(d.projet, d.slug, "pom.xml", "", TAILLE_LECTURE)[0]   # branche par défaut
                except bitbucket.BitbucketErreur:
                    return []
            return [(f"{d.depot}:pom.xml", _xml(brut))]

        poms_std = []
        for _, r in lecteur.en_parallele(poms_racine, dep_std, avance("pom.xml du Standard")):
            if isinstance(r, list):
                poms_std += r
        groupes = _groupes(p for _, p in poms_std)

        def citations_depot(d: _Depot) -> list:
            if d.erreur:
                return []
            trouvees = []
            for chemin in d.poms:
                pom = _xml(d.lire(lecteur, chemin, TAILLE_LECTURE))
                if pom is not None:
                    trouvees += _citations(d.reference(chemin), pom, groupes)
            return trouvees

        citees = []
        for d, r in lecteur.en_parallele(citations_depot, dep_spe):
            if isinstance(r, Exception):
                limites.append(f"{d.libelle} : pom.xml illisibles ({r}).")
            else:
                citees += r
        v = _version_retenue(groupes, citees, []) if dep_std else {}

        # 4. Dépôts du Standard : branche de la version du client, commit, fichiers
        source_std = next((s for s in profil.sources if s.nom == standard), None)
        if source_std is not None and source_std.branche:
            candidates = [source_std.branche]
        elif v.get("version"):
            candidates = [f"v{v['version']}"]
            v["branche_deduite"] = candidates[0]
        else:
            candidates = []
        sur_etape(f"Inventaire Bitbucket : {len(dep_std)} dépôt(s) du Standard"
                  + (f", branche {candidates[0]}" if candidates else "") + "…")
        for d, r in lecteur.en_parallele(lambda d: _preparer_depot(lecteur, d, candidates, FICHIERS_MAX * 3),
                                         dep_std, avance("dépôts du Standard")):
            if isinstance(r, Exception):
                d.erreur = str(r) or type(r).__name__
        if not v.get("version") and poms_std:
            v = _version_retenue(groupes, [], poms_std)

        distants_std = [d for d in dep_std if d.racine is None]
        replis = [d.libelle for d in distants_std if d.repli and not d.erreur]
        if replis:
            limites.append(f"Standard : {len(replis)} dépôt(s) sans branche « {candidates[0]} », lus sur leur "
                           f"branche par défaut ({', '.join(replis[:6])}{'…' if len(replis) > 6 else ''}).")
        elif distants_std and not candidates:
            limites.append("Standard lu sur la branche par défaut de chaque dépôt : branche de la version du client "
                           "inconnue (ni branche dans la source Standard, ni version dans les pom.xml).")
        repli_spe = [d.libelle for d in dep_spe if d.repli and not d.erreur]
        if repli_spe:
            limites.append(f"Spécifique : {len(repli_spe)} dépôt(s) sans la branche de la source, lus sur leur "
                           f"branche par défaut ({', '.join(repli_spe[:6])}{'…' if len(repli_spe) > 6 else ''}).")
        for d in dep_spe + dep_std:
            if d.erreur and d.erreur != "dépôt vide":
                limites.append(f"{d.libelle} : non inventorié ({d.erreur[:200]}).")
            if d.tronque:
                limites.append(f"{d.libelle} : liste des fichiers tronquée (plafond de fichiers par dépôt).")
        interdits = sum(d.interdits for d in dep_spe)
        if interdits:
            limites.append(f"{interdits} fichier(s) interdit(s) par le profil (secrets, clés) écarté(s) sans être "
                           "lu(s).")

        # 5. Index du Standard : (dépôt, chemin), chemin seul (Standard local) et noms de classes sous src/main
        par_depot, par_chemin_local, index_classes = {}, {}, {}
        for d in dep_std:
            if d.erreur:
                continue
            for chemin in d.fichiers:
                if d.racine is None:
                    par_depot[(d.slug, chemin)] = d
                else:
                    par_chemin_local.setdefault(chemin, d)
                if chemin.endswith((".java", ".kt")) and "/src/main/" in f"/{chemin}":
                    index_classes.setdefault(chemin.rsplit("/", 1)[-1], d.reference_lue(chemin))

        def meme_chemin(d: _Depot, chemin: str):
            """(dépôt du Standard, chemin) du fichier Standard au même endroit, None sinon."""
            if d.slug and (d.slug, chemin) in par_depot:                       # même dépôt, même chemin
                return par_depot[(d.slug, chemin)], chemin
            for candidat in ([f"{d.slug}/{chemin}"] if d.slug else []) + [chemin]:   # Standard en dossier local
                if candidat in par_chemin_local:
                    return par_chemin_local[candidat], candidat
            if not d.slug and "/" in chemin:                                   # spécifique local cloné par projet
                tete, reste = chemin.split("/", 1)
                if (tete, reste) in par_depot:
                    return par_depot[(tete, reste)], reste
            return None

        # 6. Classement ; un fichier au même chemin que le Standard est comparé
        retenus, a_comparer = [], []
        for d in dep_spe:
            if d.erreur:
                continue
            presents = set(d.fichiers)
            for chemin in d.fichiers:
                nom = chemin.rsplit("/", 1)[-1]
                if nom.startswith("original."):           # référence Standard d'un processus Studio
                    continue
                trouve = meme_chemin(d, chemin)
                if trouve is not None:
                    a_comparer.append((d, chemin, trouve))
                    continue
                entree = {"chemin": chemin}
                original = chemin[:-len(nom)] + "original" + nom[10:]
                if nom.startswith("customized.") and original in presents:
                    entree.update(type="surcharge", standard=d.reference(original))
                elif nom in index_classes and "/src/main/" in f"/{chemin}":
                    entree.update(type="surcharge", standard=index_classes[nom])
                else:
                    entree["type"] = "ajout"
                retenus.append((d, entree))

        plafond = max(0, int(reglages.synthese_comparaisons))
        compares, hors_plafond = a_comparer[:plafond], a_comparer[plafond:]
        sur_etape(f"Inventaire Bitbucket : {len(compares)} fichier(s) au même chemin que le Standard à comparer…")
        contenus = {}

        def comparer(item) -> tuple:
            d, chemin, (ds, chemin_std) = item
            a = d.lire(lecteur, chemin, TAILLE_COMPARAISON + 1)
            b = ds.lire(lecteur, chemin_std, TAILLE_COMPARAISON + 1)
            if len(a) > TAILLE_COMPARAISON or len(b) > TAILLE_COMPARAISON:
                return None, a                                  # trop gros pour être comparé
            return a.replace(b"\r\n", b"\n") == b.replace(b"\r\n", b"\n"), a

        identiques, non_compares, trop_gros = 0, [], 0
        for (d, chemin, (ds, chemin_std)), r in lecteur.en_parallele(comparer, compares, avance("fichiers comparés")):
            if isinstance(r, Exception):
                non_compares.append(d.reference(chemin))
                continue
            egal, contenu = r
            if egal:
                identiques += 1
                continue
            trop_gros += egal is None
            contenus[(id(d), chemin)] = contenu[:TAILLE_LECTURE]
            retenus.append((d, {"chemin": chemin, "type": "surcharge", "standard": ds.reference_lue(chemin_std)}))
        if hors_plafond:
            limites.append(f"Plafond de {plafond} fichiers comparés atteint : {len(hors_plafond)} fichier(s) au même "
                           "chemin qu'un fichier du Standard non comparé(s), donc absent(s) de l'inventaire (ex. "
                           + ", ".join(d.reference(c) for d, c, _ in hors_plafond[:5]) + ").")
        if non_compares:
            limites.append(f"{len(non_compares)} fichier(s) au même chemin que le Standard illisible(s) sur Bitbucket, "
                           "absent(s) de l'inventaire (ex. " + ", ".join(non_compares[:5]) + ").")
        if trop_gros:
            limites.append(f"{trop_gros} fichier(s) de plus de {TAILLE_COMPARAISON // 1_000_000} Mo non comparé(s) au "
                           "Standard : classé(s) en surcharge sans vérifier qu'ils diffèrent.")

        # 7. Tickets cités : contenu déjà lu pour la comparaison, sinon lecture plafonnée (code d'abord)
        a_lire = [(d, e["chemin"]) for d, e in retenus if (id(d), e["chemin"]) not in contenus]
        a_lire.sort(key=lambda x: (Path(x[1]).suffix.lower() not in _PRIORITE_LECTURE, x[0].libelle, x[1]))
        plafond_lectures = max(0, int(reglages.synthese_lectures))
        lus = a_lire[:plafond_lectures]
        sur_etape(f"Inventaire Bitbucket : {len(lus)} fichier(s) spécifique(s) lu(s) pour les tickets cités…")
        erreurs_lecture = 0
        for (d, chemin), r in lecteur.en_parallele(lambda x: x[0].lire(lecteur, x[1], TAILLE_LECTURE), lus,
                                                   avance("fichiers lus")):
            if isinstance(r, Exception):
                erreurs_lecture += 1
            else:
                contenus[(id(d), chemin)] = r
        if len(a_lire) > plafond_lectures:
            limites.append(f"Plafond de {plafond_lectures} fichiers lus atteint : tickets cités cherchés dans "
                           f"{len(retenus) - len(a_lire) + plafond_lectures} fichier(s) spécifique(s) sur "
                           f"{len(retenus)}.")
        if erreurs_lecture:
            limites.append(f"{erreurs_lecture} fichier(s) spécifique(s) illisible(s) sur Bitbucket : tickets cités "
                           "non cherchés dedans.")

        # 8. Éléments : regroupés par (source, dépôt, catégorie, unité)
        groupes_el = {}
        for d, entree in retenus:
            chemin = entree["chemin"]
            cat = categorie(chemin, d.slug or d.source)
            if cat == "paramétrage" and entree["type"] == "ajout":
                entree["type"] = "paramétrage"
            unit = unite(chemin)
            if unit in (".", "") and d.slug:
                unit = d.slug
            groupe = groupes_el.setdefault((d.source, d.depot, cat, unit),
                                           {"fichiers": [], "tickets": {}, "branche": d.branche})
            groupe["fichiers"].append(entree)
            texte = contenus.get((id(d), chemin), b"").decode("utf-8", errors="ignore")
            for t in _tickets_texte(texte):
                groupe["tickets"][t] = groupe["tickets"].get(t, 0) + 1

        elements = []
        for (source, depot, cat, unit), groupe in sorted(groupes_el.items(),
                                                         key=lambda g: (CATEGORIES.index(g[0][2]), g[0])):
            types = {f["type"] for f in groupe["fichiers"]}
            nature = "surcharge" if "surcharge" in types else ("paramétrage" if types == {"paramétrage"} else "ajout")
            element = {
                "id": "e-" + hashlib.sha1(f"{source}:{depot}:{cat}:{unit}".encode()).hexdigest()[:8],
                "source": source, "categorie": cat, "unite": unit, "nature": nature,
                "nb_fichiers": len(groupe["fichiers"]),
                "fichiers": sorted(groupe["fichiers"], key=lambda f: (f["type"] != "surcharge", f["chemin"])),
                "tickets": [t for t, _ in sorted(groupe["tickets"].items(), key=lambda x: (-x[1], x[0]))][:5],
            }
            if depot:
                element.update(depot=depot, branche=groupe["branche"])
            elements.append(element)

        def references(liste: list) -> list:
            par_source = {}
            for d in liste:
                par_source.setdefault(d.source, []).append(d)
            resultat = []
            for nom, ds in par_source.items():
                if ds[0].racine is not None:
                    resultat.append(ds[0].info())
                    continue
                source = next(s for s in profil.sources if s.nom == nom)
                resultat.append({"source": nom, "bitbucket": bitbucket.cible(source.depot) or source.depot,
                                 "branches": sorted({d.branche for d in ds if d.branche and not d.erreur}),
                                 "depots": [d.info() for d in ds]})
            return resultat

        std_ref = references(dep_std)
        duree = round(time.monotonic() - debut, 1)
        sur_etape(f"Inventaire Bitbucket terminé : {len(elements)} élément(s), {lecteur.appels} appel(s), {duree} s.")
        return {"projet": profil.nom, "genere_le": datetime.now().isoformat(timespec="seconds"),
                "standard": std_ref[0] if std_ref else {}, "specifiques": references(dep_spe),
                "version": v, "elements": elements, "fichiers_identiques": identiques, "limites": limites,
                "bitbucket": {"appels": lecteur.appels, "duree_s": duree, "parallele": lecteur.parallele,
                              "depots": len(dep_std) + len(dep_spe),
                              "fichiers_listes": sum(len(d.fichiers) for d in dep_std + dep_spe),
                              "comparaisons": len(compares), "lectures": len(lus)}}
    finally:
        lecteur.fermer()


# ---------------------------------------------------------------------------
# Dossier, état et lots
# ---------------------------------------------------------------------------

def _slug(texte: str) -> str:
    texte = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", texte).strip("-")[:50] or "projet"


def racine() -> Path:
    return config.CACHE / "synthese"


def _ecrire(fichier: Path, donnees) -> None:
    """Écriture atomique (fichier temporaire puis remplacement) : un arrêt brutal ne laisse pas de JSON coupé."""
    fichier.parent.mkdir(parents=True, exist_ok=True)
    temporaire = fichier.with_suffix(fichier.suffix + ".tmp")
    temporaire.write_text(json.dumps(donnees, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporaire, fichier)


def _lire(fichier: Path, defaut=None):
    try:
        return json.loads(Path(fichier).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaut


def creer(projet: str, inventaire: dict, taille_lot: int) -> Path:
    """Nouveau dossier de synthèse : inventaire et état initial (lots à faire, finale à faire)."""
    dossier = racine() / f"{_slug(projet)}-{datetime.now():%Y%m%d-%H%M%S}"
    ids = [e["id"] for e in inventaire["elements"]]
    taille = max(1, int(taille_lot))
    _ecrire(dossier / "inventaire.json", inventaire)
    _ecrire(dossier / "etat.json", {
        "projet": projet, "cree_le": datetime.now().isoformat(timespec="seconds"), "termine": False,
        "lots": [{"numero": i + 1, "elements": ids[k:k + taille], "statut": "a_faire"}
                 for i, k in enumerate(range(0, len(ids), taille))],
        "finale": {"statut": "a_faire"}, "cout_usd": 0.0, "duree_s": 0.0})
    return dossier


def etat(dossier: Path) -> dict:
    donnees = _lire(Path(dossier) / "etat.json")
    if not isinstance(donnees, dict):
        raise SyntheseErreur(f"Synthèse illisible : {dossier}")
    return donnees


def inventaire(dossier: Path) -> dict:
    donnees = _lire(Path(dossier) / "inventaire.json")
    if not isinstance(donnees, dict):
        raise SyntheseErreur(f"Inventaire illisible : {dossier}")
    return donnees


def lister(projet: str, limite: int = 30) -> list:
    """Synthèses du projet, de la plus récente à la plus ancienne : [{dossier, cree_le, termine, lots, faits}]."""
    resultats = []
    base = racine()
    for dossier in base.glob(f"{_slug(projet)}-*") if base.is_dir() else []:
        e = _lire(dossier / "etat.json")
        if not isinstance(e, dict) or e.get("projet") != projet:
            continue
        resultats.append({"dossier": str(dossier), "cree_le": e.get("cree_le", ""), "termine": e.get("termine"),
                          "lots": len(e.get("lots") or []),
                          "faits": sum(1 for l in e.get("lots") or [] if l.get("statut") == "fait")})
    return sorted(resultats, key=lambda r: r["cree_le"], reverse=True)[:limite]


def prochaine_etape(e: dict, tentes=()) -> tuple:
    """("lot", numéro) du premier lot pas encore fait, ("finale", 0), ou ("fini", 0) ; ``tentes`` : étapes déjà
    tentées pendant cette exécution, sautées (un lot en échec n'est relancé que par une nouvelle reprise)."""
    for lot in e["lots"]:
        if lot["statut"] != "fait" and ("lot", lot["numero"]) not in tentes:
            return "lot", lot["numero"]
    aucun_lot = e["lots"] and not any(l["statut"] == "fait" for l in e["lots"])
    if e["finale"].get("statut") != "fait" and ("finale", 0) not in tentes and not aucun_lot:
        return "finale", 0         # jamais si aucun lot n'a abouti : elle n'aurait rien à synthétiser
    return "fini", 0


def reconcilier(dossier: Path) -> dict:
    """Après un arrêt brutal : une partie enregistrée par l'agent dont l'état n'a pas été mis à jour compte comme
    faite (coût inconnu)."""
    dossier = Path(dossier)
    e = etat(dossier)
    change = False
    for lot in e["lots"]:
        if lot["statut"] != "fait" and (dossier / "lots" / f"{lot['numero']:03d}.json").exists():
            lot.update(statut="fait", message="repris après un arrêt (coût inconnu)")
            change = True
    if e["finale"].get("statut") != "fait" and (dossier / "finale.json").exists():
        e["finale"].update(statut="fait", message="repris après un arrêt (coût inconnu)")
        change = True
    if change:
        _ecrire(dossier / "etat.json", e)
    return e


# ---------------------------------------------------------------------------
# Validation des parties enregistrées par l'agent et du rapport
# ---------------------------------------------------------------------------

def _textes(valeur):
    if isinstance(valeur, str):
        yield valeur
    elif isinstance(valeur, dict):
        for v in valeur.values():
            yield from _textes(v)
    elif isinstance(valeur, list):
        for v in valeur:
            yield from _textes(v)


def sensibles(texte: str) -> list:
    """Motifs refusés dans tout le rapport : secrets, e-mails, téléphones, chaînes de connexion, clés privées."""
    problemes = []
    if remaining_secrets(texte) or _SECRET_EN_CLAIR.search(texte) or _CLE_PRIVEE.search(texte):
        problemes.append("ce qui ressemble à un secret (mot de passe, jeton, clé)")
    if _EMAIL.search(texte):
        problemes.append("une adresse e-mail")
    if _TELEPHONE.search(texte):
        problemes.append("un numéro de téléphone")
    if _CONNEXION.search(texte):
        problemes.append("une chaîne de connexion")
    return problemes


def _ligne_sourcee(ligne: dict, ou: str, erreurs: list, champ_source: str = "sources") -> None:
    sources = ligne.get(champ_source)
    if isinstance(sources, str):
        sources = [sources]
    if not sources or not any(str(s).strip() for s in sources):
        erreurs.append(f"{ou} : source manquante (fichier et ligne, table et colonne, clé de ticket).")
    if str(ligne.get("statut", "")).strip().lower() not in STATUTS:
        erreurs.append(f"{ou} : statut « {ligne.get('statut', '')} » invalide ({' ou '.join(STATUTS)}).")


def valider_lot(partie: dict, attendus: list) -> list:
    """Problèmes d'un lot (liste vide = accepté) : chaque élément attendu expliqué (objectif, composants,
    sources, statut) ou déclaré non expliqué avec la raison ; aucun élément étranger ; aucun motif sensible."""
    if not isinstance(partie, dict):
        return ["Le lot doit être un objet JSON."]
    erreurs = []
    expliques = {str(e.get("id")): e for e in partie.get("elements") or [] if isinstance(e, dict)}
    non = {str(e.get("id")): e for e in partie.get("non_expliques") or [] if isinstance(e, dict)}
    for ident, e in expliques.items():
        if not str(e.get("objectif", "")).strip():
            erreurs.append(f"Élément {ident} : objectif fonctionnel vide.")
        if not e.get("composants"):
            erreurs.append(f"Élément {ident} : composants (fichiers) manquants.")
        _ligne_sourcee(e, f"Élément {ident}", erreurs)
    for ident, e in non.items():
        if not str(e.get("raison", "")).strip():
            erreurs.append(f"Élément {ident} non expliqué : donne la raison et où chercher.")
    inconnus = sorted((set(expliques) | set(non)) - set(attendus))
    if inconnus:
        erreurs.append(f"Éléments hors de ce lot : {', '.join(inconnus)}. N'en invente pas.")
    manquants = [i for i in attendus if i not in expliques and i not in non]
    if manquants:
        erreurs.append(f"Éléments sans réponse : {', '.join(manquants)} (explique-les ou mets-les dans "
                       f"non_expliques avec la raison).")
    for i, c in enumerate(partie.get("constats") or [], start=1):
        _constat(c, f"Constat {i}", erreurs)
    problemes = sensibles("\n".join(_textes(partie)))
    if problemes:
        erreurs.append(f"Le lot contient {', '.join(problemes)} : retire-le.")
    return erreurs


def _constat(c, ou: str, erreurs: list) -> None:
    if not isinstance(c, dict) or not str(c.get("constat", "")).strip():
        erreurs.append(f"{ou} : texte du constat vide.")
        return
    if str(c.get("severite", "")).lower() not in SEVERITES:
        erreurs.append(f"{ou} : sévérité « {c.get('severite', '')} » invalide ({', '.join(SEVERITES)}).")
    _ligne_sourcee(c, ou, erreurs, "preuves")


def valider_finale(partie: dict) -> list:
    """Problèmes de la partie finale : contexte, configuration et audit sourcés, avec statut ; points à vérifier
    avec l'endroit où chercher ; aucun motif sensible."""
    if not isinstance(partie, dict):
        return ["La partie finale doit être un objet JSON."]
    erreurs = []
    for i, ligne in enumerate(partie.get("contexte") or [], start=1):
        if not str(ligne.get("sujet", "")).strip() or not str(ligne.get("constat", "")).strip():
            erreurs.append(f"Contexte, ligne {i} : sujet et constat obligatoires.")
        _ligne_sourcee(ligne, f"Contexte, ligne {i}", erreurs)
    for i, ligne in enumerate(partie.get("configuration") or [], start=1):
        if not str(ligne.get("parametre", "")).strip():
            erreurs.append(f"Configuration, ligne {i} : paramètre vide.")
        _ligne_sourcee(ligne, f"Configuration, ligne {i}", erreurs)
    for i, c in enumerate(partie.get("audit") or [], start=1):
        _constat(c, f"Audit, constat {i}", erreurs)
    for i, p in enumerate(partie.get("points_a_verifier") or [], start=1):
        if not str(p.get("point", "")).strip() or not str(p.get("ou_chercher", "")).strip():
            erreurs.append(f"Point à vérifier {i} : le point et l'endroit où chercher sont obligatoires.")
    if not partie.get("contexte") and not partie.get("configuration") and not partie.get("points_a_verifier"):
        erreurs.append("Partie vide : renseigne au moins le contexte, la configuration ou les points à vérifier.")
    problemes = sensibles("\n".join(_textes(partie)))
    if problemes:
        erreurs.append(f"La partie finale contient {', '.join(problemes)} : retire-le.")
    return erreurs


def _normaliser(partie: dict) -> dict:
    """Statuts et sévérités en minuscules ; sources et preuves toujours en liste."""
    for lignes in partie.values():
        for ligne in lignes:
            for cle in ("statut", "severite"):
                if cle in ligne:
                    ligne[cle] = str(ligne[cle]).strip().lower()
            for cle in ("sources", "preuves", "composants"):
                if isinstance(ligne.get(cle), str):
                    ligne[cle] = [ligne[cle]]
    return partie


def enregistrer_lot(dossier: Path, numero: int, partie: dict) -> dict:
    e = etat(dossier)
    lot = next((l for l in e["lots"] if l["numero"] == numero), None)
    if lot is None:
        raise SyntheseErreur(f"Lot {numero} inconnu.")
    erreurs = valider_lot(partie, lot["elements"])
    if erreurs:
        raise SyntheseErreur("Lot refusé, corrige puis renvoie-le en entier :\n- " + "\n- ".join(erreurs))
    partie = _normaliser({k: partie.get(k) or [] for k in ("elements", "non_expliques", "constats",
                                                           "points_a_verifier")})
    _ecrire(Path(dossier) / "lots" / f"{numero:03d}.json", partie)
    return partie


def enregistrer_finale(dossier: Path, partie: dict) -> dict:
    erreurs = valider_finale(partie)
    if erreurs:
        raise SyntheseErreur("Partie finale refusée, corrige puis renvoie-la en entier :\n- " + "\n- ".join(erreurs))
    partie = _normaliser({k: partie.get(k) or [] for k in ("contexte", "configuration", "audit",
                                                           "points_a_verifier")})
    _ecrire(Path(dossier) / "finale.json", partie)
    return partie


# --- schémas des outils (synthese_enregistrer) ------------------------------------------------------------------

_T = {"type": "string"}
_L = {"type": "array", "items": {"type": "string"}}
_STATUT = {"type": "string", "enum": list(STATUTS)}
_CONSTAT = {"type": "object", "properties": {
    "constat": _T, "severite": {"type": "string", "enum": list(SEVERITES)},
    "preuves": {**_L, "description": "fichier et ligne, table et colonne, clé de ticket"}, "statut": _STATUT},
    "required": ["constat", "severite", "preuves", "statut"]}
_POINT = {"type": "object", "properties": {"point": _T, "ou_chercher": _T}, "required": ["point", "ou_chercher"]}
SCHEMA_LOT = {"type": "object", "properties": {
    "elements": {"type": "array", "description": "un objet par élément expliqué", "items": {
        "type": "object", "properties": {
            "id": _T, "objectif": {"type": "string", "description": "objectif fonctionnel en une phrase, termes métier"},
            "composants": {**_L, "description": "fichiers principaux (chemin relatif à la source)"},
            "ticket": {"type": "string", "description": "clé Jira d'origine si retrouvée, sinon vide"},
            "sources": {**_L, "description": "preuves : fichier et ligne, table et colonne, clé de ticket"},
            "statut": _STATUT}, "required": ["id", "objectif", "composants", "sources", "statut"]}},
    "non_expliques": {"type": "array", "items": {"type": "object", "properties": {"id": _T, "raison": _T},
                                                 "required": ["id", "raison"]}},
    "constats": {"type": "array", "description": "incohérences prouvées vues dans ce lot", "items": _CONSTAT},
    "points_a_verifier": {"type": "array", "items": _POINT}}, "required": ["elements"]}
SCHEMA_FINALE = {"type": "object", "properties": {
    "contexte": {"type": "array", "description": "architecture, environnements, modules utilisés, périmètre",
                 "items": {"type": "object", "properties": {"sujet": _T, "constat": _T, "sources": _L,
                                                            "statut": _STATUT},
                           "required": ["sujet", "constat", "sources", "statut"]}},
    "configuration": {"type": "array", "items": {"type": "object", "properties": {
        "domaine": _T, "parametre": _T, "valeur": _T, "sources": _L, "statut": _STATUT},
        "required": ["parametre", "valeur", "sources", "statut"]}},
    "audit": {"type": "array", "items": _CONSTAT},
    "points_a_verifier": {"type": "array", "items": _POINT}}, "required": ["contexte", "configuration"]}


# ---------------------------------------------------------------------------
# Données transmises à l'agent (marches à suivre de consignes.py)
# ---------------------------------------------------------------------------

def donnees_lot(dossier: Path, numero: int) -> str:
    """Éléments du lot à expliquer, en texte borné (une DONNÉE pour l'agent)."""
    inv, e = inventaire(dossier), etat(dossier)
    lot = next(l for l in e["lots"] if l["numero"] == numero)
    par_id = {el["id"]: el for el in inv["elements"]}
    blocs = []
    for ident in lot["elements"]:
        el = par_id[ident]
        ou = f"dépôt Bitbucket {el['depot']}, branche {el.get('branche') or '(défaut)'}" if el.get("depot") \
            else f"source {el['source']}"
        lignes = [f"### {ident} — {el['categorie']} · {el['unite']} ({ou}, {el['nature']}, "
                  f"{el['nb_fichiers']} fichier(s))"]
        for f in el["fichiers"][:FICHIERS_PAR_ELEMENT]:
            lignes.append(f"- {f['chemin']} [{f['type']}]" + (f" ← Standard : {f['standard']}" if f.get("standard")
                                                               else ""))
        if el["nb_fichiers"] > FICHIERS_PAR_ELEMENT:
            lignes.append(f"- … {el['nb_fichiers'] - FICHIERS_PAR_ELEMENT} autre(s) fichier(s)")
        if el["tickets"]:
            lignes.append("Tickets cités dans ces fichiers : " + ", ".join(el["tickets"]))
        blocs.append("\n".join(lignes))
    return f"Lot {numero}/{len(e['lots'])} — {len(lot['elements'])} élément(s)\n\n" + "\n\n".join(blocs)


def donnees_finale(dossier: Path) -> str:
    """Résumé de l'inventaire et des lots pour la session finale (une DONNÉE pour l'agent)."""
    inv = inventaire(dossier)
    v = inv.get("version") or {}
    lignes = [f"Projet : {inv['projet']}",
              f"Version du Standard (pom.xml) : {v['version']} — {v['detail']}, {v['source']}" if v.get("version")
              else "Version du Standard : introuvable dans les pom.xml.",
              f"Éléments spécifiques inventoriés : {len(inv['elements'])} ; fichiers identiques au Standard ignorés : "
              f"{inv['fichiers_identiques']}."]
    lignes += [f"Source {'Standard' if s is inv.get('standard') else 'spécifique'} : {_libelle_source(s)}"
               for s in ([inv["standard"]] if inv.get("standard") else []) + (inv.get("specifiques") or [])]
    if inv.get("bitbucket"):
        lignes.append("Sources lues sur Bitbucket (non clonées) : bitbucket_fichiers et bitbucket_lire avec le dépôt "
                      "« PROJET/depot » et la branche.")
    if inv.get("limites"):
        lignes.append(f"Limites de l'inventaire : {len(inv['limites'])} (déjà reportées dans les points à vérifier).")
    par_categorie = {}
    for el in inv["elements"]:
        par_categorie[el["categorie"]] = par_categorie.get(el["categorie"], 0) + 1
    lignes.append("Par catégorie : " + ", ".join(f"{c} {n}" for c, n in par_categorie.items()))
    lignes.append("\nÉléments expliqués par les lots (objectif) :")
    for el in elements_expliques(dossier)[:120]:
        lignes.append(f"- {el['id']} [{el['categorie']}] {el['unite']} : {el['objectif'][:200]}")
    constats = [c for p in parties_lots(dossier) for c in p.get("constats") or []]
    if constats:
        lignes.append("\nConstats déjà relevés par les lots (à reprendre, compléter ou écarter) :")
        lignes += [f"- [{c['severite']}] {c['constat'][:300]}" for c in constats[:40]]
    return "\n".join(lignes)


def parties_lots(dossier: Path) -> list:
    return [p for f in sorted((Path(dossier) / "lots").glob("*.json")) if isinstance(p := _lire(f), dict)]


def elements_expliques(dossier: Path) -> list:
    par_id = {el["id"]: el for el in inventaire(dossier)["elements"]}
    return [{**par_id[e["id"]], **e} for p in parties_lots(dossier) for e in p.get("elements") or []
            if e.get("id") in par_id]


# ---------------------------------------------------------------------------
# Déroulé (lots puis finale), indépendant de l'IHM
# ---------------------------------------------------------------------------

QUESTION_LOT = ("Synthèse client, lot {numero} : appelle synthese_demarrer pour recevoir la marche à suivre et les "
                "éléments de ce lot, enquête, puis appelle synthese_enregistrer avec la réponse complète du lot.")
QUESTION_FINALE = ("Synthèse client, partie finale : appelle synthese_demarrer pour recevoir la marche à suivre et "
                   "le résumé de l'inventaire, enquête, puis appelle synthese_enregistrer avec la partie finale.")


async def executer(dossier: Path, creer_session, sur_etape=lambda texte: None) -> dict:
    """Exécute les étapes restantes d'une synthèse (reprise comprise) puis assemble le rapport.

    ``creer_session(mode, numero)`` renvoie une session à ouvrir et fermer (session.Session avec synthese=…) ;
    ``sur_etape(texte)`` reçoit la progression. L'état est écrit après chaque lot : un arrêt ne perd que le lot en
    cours. Un lot en échec n'est pas relancé dans la même exécution ; la partie finale est faite quand tous les
    lots ont été tentés, et refaite si un lot est terminé après elle. Le rapport est produit dès que la partie
    finale est faite (« partiel » si des lots restent à refaire). Renvoie l'état final."""
    dossier = Path(dossier)
    reconcilier(dossier)
    tentes = set()
    while True:
        e = etat(dossier)
        etape, numero = prochaine_etape(e, tentes)
        if etape == "fini":
            break
        tentes.add((etape, numero))
        libelle = f"lot {numero}/{len(e['lots'])}" if etape == "lot" else "partie finale"
        sur_etape(f"Synthèse : {libelle}…")
        debut = time.monotonic()
        fichier = dossier / "lots" / f"{numero:03d}.json" if etape == "lot" else dossier / "finale.json"
        avant = fichier.stat().st_mtime_ns if fichier.exists() else None
        session = creer_session(etape, numero)
        await session.ouvrir()
        try:
            bilan = await session.demander(QUESTION_LOT.format(numero=numero) if etape == "lot" else QUESTION_FINALE,
                                           lambda _: None, lambda nom, entree: sur_etape(_outil(nom, entree)))
        finally:
            await session.fermer()
        e = etat(dossier)
        cible = next(l for l in e["lots"] if l["numero"] == numero) if etape == "lot" else e["finale"]
        enregistre = fichier.exists() and fichier.stat().st_mtime_ns != avant
        cible.update(statut="fait" if enregistre else "erreur", cout_usd=bilan.cout_usd,
                     duree_s=round(time.monotonic() - debut, 1),
                     message="" if enregistre else (bilan.message or "aucune réponse enregistrée par l'agent"))
        if etape == "lot" and enregistre and e["finale"].get("statut") == "fait":
            e["finale"]["statut"] = "a_faire"       # la partie finale doit intégrer ce lot
        e["cout_usd"] = round((e.get("cout_usd") or 0) + (bilan.cout_usd or 0), 4)
        e["duree_s"] = round((e.get("duree_s") or 0) + cible["duree_s"], 1)
        _ecrire(dossier / "etat.json", e)
        if not enregistre:
            sur_etape(f"Synthèse : {libelle} non enregistré ({cible['message']}) ; « Reprendre » le relance.")
    e = etat(dossier)
    if e["finale"].get("statut") == "fait":
        rendu = produire(dossier)
        e = etat(dossier)
        e.update(termine=all(l["statut"] == "fait" for l in e["lots"]),
                 fichiers={k: str(v) for k, v in rendu.items() if v})
        e["partiel"] = not e["termine"]
        _ecrire(dossier / "etat.json", e)
    return e


def _outil(nom: str, entree: dict) -> str:
    from .etapes import resumer
    return resumer(nom, entree, [])


# ---------------------------------------------------------------------------
# Rapport : assemblage, contrôle, Markdown et Word
# ---------------------------------------------------------------------------

def _libelle_source(s: dict) -> str:
    """Source lue : dossier local, dépôt Git (commit) ou source Bitbucket (branches, dépôts lus)."""
    if s.get("bitbucket"):
        depots = s.get("depots") or []
        lus = [d for d in depots if d.get("commit")]
        branches = s.get("branches") or []
        texte = (f"{s['source']} — Bitbucket, {s['bitbucket']}, branche "
                 f"{', '.join(branches[:4]) + ('…' if len(branches) > 4 else '') if branches else 'inconnue'}, "
                 f"{len(lus)}/{len(depots)} dépôt(s) lu(s), sans clonage")
        if len(depots) == 1 and lus:
            texte += f" @ {lus[0]['commit']} ({lus[0].get('date', '')[:10]})"
        return texte
    return s["source"] + (f" @ {s['commit']} ({s['date'][:10]})" if s.get("commit") else " (dossier local)")


def _cellule(valeur) -> str:
    if isinstance(valeur, list):
        valeur = " ; ".join(str(v) for v in valeur if str(v).strip())
    texte = " ".join(str(valeur or "").split()).replace("|", "\\|")
    return texte if len(texte) <= TEXTE_MAX else texte[:TEXTE_MAX - 1] + "…"


def assembler(dossier: Path) -> list:
    """Le rapport en blocs : [("titre", niveau, texte) | ("paragraphe", texte) | ("tableau", entêtes, lignes) |
    ("liste", éléments)]. Les tableaux des sections 1 à 4 ont toujours les colonnes Source et Statut."""
    dossier = Path(dossier)
    inv, e = inventaire(dossier), etat(dossier)
    finale = _lire(dossier / "finale.json", {}) or {}
    lots = parties_lots(dossier)
    expliques = elements_expliques(dossier)
    ids_expliques = {el["id"] for el in expliques}
    blocs = [("titre", 1, f"Synthèse client — {inv['projet']}"),
             ("paragraphe", f"Généré le {e.get('cree_le', '')[:16].replace('T', ' à ')} par l'agent Odatio, en lecture "
                            "seule. Chaque ligne cite sa source ; « déduit » signale un raisonnement, non une lecture "
                            "directe. À relire avant envoi au support.")]

    # 1. Identité et contexte
    v = inv.get("version") or {}
    lignes = [["Projet", inv["projet"], "profil de l'agent Odatio", "vérifié"]]
    if inv.get("standard"):
        lignes.append(["Source Standard", _libelle_source(inv["standard"]), "sources du profil", "vérifié"])
    for s in inv.get("specifiques") or []:
        lignes.append(["Source spécifique", _libelle_source(s), "sources du profil", "vérifié"])
        for d in (s.get("depots") or []) if len(s.get("depots") or []) > 1 else []:
            if d.get("commit"):
                lignes.append(["Dépôt spécifique", f"{d['depot']} @ {d.get('branche', '')}, commit {d['commit']} "
                               f"({d.get('date', '')[:10]})", "Bitbucket", "vérifié"])
    if v.get("version"):
        detail = v["detail"] + (f" ; autres versions citées : {', '.join(v['divergentes'])}" if v.get("divergentes")
                                else "")
        if v.get("branche_deduite"):
            detail += f" ; Standard lu sur la branche {v['branche_deduite']}, déduite de cette version"
        lignes.append(["Version du Standard", f"{v['version']} ({v['complete']}) — {detail}", v["source"],
                       "vérifié" if "pas citée" not in v["detail"] else "déduit"])
    for c in finale.get("contexte") or []:
        lignes.append([c["sujet"], c["constat"], c["sources"], c["statut"]])
    blocs += [("titre", 2, f"1. {SECTIONS[0]}"), ("tableau", ["Sujet", "Constat", "Source", "Statut"], lignes)]

    # 2. Développements spécifiques
    blocs.append(("titre", 2, f"2. {SECTIONS[1]}"))
    for cat in CATEGORIES:
        lignes = [[el["unite"], el["objectif"], el["composants"], el["nature"], el.get("ticket") or "—",
                   el["sources"], el["statut"]] for el in expliques if el["categorie"] == cat]
        if lignes:
            blocs += [("titre", 3, cat[0].upper() + cat[1:]),
                      ("tableau", ["Élément", "Objectif fonctionnel", "Composants", "Surcharge / ajout",
                                   "Ticket d'origine", "Source", "Statut"], lignes)]
    if not expliques:
        blocs.append(("paragraphe", "Aucun élément expliqué : voir les points à vérifier."))

    # 3. Configuration en place
    blocs.append(("titre", 2, f"3. {SECTIONS[2]}"))
    configuration = finale.get("configuration") or []
    if configuration:
        blocs.append(("tableau", ["Domaine", "Paramètre", "Valeur", "Source", "Statut"],
                      [[c.get("domaine") or "—", c["parametre"], c["valeur"], c["sources"], c["statut"]]
                       for c in configuration]))
    else:
        blocs.append(("paragraphe", "Aucun paramétrage relevé : voir les points à vérifier."))

    # 4. Audit
    blocs.append(("titre", 2, f"4. {SECTIONS[3]}"))
    constats = [c for p in lots for c in p.get("constats") or []] + list(finale.get("audit") or [])
    vus, uniques = set(), []
    for c in constats:
        cle = " ".join(c["constat"].lower().split())
        if cle not in vus:
            vus.add(cle)
            uniques.append(c)
    uniques.sort(key=lambda c: SEVERITES.index(c["severite"].lower()) if c["severite"].lower() in SEVERITES else 9)
    if uniques:
        blocs.append(("tableau", ["Sévérité", "Constat", "Source", "Statut"],
                      [[c["severite"], c["constat"], c["preuves"], c["statut"]] for c in uniques]))
    else:
        blocs.append(("paragraphe", "Aucune incohérence prouvée relevée."))

    # 5. Points à vérifier
    points = []
    par_id = {el["id"]: el for el in inv["elements"]}
    for p in lots:
        for n in p.get("non_expliques") or []:
            el = par_id.get(n["id"], {})
            points.append(f"{el.get('categorie', '')} · {el.get('unite', n['id'])} ({el.get('source', '')}) : non "
                          f"expliqué — {n['raison']}")
    for lot in e["lots"]:
        if lot["statut"] != "fait":
            for ident in lot["elements"]:
                if ident not in ids_expliques:
                    el = par_id.get(ident, {})
                    points.append(f"{el.get('categorie', '')} · {el.get('unite', ident)} : lot {lot['numero']} non "
                                  f"traité ({lot.get('message') or 'à reprendre'})")
    for p in [x for lot in lots for x in lot.get("points_a_verifier") or []] + list(finale.get("points_a_verifier")
                                                                                   or []):
        points.append(f"{p['point']} — où chercher : {p['ou_chercher']}")
    if not v.get("version"):
        points.append("Version du Standard introuvable dans les pom.xml — où chercher : pom.xml du spécifique, "
                      "builds Jenkins du client ou équipe projet.")
    points += [f"Limite de l'inventaire : {l}" for l in inv.get("limites") or []]
    points = list(dict.fromkeys(_cellule(p) for p in points))
    blocs += [("titre", 2, f"5. {SECTIONS[4]}"), ("liste", points or ["Aucun."])]

    # 6. Couverture
    total = len(inv["elements"])
    nb_non = sum(len(p.get("non_expliques") or []) for p in lots)
    couverture = [f"Éléments de l'inventaire expliqués : {len(ids_expliques)} / {total}"
                  + (f" ({round(100 * len(ids_expliques) / total)} %)" if total else ""),
                  f"Éléments déclarés non expliqués par l'agent : {nb_non}",
                  f"Lots : {sum(1 for l in e['lots'] if l['statut'] == 'fait')} / {len(e['lots'])} faits ; partie "
                  f"finale : {'faite' if e['finale'].get('statut') == 'fait' else 'non faite'}",
                  f"Fichiers identiques au Standard ignorés : {inv.get('fichiers_identiques', 0)}",
                  "Sources lues : " + " ; ".join(
                      _libelle_source(s)
                      for s in ([inv["standard"]] if inv.get("standard") else []) + (inv.get("specifiques") or [])),
                  f"Inventaire du {inv['genere_le'][:16].replace('T', ' à ')}"]
    if inv.get("bitbucket"):
        b = inv["bitbucket"]
        couverture.append(f"Inventaire lu sur Bitbucket, sans clonage : {b['appels']} appel(s), {b['depots']} "
                          f"dépôt(s), {b['fichiers_listes']} fichier(s) listé(s), {b['comparaisons']} comparé(s) au "
                          f"Standard, {b['lectures']} lu(s) pour les tickets, {b['duree_s']} s")
    limites = inv.get("limites") or []
    couverture.append(f"Limites de l'inventaire : {len(limites)} (détail dans les points à vérifier)" if limites
                      else "Limites de l'inventaire : aucune")
    couverture.append(f"Coût : {e.get('cout_usd') or 0:.2f} $ ; durée : {round((e.get('duree_s') or 0) / 60, 1)} "
                      "min")
    blocs += [("titre", 2, f"6. {SECTIONS[5]}"), ("liste", couverture)]
    return blocs


def en_markdown(blocs: list) -> str:
    lignes = []
    for bloc in blocs:
        if bloc[0] == "titre":
            lignes += ["#" * bloc[1] + " " + bloc[2], ""]
        elif bloc[0] == "paragraphe":
            lignes += [bloc[1], ""]
        elif bloc[0] == "liste":
            lignes += [f"- {x}" for x in bloc[1]] + [""]
        elif bloc[0] == "tableau":
            lignes += ["| " + " | ".join(bloc[1]) + " |", "|" + "---|" * len(bloc[1])]
            lignes += ["| " + " | ".join(_cellule(c) for c in ligne) + " |" for ligne in bloc[2]] + [""]
    return "\n".join(lignes)


def valider_rapport(markdown: str) -> list:
    """Contrôle mécanique du rapport final : six sections présentes ; dans chaque tableau, une colonne Source et
    une colonne Statut, renseignées sur chaque ligne (statut vérifié ou déduit) ; aucun motif sensible."""
    erreurs = []
    for i, section in enumerate(SECTIONS, start=1):
        if not re.search(rf"^## {i}\. {re.escape(section)}\s*$", markdown, re.M):
            erreurs.append(f"Section manquante : {i}. {section}")
    entetes = None
    for numero, ligne in enumerate(markdown.splitlines(), start=1):
        if not ligne.startswith("|"):
            entetes = None
            continue
        cellules = [c.strip() for c in re.split(r"(?<!\\)\|", ligne.strip())[1:-1]]
        if entetes is None:
            entetes = cellules
            if "Source" not in entetes or "Statut" not in entetes:
                erreurs.append(f"Ligne {numero} : tableau sans colonne Source ou Statut.")
            continue
        if all(set(c) <= {"-", ":"} for c in cellules):
            continue
        if "Source" in entetes and "Statut" in entetes and len(cellules) == len(entetes):
            source, statut = cellules[entetes.index("Source")], cellules[entetes.index("Statut")]
            if not source or source == "—":
                erreurs.append(f"Ligne {numero} : source manquante.")
            if statut not in STATUTS:
                erreurs.append(f"Ligne {numero} : statut « {statut} » invalide.")
        elif len(cellules) != len(entetes):
            erreurs.append(f"Ligne {numero} : nombre de colonnes incorrect.")
    problemes = sensibles(markdown)
    if problemes:
        erreurs.append(f"Le rapport contient {', '.join(problemes)}.")
    return erreurs


def en_word(blocs: list, chemin: Path) -> Path | None:
    """Document Word (python-docx) ; None si python-docx n'est pas installé."""
    try:
        from docx import Document
        from docx.shared import Pt
    except ImportError:
        return None
    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(10)
    for bloc in blocs:
        if bloc[0] == "titre":
            doc.add_heading(bloc[2], level=0 if bloc[1] == 1 else bloc[1] - 1)
        elif bloc[0] == "paragraphe":
            doc.add_paragraph(bloc[1])
        elif bloc[0] == "liste":
            for x in bloc[1]:
                doc.add_paragraph(x, style="List Bullet")
        elif bloc[0] == "tableau":
            table = doc.add_table(rows=1, cols=len(bloc[1]))
            table.style = "Light Grid Accent 1"
            for cellule, texte in zip(table.rows[0].cells, bloc[1]):
                cellule.text = texte
            for ligne in bloc[2]:
                for cellule, valeur in zip(table.add_row().cells, ligne):
                    cellule.text = _cellule(valeur).replace("\\|", "|")
    doc.save(chemin)
    return chemin


def produire(dossier: Path) -> dict:
    """Assemble, contrôle puis écrit rapport.md et rapport.docx ; SyntheseErreur si le contrôle échoue."""
    dossier = Path(dossier)
    blocs = assembler(dossier)
    markdown = en_markdown(blocs)
    erreurs = valider_rapport(markdown)
    if erreurs:
        raise SyntheseErreur("Rapport refusé par le contrôle :\n- " + "\n- ".join(erreurs))
    (dossier / "rapport.md").write_text(markdown, encoding="utf-8")
    return {"markdown": dossier / "rapport.md", "word": en_word(blocs, dossier / "rapport.docx")}
