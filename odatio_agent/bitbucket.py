"""
Bitbucket Server / Data Center en lecture seule (API REST 1.0), pour que l'agent aille chercher sur Bitbucket ce
que les sources clonées du profil ne contiennent pas : la branche d'une tâche pas encore fusionnée et ce qu'elle
change, une autre branche d'un dépôt du profil, un autre dépôt (bibliothèque partagée, autre client).

Authentification : le même jeton HTTP que pour le clonage (BITBUCKET_HTTP_TOKEN), en en-tête Bearer, jamais dans
l'URL. Uniquement des requêtes GET (et le POST de la recherche de code, qui ne modifie rien). Adresse du serveur :
BITBUCKET_URL, ou à défaut celle des dépôts du profil. BITBUCKET_PROJETS (facultatif) restreint les projets lisibles.

Toutes les fonctions renvoient un texte borné et masqué, ou lèvent BitbucketErreur avec un message sans secret.
Les fichiers interdits du profil (secrets, clés…) ne sont jamais lus, ni affichés dans un diff.
"""
import re
from datetime import datetime
from fnmatch import fnmatch
from urllib.parse import quote, urlsplit

import requests

from tools.secret_tools import mask_secrets, register_secrets

from . import config

API = "/rest/api/1.0"
DELAI = 30                  # secondes par requête
LIGNES_MAX = 400            # lignes d'un fichier renvoyées en une fois
CARACTERES_MAX = 40000      # taille d'une réponse donnée à l'agent
FICHIERS_MAX = 300          # fichiers listés
DEPOTS_MAX = 60             # dépôts parcourus pour chercher des branches dans tout un projet
DIFF_LIGNES_MAX = 600       # lignes d'un diff


class BitbucketErreur(Exception):
    pass


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _hote(url: str) -> str:
    parties = urlsplit(url.strip())
    return f"{parties.scheme}://{parties.netloc}" if parties.scheme in ("http", "https") and parties.netloc else ""


def adresse(profil: config.Profil | None = None) -> str:
    """Adresse du serveur : BITBUCKET_URL, sinon l'hôte du premier dépôt HTTP(S) du profil."""
    url = config.secret("BITBUCKET_URL")
    if url:
        url = url if "://" in url else "https://" + url
        chemin = urlsplit(url).path.rstrip("/")
        return (_hote(url) + chemin) if _hote(url) else ""
    for source in (profil.sources if profil else []):
        if source.depot and _hote(source.depot):
            depot = source.depot
            racine = depot.split("/scm/", 1)[0] if "/scm/" in depot else _hote(depot)
            return racine.rstrip("/")
    return ""


def configure(profil: config.Profil | None = None) -> bool:
    return bool(config.secret("BITBUCKET_HTTP_TOKEN") and adresse(profil))


def projets_autorises() -> set:
    return {p.strip().upper() for p in re.split(r"[,;\s]+", config.secret("BITBUCKET_PROJETS")) if p.strip()}


# ---------------------------------------------------------------------------
# Appels HTTP
# ---------------------------------------------------------------------------

def _get(profil, chemin: str, params: dict | None = None, brut: bool = False):
    racine = adresse(profil)
    jeton = config.secret("BITBUCKET_HTTP_TOKEN")
    if not racine or not jeton:
        raise BitbucketErreur("Bitbucket n'est pas configuré (onglet Configuration, Accès).")
    register_secrets([jeton])
    try:
        reponse = requests.get(racine + chemin, params=params or {}, timeout=DELAI,
                               headers={"Authorization": f"Bearer {jeton}", "Accept": "application/json"})
    except requests.exceptions.SSLError:
        raise BitbucketErreur("Certificat du serveur Bitbucket refusé : si ton entreprise a sa propre autorité de "
                              "certification, indique son fichier dans la variable REQUESTS_CA_BUNDLE.")
    except requests.RequestException as e:
        raise BitbucketErreur(f"Bitbucket injoignable ({type(e).__name__}).")
    if reponse.status_code == 401:
        raise BitbucketErreur("Bitbucket a refusé le jeton (HTTP 401) : jeton absent, expiré ou révoqué.")
    if reponse.status_code == 403:
        raise BitbucketErreur("Accès refusé par Bitbucket (HTTP 403) : le jeton n'a pas les droits sur ce dépôt.")
    if reponse.status_code == 404:
        raise BitbucketErreur(_message(reponse) or "Introuvable sur Bitbucket (HTTP 404) : dépôt, branche ou "
                                                   "chemin inexistant.")
    if reponse.status_code >= 400:
        raise BitbucketErreur(f"Bitbucket a répondu HTTP {reponse.status_code} : {_message(reponse)}")
    if brut:
        return reponse
    try:
        return reponse.json()
    except ValueError:
        raise BitbucketErreur("Réponse de Bitbucket illisible (est-ce bien l'adresse d'un Bitbucket Server ?).")


