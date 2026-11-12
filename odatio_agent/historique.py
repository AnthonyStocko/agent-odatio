"""
Historique des conversations, pour les reprendre plus tard (IHM et ligne de commande).

Chaque conversation est enregistrée après chaque réponse dans .cache/conversations/<profil>/<id>.json :
questions et réponses affichées, étapes, fichiers générés, tâche Jira, documentation produite, et l'identifiant
de reprise de l'agent (Bilan.session_id). Reprendre une conversation rouvre ce fil côté agent : il retrouve tout
son contexte (sources lues, requêtes, conclusions). Si ce fil n'est plus disponible (autre compte Claude, autre
poste, historique de Claude Code effacé), la conversation repart avec un récapitulatif des échanges.

Rien de secret n'y est écrit : les réponses de l'agent sont déjà masquées, et le dossier .cache n'est pas
versionné.
"""
import json
import re
from datetime import datetime
from pathlib import Path

from . import config

RECAPITULATIF_MAX = 15000    # caractères des échanges repris quand le fil de l'agent est perdu
TITRE_MAX = 90


def _dossier(profil: str) -> Path:
    return config.CACHE / "conversations" / re.sub(r"[^A-Za-z0-9_.-]+", "-", profil)


def _identifiant() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S-%f}"


def _valide(identifiant: str) -> bool:
    return bool(re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9]{6}", str(identifiant or "")))


def titre(messages: list, ticket: dict | None = None) -> str:
    """La première question, raccourcie ; préfixée de la tâche Jira s'il y en a une."""
    question = next((m["texte"] for m in messages if m.get("role") == "user"), "") or "Conversation"
    question = " ".join(question.split())
    if len(question) > TITRE_MAX:
        question = question[:TITRE_MAX - 1].rstrip() + "…"
    return f"{ticket['cle']} · {question}" if ticket else question


def enregistrer(profil: str, conversation: dict) -> str:
    """Écrit la conversation ({id?, reprise, ticket, messages, documentation}) ; renvoie son identifiant."""
    identifiant = conversation.get("id") if _valide(conversation.get("id")) else _identifiant()
    dossier = _dossier(profil)
    dossier.mkdir(parents=True, exist_ok=True)
    chemin = dossier / f"{identifiant}.json"
    cree_le = datetime.now().isoformat(timespec="seconds")
    if chemin.exists():
        try:
            cree_le = json.loads(chemin.read_text(encoding="utf-8")).get("cree_le", cree_le)
        except (OSError, ValueError):
            pass
    donnees = {
        "id": identifiant, "profil": profil, "titre": titre(conversation.get("messages") or [], conversation.get("ticket")),
        "cree_le": cree_le, "maj_le": datetime.now().isoformat(timespec="seconds"),
        "reprise": conversation.get("reprise") or "", "ticket": conversation.get("ticket"),
        "documentation": conversation.get("documentation"), "messages": conversation.get("messages") or [],
    }
    temporaire = chemin.with_suffix(".json.partiel")
    temporaire.write_text(json.dumps(donnees, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    temporaire.replace(chemin)
    return identifiant


def lister(profil: str, limite: int = 50) -> list:
    """[{id, titre, maj_le, questions, documentation}] du plus récent au plus ancien."""
    dossier = _dossier(profil)
    resultats = []
    for chemin in dossier.glob("*.json") if dossier.is_dir() else []:
        try:
            d = json.loads(chemin.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        resultats.append({"id": d.get("id", chemin.stem), "titre": d.get("titre", chemin.stem),
                          "maj_le": d.get("maj_le", ""),
                          "questions": sum(1 for m in d.get("messages") or [] if m.get("role") == "user"),
                          "documentation": (d.get("documentation") or {}).get("dossier", "")})
    return sorted(resultats, key=lambda r: r["maj_le"], reverse=True)[:limite]


def charger(profil: str, identifiant: str) -> dict:
    if not _valide(identifiant):
        raise config.ConfigError(f"Conversation « {identifiant} » inconnue.")
    chemin = _dossier(profil) / f"{identifiant}.json"
    try:
        return json.loads(chemin.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise config.ConfigError(f"Conversation « {identifiant} » introuvable pour le projet « {profil} ».")
    except ValueError as e:
        raise config.ConfigError(f"Conversation « {identifiant} » illisible : {e}")


def supprimer(profil: str, identifiant: str) -> None:
    if _valide(identifiant):
        (_dossier(profil) / f"{identifiant}.json").unlink(missing_ok=True)


def de_la_documentation(profil: str, dossier_documentation: str) -> dict | None:
    """La conversation la plus récente qui a produit cette documentation, s'il y en a une."""
    cible = str(Path(dossier_documentation).resolve())
    for r in lister(profil, limite=500):
        if r["documentation"] and str(Path(r["documentation"]).resolve()) == cible:
            return charger(profil, r["id"])
    return None


def libelle_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d/%m %H:%M")
    except ValueError:
        return iso


def recapitulatif(messages: list, question: str) -> str:
    """Question précédée des échanges précédents, quand le fil de l'agent ne peut pas être rouvert. Les échanges
    les plus récents sont gardés en priorité."""
    blocs = []
    for m in messages:
        qui = "Question" if m.get("role") == "user" else "Ta réponse"
        blocs.append(f"[{qui}]\n{str(m.get('texte', '')).strip()}")
    texte, total = [], 0
    for bloc in reversed(blocs):
        if total + len(bloc) > RECAPITULATIF_MAX:
            texte.append("[… échanges plus anciens omis …]")
            break
        texte.append(bloc)
        total += len(bloc)
    if not blocs:
        return question
    return ("Nous reprenons une conversation précédente dont ton historique détaillé n'est plus disponible. Voici "
            "les échanges, à prendre comme contexte (tes conclusions d'alors restent à revérifier si tu t'appuies "
            "dessus) :\n--- début des échanges ---\n" + "\n\n".join(reversed(texte))
            + "\n--- fin des échanges ---\n\nNouvelle question : " + question)
