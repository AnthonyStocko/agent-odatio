"""
Documentation d'une fonctionnalité pour le consultant fonctionnel : l'agent enquête comme d'habitude (ticket,
branche Bitbucket et diff, sources, paramétrage en base), puis enregistre un document structuré avec l'outil
documentation_enregistrer. Ce module valide ce document, contrôle qu'il ne contient ni secret ni donnée
nominative, puis le rend en Markdown et en Word ; la vidéo est produite par video.py.

Tout est écrit dans .cache/documentation/<sujet>-<date>/ :
  document.json     le document de l'agent (modifiable à la main, puis « python agent.py --rendre <dossier> »)
  captures/         captures d'écran fournies par l'utilisateur, citées par les scènes
  <sujet>.md / .docx / .mp4 / .srt

Les cas de test (écran, action, résultat attendu) sont structurés pour qu'une démonstration filmée dans
l'application puisse les rejouer plus tard.
"""
import json
import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path

from tools.secret_tools import mask_secrets, remaining_secrets

from . import config

SCENES_MIN, SCENES_MAX = 3, 12
NARRATION_MAX = 900          # caractères d'une narration (environ 40 secondes)
POINTS_MAX, POINT_MAX = 6, 220
EXTENSIONS_CAPTURES = {".png", ".jpg", ".jpeg", ".webp"}

_TEXTE = {"type": "string"}
_LISTE = {"type": "array", "items": {"type": "string"}}
SCHEMA = {
    "type": "object",
    "properties": {
        "titre": {"type": "string", "description": "titre de la fonctionnalité, en termes métier"},
        "resume": {"type": "string", "description": "2 ou 3 phrases : à quoi elle sert, pour qui"},
        "scenes": {
            "type": "array", "minItems": SCENES_MIN, "maxItems": SCENES_MAX,
            "description": "les parties de la vidéo et du document, dans l'ordre",
            "items": {"type": "object", "properties": {
                "titre": _TEXTE,
                "points": {**_LISTE, "maxItems": POINTS_MAX, "description": "points affichés, courts"},
                "narration": {"type": "string", "description": "texte lu à voix haute, 90 mots au plus"},
                "capture": {"type": "string", "description": "nom d'une capture fournie, si elle illustre la scène"},
            }, "required": ["titre", "points", "narration"]},
        },
        "parametrage": {**_LISTE, "description": "paramétrage et données à préparer, vérifiés en base si possible"},
        "cas_de_test": {
            "type": "array",
            "items": {"type": "object", "properties": {
                "titre": _TEXTE, "prerequis": _TEXTE,
                "etapes": {"type": "array", "items": {"type": "object", "properties": {
                    "ecran": _TEXTE, "action": _TEXTE, "attendu": _TEXTE}, "required": ["action", "attendu"]}},
            }, "required": ["titre", "etapes"]},
        },
        "points_attention": _LISTE,
        "preuves": {**_LISTE, "description": "sources de chaque affirmation (dépôt, branche, fichier, ligne ; table) "
                                             "et ce qui est déduit plutôt que vérifié ; non lu dans la vidéo"},
    },
    "required": ["titre", "resume", "scenes", "cas_de_test"],
}

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_SECRET_EN_CLAIR = re.compile(r"(?i)\b(?:pass(?:word|wd)?|mot de passe|pwd|token|jeton|secret|api[_-]?key)\s*[:=]\s*\S+")
_TELEPHONE = re.compile(r"(?<!\d)(?:\+33\s?|0)[1-9](?:[\s.-]?\d{2}){4}(?!\d)")


class DocumentationErreur(Exception):
    pass


def question(sujet: str, ticket: dict | None) -> str:
    """Demande adressée à l'agent par le bloc de l'IHM et par --documenter."""
    cible = f"la tâche {ticket['cle']} ({ticket['titre']})" if ticket else f"la fonctionnalité suivante : {sujet}"
    angle = f" Angle demandé : {sujet}." if ticket and sujet else ""
    return (f"Fais la documentation fonctionnelle complète (document et vidéo) de {cible}, destinée au consultant "
            f"fonctionnel : appelle documentation_demarrer, enquête, puis documentation_enregistrer.{angle}")