def _message(reponse) -> str:
    try:
        erreurs = reponse.json().get("errors") or []
        return mask_secrets("; ".join(e.get("message", "") for e in erreurs))[0][:300]
    except ValueError:
        return ""


def _pages(profil, chemin: str, params: dict | None = None, maximum: int = 500, taille: int = 100) -> list:
    """Valeurs d'une ressource paginée, jusqu'à maximum."""
    valeurs, debut = [], 0
    while len(valeurs) < maximum:
        page = _get(profil, chemin, {**(params or {}), "start": debut, "limit": min(taille, maximum - len(valeurs))})
        valeurs += page.get("values", [])
        if page.get("isLastPage", True) or page.get("nextPageStart") is None:
            break
        debut = page["nextPageStart"]
    return valeurs[:maximum]


def _borner(texte: str, maximum: int = CARACTERES_MAX) -> str:
    texte = mask_secrets(texte)[0]
    return texte if len(texte) <= maximum else texte[:maximum] + "\n[… réponse tronquée]"


# ---------------------------------------------------------------------------
# Dépôts et références
# ---------------------------------------------------------------------------

def _depuis_url(url: str) -> tuple | None:
    """(PROJET, depot) d'une URL de clone (…/scm/PROJ/depot.git) ou de navigation (…/projects/PROJ/repos/depot)."""
    m = re.search(r"/scm/([^/]+)/([^/]+?)(?:\.git)?/?$", url) \
        or re.search(r"/projects/([^/]+)/repos/([^/?#]+)", url)
    return (m.group(1).upper(), m.group(2)) if m else None


def resoudre(profil: config.Profil | None, depot: str) -> tuple:
    """(PROJET, depot, branche par défaut du profil) depuis un nom de source du profil, « PROJET/depot » ou une URL."""
    depot = (depot or "").strip()
    branche = ""
    for source in (profil.sources if profil else []):
        if source.depot and depot.lower() in (source.nom.lower(), source.depot.lower()):
            depot, branche = source.depot, source.branche
            break
    cible = _depuis_url(depot) if "/" in depot and ("://" in depot or "/scm/" in depot or "/projects/" in depot) else None
    if cible is None:
        m = re.fullmatch(r"([A-Za-z0-9_~.-]+)/([A-Za-z0-9_.-]+)", depot)
        if not m:
            noms = [s.nom for s in (profil.sources if profil else []) if s.depot]
            raise BitbucketErreur(f"Dépôt « {depot} » non reconnu : donne « PROJET/depot », une URL Bitbucket"
                                  + (f" ou une source du profil ({', '.join(noms)})" if noms else "") + ".")
        cible = (m.group(1).upper(), m.group(2))
    autorises = projets_autorises()
    if autorises and cible[0] not in autorises:
        raise BitbucketErreur(f"Projet Bitbucket {cible[0]} non autorisé (autorisés : {', '.join(sorted(autorises))}).")
    return cible[0], cible[1], branche


def _chemin_depot(projet: str, depot: str) -> str:
    return f"{API}/projects/{quote(projet)}/repos/{quote(depot)}"


