"""
Liens préremplis d'agent-manager-savoye : le manager ouvre l'agent sur le bon projet, avec ce qu'il sait déjà.

Paramètres d'adresse (contrat commun aux agents, agent-manager-savoye/manager/liens.py) :
    ?source=manager&projet=<projet>&ticket=<CLE>&conversation=<identifiant>&question=<texte>&contexte=<résumé>

- projet : projet de l'agent (casse et séparateurs ignorés) ;
- conversation : conversation enregistrée par une question du manager (``manager.question``), reprise ;
- ticket : tâche Jira chargée (lecture seule, comme le bouton « Charger la tâche ») ;
- question : question PRÉREMPLIE, posée seulement quand l'utilisateur clique ;
- contexte : résumé du manager, affiché en tête de page.
Un lien n'est appliqué qu'une fois par session ; toute valeur invalide est ignorée. Aucune écriture, aucune
question posée à l'agent sans l'utilisateur.
"""
from __future__ import annotations

import re

TEXTE_MAX = 1500
CONTEXTE_MAX = 600
_TICKET = re.compile(r"[A-Z][A-Z0-9_]+-\d+")
_CONVERSATION = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9]{6}")


def _cle(nom: str) -> str:
    return re.sub(r"[\s_.-]+", "", (nom or "").lower())


def lire(params: dict) -> dict:
    """Paramètres d'un lien du manager, validés ; {} si l'adresse ne vient pas du manager."""
    params = {k: (v[0] if isinstance(v, list) and v else v) for k, v in (params or {}).items()}
    if params.get("source") != "manager":
        return {}
    ticket = str(params.get("ticket") or "").strip().upper()
    conversation = str(params.get("conversation") or "").strip()
    lien = {"projet": str(params.get("projet") or "").strip()[:80],
            "ticket": ticket if _TICKET.fullmatch(ticket) else "",
            "conversation": conversation if _CONVERSATION.fullmatch(conversation) else "",
            "question": str(params.get("question") or "").strip()[:TEXTE_MAX],
            "contexte": " ".join(str(params.get("contexte") or "").split())[:CONTEXTE_MAX]}
    return {k: v for k, v in lien.items() if v}


def signature(lien: dict) -> str:
    return "|".join(f"{k}={lien[k]}" for k in sorted(lien))


def projet(nom: str, noms: list) -> str:
    """Projet de l'agent qui correspond au projet du manager (casse et séparateurs ignorés) ; '' sinon."""
    cle = _cle(nom)
    return next((n for n in noms or [] if _cle(n) == cle), "") if cle else ""