def sujet(dossier: Path) -> str:
    fichier = Path(dossier) / "sujet.txt"
    return fichier.read_text(encoding="utf-8").strip() if fichier.exists() else Path(dossier).name


# ---------------------------------------------------------------------------
# Dossier de travail
# ---------------------------------------------------------------------------

def _slug(texte: str) -> str:
    texte = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", texte).strip("-")[:50] or "documentation"


def nouveau_dossier(sujet: str) -> Path:
    dossier = config.CACHE / "documentation" / f"{_slug(sujet)}-{datetime.now():%Y%m%d-%H%M%S}"
    (dossier / "captures").mkdir(parents=True, exist_ok=True)
    return dossier


def ajouter_capture(dossier: Path, nom: str, contenu: bytes) -> str:
    """Enregistre une capture fournie par l'utilisateur ; renvoie le nom sous lequel l'agent peut la citer."""
    chemin = Path(nom)
    if chemin.suffix.lower() not in EXTENSIONS_CAPTURES:
        raise DocumentationErreur(f"Capture « {nom} » : formats acceptés {', '.join(sorted(EXTENSIONS_CAPTURES))}.")
    propre = _slug(chemin.stem).lower() + chemin.suffix.lower()
    (dossier / "captures" / propre).write_bytes(contenu)
    return propre


def copier_captures(dossier: Path, source: Path) -> list:
    noms = []
    for fichier in sorted(Path(source).iterdir()):
        if fichier.suffix.lower() in EXTENSIONS_CAPTURES:
            noms.append(ajouter_capture(dossier, fichier.name, fichier.read_bytes()))
    return noms


def captures(dossier: Path) -> list:
    rep = Path(dossier) / "captures"
    return sorted(f.name for f in rep.iterdir() if f.suffix.lower() in EXTENSIONS_CAPTURES) if rep.is_dir() else []


# ---------------------------------------------------------------------------
# Validation
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


def valider(document: dict, captures_disponibles: list) -> list:
    """Problèmes à corriger (liste vide = document accepté). Messages adressés à l'agent."""
    if not isinstance(document, dict):
        return ["Le document doit être un objet JSON."]
    erreurs = []
    for champ in ("titre", "resume"):
        if not str(document.get(champ, "")).strip():
            erreurs.append(f"« {champ} » est vide.")
    scenes = document.get("scenes") or []
    if not SCENES_MIN <= len(scenes) <= SCENES_MAX:
        erreurs.append(f"{len(scenes)} scène(s) : il en faut entre {SCENES_MIN} et {SCENES_MAX}.")
    for i, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict) or not str(scene.get("titre", "")).strip():
            erreurs.append(f"Scène {i} : titre manquant.")
            continue
        narration = str(scene.get("narration", "")).strip()
        if not narration:
            erreurs.append(f"Scène {i} « {scene['titre']} » : narration vide.")
        elif len(narration) > NARRATION_MAX:
            erreurs.append(f"Scène {i} « {scene['titre']} » : narration de {len(narration)} caractères, "
                           f"{NARRATION_MAX} au plus (découpe en deux scènes ou raccourcis).")
        points = scene.get("points") or []
        if len(points) > POINTS_MAX or any(len(str(p)) > POINT_MAX for p in points):
            erreurs.append(f"Scène {i} « {scene['titre']} » : {POINTS_MAX} points au plus, de {POINT_MAX} "
                           f"caractères au plus chacun.")
        capture = str(scene.get("capture") or "").strip()
        if capture and capture not in captures_disponibles:
            erreurs.append(f"Scène {i} : capture « {capture} » inconnue (disponibles : "
                           f"{', '.join(captures_disponibles) or 'aucune'}). N'en invente pas.")
    cas = document.get("cas_de_test") or []
    if not cas:
        erreurs.append("Aucun cas de test : le consultant doit savoir comment vérifier la fonctionnalité.")
    for i, c in enumerate(cas, start=1):
        if not (c.get("etapes") if isinstance(c, dict) else None):
            erreurs.append(f"Cas de test {i} : aucune étape.")

    tout = "\n".join(_textes(document))
    if remaining_secrets(tout) or _SECRET_EN_CLAIR.search(tout):
        erreurs.append("Le document contient ce qui ressemble à un secret (mot de passe, jeton, clé) : retire-le.")
    if _EMAIL.search(tout) or _TELEPHONE.search(tout):
        erreurs.append("Le document contient une adresse e-mail ou un numéro de téléphone : décris la personne ou "
                       "la donnée de façon anonyme.")
    return erreurs