def _ref(branche: str) -> str:
    branche = branche.strip()
    if not branche or branche.startswith("refs/") or re.fullmatch(r"[0-9a-f]{7,40}", branche):
        return branche
    return f"refs/heads/{branche}"


def _branche_ou_defaut(profil, projet: str, depot: str, branche: str, branche_profil: str) -> str:
    if branche.strip():
        return branche.strip()
    if branche_profil:
        return branche_profil
    try:
        return _get(profil, _chemin_depot(projet, depot) + "/branches/default").get("displayId", "")
    except BitbucketErreur:
        return ""


def _depots_du_profil(profil) -> list:
    cibles = []
    for source in (profil.sources if profil else []):
        if source.depot and _depuis_url(source.depot):
            try:
                projet, depot, _ = resoudre(profil, source.nom)
                cibles.append((projet, depot, source.nom))
            except BitbucketErreur:
                pass
    return cibles


def _interdit(chemin: str, profil) -> bool:
    nom = chemin.rsplit("/", 1)[-1].lower()
    return any(fnmatch(nom, m.lower()) for m in (profil.fichiers_interdits if profil else config.INTERDITS_PAR_DEFAUT))


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------

def depots(profil, recherche: str = "", projet: str = "") -> str:
    """Dépôts lisibles avec le jeton, filtrés par nom et/ou projet."""
    autorises = projets_autorises()
    projet = projet.strip().upper()
    if projet and autorises and projet not in autorises:
        raise BitbucketErreur(f"Projet Bitbucket {projet} non autorisé (autorisés : {', '.join(sorted(autorises))}).")
    if projet:
        valeurs = _pages(profil, f"{API}/projects/{quote(projet)}/repos", maximum=500)
        if recherche:
            valeurs = [v for v in valeurs if recherche.lower() in (v.get("slug", "") + v.get("name", "")).lower()]
    else:
        valeurs = _pages(profil, f"{API}/repos", {"name": recherche} if recherche else {}, maximum=500)
    if autorises:
        valeurs = [v for v in valeurs if v.get("project", {}).get("key", "").upper() in autorises]
    lignes = [f"- {v['project']['key']}/{v['slug']}" + (f" — {v['name']}" if v.get("name") != v.get("slug") else "")
              + (f" : {v['description'][:120]}" if v.get("description") else "") for v in valeurs[:200]]
    if len(valeurs) > 200:
        lignes.append(f"[… {len(valeurs) - 200} autres : précise la recherche]")
    return _borner("\n".join(lignes) or "(aucun dépôt)")


def branches(profil, filtre: str = "", depot: str = "", projet: str = "") -> str:
    """Branches dont le nom contient filtre (ex. clé de ticket), dans un dépôt, tout un projet ou, par défaut,
    les dépôts du profil. Les plus récemment modifiées d'abord."""
    if depot:
        p, d, _ = resoudre(profil, depot)
        cibles = [(p, d, f"{p}/{d}")]
    elif projet:
        projet = projet.strip().upper()
        autorises = projets_autorises()
        if autorises and projet not in autorises:
            raise BitbucketErreur(f"Projet Bitbucket {projet} non autorisé.")
        cibles = [(projet, v["slug"], f"{projet}/{v['slug']}")
                  for v in _pages(profil, f"{API}/projects/{quote(projet)}/repos", maximum=DEPOTS_MAX)]
    else:
        cibles = _depots_du_profil(profil)
        if not cibles:
            raise BitbucketErreur("Aucun dépôt Bitbucket dans ce profil : précise « depot » ou « projet ».")
    blocs, erreurs = [], []
    for p, d, libelle in cibles:
        try:
            valeurs = _pages(profil, _chemin_depot(p, d) + "/branches",
                             {"filterText": filtre, "orderBy": "MODIFICATION", "details": "true"}, maximum=50)
        except BitbucketErreur as e:
            erreurs.append(f"{libelle} : {e}")
            continue
        if valeurs:
            lignes = []
            for v in valeurs:
                commit = (v.get("metadata") or {}).get(
                    "com.atlassian.bitbucket.server.bitbucket-branch:latest-commit-metadata") or {}
                quand = commit.get("authorTimestamp")
                date = f", modifiée le {_date(quand)}" if quand else ""
                defaut = " (branche par défaut)" if v.get("isDefault") else ""
                lignes.append(f"  - {v['displayId']}{defaut}{date}")
            blocs.append(f"{libelle} :\n" + "\n".join(lignes))
    texte = "\n".join(blocs) or f"(aucune branche contenant « {filtre} »)"
    if erreurs:
        texte += "\n\nNon consultés : " + "; ".join(erreurs[:5])
    return _borner(texte)


