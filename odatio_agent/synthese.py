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
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from tools.secret_tools import remaining_secrets

from . import config
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


def _tickets(fichier: Path) -> list:
    try:
        with open(fichier, "rb") as f:
            texte = f.read(TAILLE_LECTURE).decode("utf-8", errors="ignore")
    except OSError:
        return []
    return [f"{p}-{n}" for p, n in TICKET.findall(texte) if p not in FAUX_TICKETS and not p.startswith("X")]


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


def groupes_standard(racine: Path) -> set:
    """groupIds du Standard : ceux de ses pom.xml (racine et modules), jamais en dur."""
    groupes = set()
    for fichier in _poms(racine, 2):
        racine_pom = _pom(fichier)
        if racine_pom is not None:
            groupe = _texte(racine_pom, "groupId") or _texte(_enfant(racine_pom, "parent"), "groupId")
            if groupe:
                groupes.add(groupe)
    return groupes


def versions_standard(nom_source: str, racine: Path, groupes: set) -> list:
    """Versions d'artefacts du Standard citées par les pom.xml d'une source spécifique :
    [{version, complete, artefact, emplacement, fichier}] (propriétés ${…} résolues)."""
    trouvees = []
    for fichier in _poms(racine):
        pom = _pom(fichier)
        if pom is None:
            continue
        proprietes = _enfant(pom, "properties")
        props = {_balise(p): (p.text or "").strip() for p in (proprietes if proprietes is not None else [])}
        relatif = f"{nom_source}/{fichier.relative_to(racine).as_posix()}"

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


def version(standard: tuple | None, specifiques: list) -> dict:
    """Version du Standard déployée : la plus citée par les pom.xml des sources spécifiques (parent d'abord) ;
    à défaut, celle du pom.xml de la source Standard (branche suivie par le profil). {} si introuvable."""
    if standard is None:
        return {}
    nom_std, racine_std = standard
    groupes = groupes_standard(racine_std)
    citees = [v for nom, racine in specifiques for v in versions_standard(nom, racine, groupes)]
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
    pom = _pom(racine_std / "pom.xml")
    brute = (_texte(pom, "version") or _texte(_enfant(pom, "parent"), "version")) if pom is not None else ""
    m = _VERSION.search(brute)
    if m:
        resultat.update(version=f"{m.group(1)}.{m.group(2)}", complete=brute, source=f"{nom_std}/pom.xml",
                        detail="version du pom de la source Standard (pas citée par le spécifique)", divergentes=[])
    return resultat


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
        lignes = [f"### {ident} — {el['categorie']} · {el['unite']} (source {el['source']}, {el['nature']}, "
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
        s = inv["standard"]
        lignes.append(["Source Standard", s["source"] + (f" @ {s['commit']} ({s['date'][:10]})" if s.get("commit")
                                                         else " (dossier local)"), "sources du profil", "vérifié"])
    for s in inv.get("specifiques") or []:
        lignes.append(["Source spécifique", s["source"] + (f" @ {s['commit']} ({s['date'][:10]})" if s.get("commit")
                                                           else " (dossier local)"), "sources du profil", "vérifié"])
    if v.get("version"):
        detail = v["detail"] + (f" ; autres versions citées : {', '.join(v['divergentes'])}" if v.get("divergentes")
                                else "")
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
                  "Sources lues : " + ", ".join(
                      f"{s['source']}" + (f" @ {s['commit']} du {s['date'][:10]}" if s.get("commit") else "")
                      for s in ([inv["standard"]] if inv.get("standard") else []) + (inv.get("specifiques") or [])),
                  f"Inventaire du {inv['genere_le'][:16].replace('T', ' à ')}",
                  f"Coût : {e.get('cout_usd') or 0:.2f} $ ; durée : {round((e.get('duree_s') or 0) / 60, 1)} min"]
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