def enregistrer(dossier: Path, document: dict) -> dict:
    """Valide puis écrit document.json ; lève DocumentationErreur avec la liste des corrections attendues."""
    erreurs = valider(document, captures(dossier))
    if erreurs:
        raise DocumentationErreur("Document refusé, corrige puis renvoie-le en entier :\n- " + "\n- ".join(erreurs))
    document = json.loads(mask_secrets(json.dumps(document, ensure_ascii=False))[0])
    document["_genere_le"] = datetime.now().isoformat(timespec="seconds")
    (Path(dossier) / "document.json").write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return document


def charger(dossier: Path) -> dict:
    fichier = Path(dossier) / "document.json"
    if not fichier.exists():
        raise DocumentationErreur(f"Aucun document.json dans {dossier}.")
    try:
        document = json.loads(fichier.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise DocumentationErreur(f"document.json illisible : {e}")
    erreurs = valider(document, captures(dossier))
    if erreurs:
        raise DocumentationErreur("document.json invalide :\n- " + "\n- ".join(erreurs))
    return document


def duree_estimee(document: dict) -> float:
    """Durée approximative de la vidéo, en minutes (150 mots lus par minute)."""
    from . import video
    mots = sum(len(s["narration"].split()) for s in video.sequences(document))
    return round(mots / 150, 1)


# ---------------------------------------------------------------------------
# Markdown et Word
# ---------------------------------------------------------------------------

def _pied(document: dict, sujet: str) -> str:
    date = document.get("_genere_le", "")[:10]
    date = f"le {date[8:10]}/{date[5:7]}/{date[:4]}" if date else ""
    return f"Documentation générée par l'agent Odatio {date} à partir de {sujet}, à relire avant diffusion.".replace("  ", " ")


def en_markdown(document: dict, sujet: str) -> str:
    lignes = [f"# {document['titre']}", "", document["resume"], ""]
    for scene in document["scenes"]:
        lignes += [f"## {scene['titre']}", ""]
        lignes += [f"- {p}" for p in scene.get("points") or []]
        lignes += ["", scene["narration"], ""]
        if scene.get("capture"):
            lignes += [f"![{scene['titre']}](captures/{scene['capture']})", ""]
    if document.get("parametrage"):
        lignes += ["## Paramétrage et données à préparer", ""] + [f"- {p}" for p in document["parametrage"]] + [""]
    lignes += ["## Cas de test", ""]
    for i, cas in enumerate(document["cas_de_test"], start=1):
        lignes += [f"### {i}. {cas['titre']}", ""]
        if cas.get("prerequis"):
            lignes += [f"Prérequis : {cas['prerequis']}", ""]
        lignes += ["| # | Écran | Action | Résultat attendu |", "|---|---|---|---|"]
        for n, e in enumerate(cas["etapes"], start=1):
            cellules = [str(e.get(k, "")).replace("|", "\\|").replace("\n", " ") for k in ("ecran", "action", "attendu")]
            lignes.append(f"| {n} | " + " | ".join(cellules) + " |")
        lignes.append("")
    if document.get("points_attention"):
        lignes += ["## Points d'attention", ""] + [f"- {p}" for p in document["points_attention"]] + [""]
    if document.get("preuves"):
        lignes += ["## Sources", ""] + [f"- {p}" for p in document["preuves"]] + [""]
    lignes += ["---", f"_{_pied(document, sujet)}_", ""]
    return "\n".join(lignes)


def en_word(document: dict, sujet: str, dossier: Path, chemin: Path) -> Path | None:
    """Document Word ; None si python-docx n'est pas installé."""
    try:
        from docx import Document
        from docx.shared import Cm, Pt
    except ImportError:
        return None
    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)
    doc.add_heading(document["titre"], level=0)
    doc.add_paragraph(document["resume"])
    for scene in document["scenes"]:
        doc.add_heading(scene["titre"], level=1)
        for p in scene.get("points") or []:
            doc.add_paragraph(p, style="List Bullet")
        doc.add_paragraph(scene["narration"])
        capture = Path(dossier) / "captures" / (scene.get("capture") or "")
        if scene.get("capture") and capture.is_file():
            try:
                doc.add_picture(str(capture), width=Cm(16))
            except Exception:  # format d'image non pris en charge par Word (webp)
                pass
    if document.get("parametrage"):
        doc.add_heading("Paramétrage et données à préparer", level=1)
        for p in document["parametrage"]:
            doc.add_paragraph(p, style="List Bullet")
    doc.add_heading("Cas de test", level=1)
    for i, cas in enumerate(document["cas_de_test"], start=1):
        doc.add_heading(f"{i}. {cas['titre']}", level=2)
        if cas.get("prerequis"):
            doc.add_paragraph(f"Prérequis : {cas['prerequis']}")
        table = doc.add_table(rows=1, cols=4)
        table.style = "Light Grid Accent 1"
        for cellule, texte in zip(table.rows[0].cells, ("#", "Écran", "Action", "Résultat attendu")):
            cellule.text = texte
        for n, e in enumerate(cas["etapes"], start=1):
            ligne = table.add_row().cells
            for cellule, texte in zip(ligne, (str(n), e.get("ecran", ""), e.get("action", ""), e.get("attendu", ""))):
                cellule.text = str(texte)
    if document.get("points_attention"):
        doc.add_heading("Points d'attention", level=1)
        for p in document["points_attention"]:
            doc.add_paragraph(p, style="List Bullet")
    if document.get("preuves"):
        doc.add_heading("Sources", level=1)
        for p in document["preuves"]:
            doc.add_paragraph(p, style="List Bullet")
    doc.add_paragraph().add_run(_pied(document, sujet)).italic = True
    doc.save(chemin)
    return chemin