def _date(millisecondes) -> str:
    return datetime.fromtimestamp(int(millisecondes) / 1000).strftime("%d/%m/%Y %H:%M")


def fichiers(profil, depot: str, branche: str = "", dossier: str = "", motif: str = "") -> str:
    """Chemins des fichiers d'un dépôt (récursif depuis dossier), filtrés par un motif (*.java, *Vague*…)."""
    p, d, branche_profil = resoudre(profil, depot)
    branche = _branche_ou_defaut(profil, p, d, branche, branche_profil)
    chemin = _chemin_depot(p, d) + "/files" + (("/" + quote(dossier.strip("/"))) if dossier.strip("/") else "")
    valeurs = _pages(profil, chemin, {"at": _ref(branche)} if branche else {}, maximum=5000, taille=1000)
    if motif:
        m = motif.lower()
        valeurs = [v for v in valeurs if fnmatch(v.lower(), m) or fnmatch(v.lower().rsplit("/", 1)[-1], m)
                   or ("*" not in m and "?" not in m and m in v.lower())]
    total = len(valeurs)
    prefixe = (dossier.strip("/") + "/") if dossier.strip("/") else ""
    lignes = [prefixe + v for v in valeurs[:FICHIERS_MAX]]
    if total > FICHIERS_MAX:
        lignes.append(f"[… {total - FICHIERS_MAX} autres fichiers : précise le dossier ou le motif]")
    entete = f"{p}/{d}" + (f" @ {branche}" if branche else "") + f" — {total} fichier(s)"
    return _borner(entete + "\n" + ("\n".join(lignes) or "(aucun fichier)"))


def lire(profil, depot: str, chemin: str, branche: str = "", debut: int = 1, nombre: int = LIGNES_MAX) -> str:
    """Contenu d'un fichier (lignes numérotées, à partir de debut) ou d'un dossier, sur une branche."""
    p, d, branche_profil = resoudre(profil, depot)
    branche = _branche_ou_defaut(profil, p, d, branche, branche_profil)
    chemin = chemin.strip().strip("/")
    if chemin and _interdit(chemin, profil):
        raise BitbucketErreur("Fichier de secrets (mots de passe, clés) : lecture refusée. Ne cherche pas à le lire.")
    debut = max(1, int(debut or 1))
    nombre = max(1, min(int(nombre or LIGNES_MAX), LIGNES_MAX))
    params = {"start": debut - 1, "limit": nombre}
    if branche:
        params["at"] = _ref(branche)
    donnees = _get(profil, _chemin_depot(p, d) + "/browse" + ("/" + quote(chemin) if chemin else ""), params)
    entete = f"{p}/{d} @ {branche or '(défaut)'} : {chemin or '/'}"

    if "children" in donnees:  # dossier
        lignes = []
        for v in donnees["children"].get("values", []):
            nom = v.get("path", {}).get("toString", "")
            lignes.append(f"- {nom}/" if v.get("type") == "DIRECTORY" else f"- {nom}")
        return _borner(entete + "\n" + ("\n".join(lignes) or "(dossier vide)"))

    if donnees.get("binary"):
        return f"{entete}\n(fichier binaire, non affiché)"
    lignes = donnees.get("lines", [])
    texte = "\n".join(f"{debut + i:>6}\t{l.get('text', '')}" for i, l in enumerate(lignes))
    suite = "" if donnees.get("isLastPage", True) else \
        f"\n[… suite : relis avec debut={debut + len(lignes)}]"
    return _borner(f"{entete} (lignes {debut} à {debut + len(lignes) - 1})\n{texte}{suite}")


def comparer(profil, depot: str, branche: str, vers: str = "", fichier: str = "") -> str:
    """Ce que branche apporte par rapport à vers (par défaut la cible de sa pull request, sinon la branche du
    profil, sinon la branche par défaut) : commits et fichiers modifiés, ou le diff d'un fichier."""
    p, d, branche_profil = resoudre(profil, depot)
    base = _chemin_depot(p, d)
    branche = branche.strip()
    if not branche:
        raise BitbucketErreur("Indique la branche à comparer (bitbucket_branches aide à la trouver).")

    pr_info = ""
    if not vers.strip():
        prs = _pages(profil, base + "/pull-requests",
                     {"at": _ref(branche), "direction": "OUTGOING", "state": "ALL", "order": "NEWEST"}, maximum=5)
        if prs:
            pr = prs[0]
            vers = pr.get("toRef", {}).get("displayId", "")
            pr_info = f"Pull request n° {pr.get('id')} « {pr.get('title', '')} » ({pr.get('state', '')}) vers {vers}\n"
            if pr.get("description"):
                pr_info += "Description : " + " ".join(pr["description"].split())[:800] + "\n"
    vers = _branche_ou_defaut(profil, p, d, vers, branche_profil)
    if not vers:
        raise BitbucketErreur("Branche cible inconnue : précise « vers ».")
    params = {"from": _ref(branche), "to": _ref(vers)}

    if fichier.strip():
        fichier = fichier.strip().strip("/")
        if _interdit(fichier, profil):
            raise BitbucketErreur("Fichier de secrets : diff refusé.")
        donnees = _get(profil, base + "/compare/diff/" + quote(fichier), {**params, "contextLines": 5})
        return _borner(f"{p}/{d} : {fichier}, {branche} par rapport à {vers}\n" + _formater_diff(donnees))

    commits = _pages(profil, base + "/compare/commits", params, maximum=50)
    changements = _pages(profil, base + "/compare/changes", params, maximum=FICHIERS_MAX)
    lignes = [f"{p}/{d} : {branche} par rapport à {vers}", pr_info.rstrip()] if pr_info else \
        [f"{p}/{d} : {branche} par rapport à {vers}"]
    lignes.append(f"\n{len(commits)} commit(s)" + (" (50 premiers)" if len(commits) >= 50 else "") + " :")
    for c in commits:
        message = (c.get("message") or "").splitlines()[0][:160] if c.get("message") else ""
        lignes.append(f"- {c.get('displayId', '')} {_date(c['authorTimestamp']) if c.get('authorTimestamp') else ''}"
                      f" {message}")
    lignes.append(f"\n{len(changements)} fichier(s) modifié(s) :")
    for c in changements:
        chemin = c.get("path", {}).get("toString", "")
        ancien = c.get("srcPath", {}).get("toString", "")
        renomme = f" (ancien : {ancien})" if ancien and ancien != chemin else ""
        lignes.append(f"- [{c.get('type', '?')}] {chemin}{renomme}")
    lignes.append("\nPour le détail d'un fichier : même outil avec « fichier ».")
    return _borner("\n".join(lignes))


def _formater_diff(donnees: dict) -> str:
    sortie = []
    for diff in donnees.get("diffs", []):
        if diff.get("binary"):
            sortie.append("(fichier binaire)")
            continue
        for bloc in diff.get("hunks", []):
            sortie.append(f"@@ -{bloc.get('sourceLine')},{bloc.get('sourceSpan')} "
                          f"+{bloc.get('destinationLine')},{bloc.get('destinationSpan')} @@")
            for segment in bloc.get("segments", []):
                signe = {"ADDED": "+", "REMOVED": "-"}.get(segment.get("type"), " ")
                for l in segment.get("lines", []):
                    numero = l.get("destination") if signe != "-" else l.get("source")
                    sortie.append(f"{signe}{numero:>6} {l.get('line', '')}")
                    if len(sortie) >= DIFF_LIGNES_MAX:
                        return "\n".join(sortie) + "\n[… diff tronqué]"
        if diff.get("truncated"):
            sortie.append("[… diff tronqué par Bitbucket]")
    return "\n".join(sortie) or "(aucune différence)"