# ---------------------------------------------------------------------------
# Production complète
# ---------------------------------------------------------------------------

def produire(dossier: Path, sujet: str, video_aussi: bool = True, journal=print) -> dict:
    """Rend le document du dossier : {"markdown", "word", "video", "sous_titres", "voix"} (chemins ou None)."""
    from . import video

    dossier = Path(dossier)
    document = charger(dossier)
    base = _slug(sujet)
    resultat = {"markdown": dossier / f"{base}.md", "word": None, "video": None, "sous_titres": None, "voix": ""}
    resultat["markdown"].write_text(en_markdown(document, sujet), encoding="utf-8")
    journal("Document Markdown écrit.")
    resultat["word"] = en_word(document, sujet, dossier, dossier / f"{base}.docx")
    if resultat["word"]:
        journal("Document Word écrit.")
    if video_aussi:
        rendu = video.produire(document, dossier, dossier / f"{base}.mp4", sujet=sujet, journal=journal)
        resultat.update(video=rendu["video"], sous_titres=rendu["sous_titres"], voix=rendu["voix"])
    return resultat


def archive(dossier: Path, sujet: str) -> Path:
    """Zip du dossier (sans document.json brut), pratique pour un envoi en une pièce."""
    dossier = Path(dossier)
    temporaire = dossier.parent / f"_{dossier.name}"
    if temporaire.exists():
        shutil.rmtree(temporaire)
    temporaire.mkdir()
    for f in dossier.iterdir():
        if f.is_file() and f.suffix in (".md", ".docx", ".mp4", ".srt"):
            shutil.copy2(f, temporaire / f.name)
    if any((dossier / "captures").iterdir()):
        shutil.copytree(dossier / "captures", temporaire / "captures")
    chemin = Path(shutil.make_archive(str(dossier / _slug(sujet)), "zip", temporaire))
    shutil.rmtree(temporaire)
    return chemin