def chercher(profil, texte: str, depot: str = "", projet: str = "") -> str:
    """Recherche de code de Bitbucket (index de la branche par défaut uniquement, si la recherche est activée
    sur le serveur)."""
    requete = texte.strip()
    if not requete:
        raise BitbucketErreur("Texte à chercher vide.")
    if depot:
        p, d, _ = resoudre(profil, depot)
        requete += f" repo:{p}/{d}"
    elif projet:
        projet = projet.strip().upper()
        if projets_autorises() and projet not in projets_autorises():
            raise BitbucketErreur(f"Projet Bitbucket {projet} non autorisé.")
        requete += f" project:{projet}"
    racine, jeton = adresse(profil), config.secret("BITBUCKET_HTTP_TOKEN")
    if not racine or not jeton:
        raise BitbucketErreur("Bitbucket n'est pas configuré (onglet Configuration, Accès).")
    register_secrets([jeton])
    try:
        reponse = requests.post(racine + "/rest/search/latest/search", timeout=DELAI,
                                headers={"Authorization": f"Bearer {jeton}", "Accept": "application/json"},
                                json={"query": requete, "entities": {"code": {}},
                                      "limits": {"primary": 25, "secondary": 10}})
    except requests.RequestException as e:
        raise BitbucketErreur(f"Bitbucket injoignable ({type(e).__name__}).")
    if reponse.status_code in (404, 405):
        raise BitbucketErreur("La recherche de code n'est pas disponible sur ce serveur Bitbucket : utilise "
                              "bitbucket_fichiers puis bitbucket_lire.")
    if reponse.status_code >= 400:
        raise BitbucketErreur(f"Recherche refusée (HTTP {reponse.status_code}) : {_message(reponse)}")
    resultats = (reponse.json().get("code") or {})
    autorises = projets_autorises()
    lignes = []
    for v in resultats.get("values", []):
        repo = v.get("repository", {})
        cle = repo.get("project", {}).get("key", "")
        if autorises and cle.upper() not in autorises:
            continue
        chemin = v.get("file", "")
        if _interdit(chemin, profil):
            continue
        lignes.append(f"{cle}/{repo.get('slug', '')} : {chemin}")
        for groupe in (v.get("hitContexts") or [])[:3]:
            for l in groupe:
                extrait = re.sub(r"</?em>", "", l.get("text", "")).replace("&lt;", "<").replace("&gt;", ">") \
                    .replace("&quot;", '"').replace("&amp;", "&")
                lignes.append(f"  {l.get('line', ''):>6}  {extrait[:200]}")
    total = resultats.get("count", len(lignes))
    entete = f"{total} fichier(s) trouvé(s) (branche par défaut de chaque dépôt)"
    return _borner(entete + "\n" + ("\n".join(lignes) or "(aucun résultat)"))


def tester(profil: config.Profil | None = None) -> tuple:
    """(ok, message) : adresse et jeton valides ?"""
    if not configure(profil):
        return False, ("Jeton et adresse du serveur nécessaires (l'adresse se déduit des dépôts d'un projet, "
                       "ou se renseigne ici).")
    try:
        version = ""
        try:
            version = _get(profil, f"{API}/application-properties").get("version", "")
        except BitbucketErreur:
            pass
        page = _get(profil, f"{API}/repos", {"limit": 100})
    except BitbucketErreur as e:
        return False, str(e)
    total = f"{page.get('size', 0)}" + ("" if page.get("isLastPage", True) else " ou plus")
    return True, (f"Connecté à {adresse(profil)}" + (f" (Bitbucket {version})" if version else "")
                  + f" : {total} dépôt(s) lisible(s) avec ce jeton")
