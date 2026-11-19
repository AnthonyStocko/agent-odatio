"""
Questions transmises par agent-manager-savoye (``mcp_server.py --manager``), en LECTURE SEULE.

Le manager choisit le projet et pose une question ponctuelle ; l'agent y répond comme dans l'IHM (sources, base,
logs, Jira, Bitbucket), mais sans aucun outil qui écrit ou agit : ni fichier d'interface, ni documentation, ni
navigation dans l'application (``Session(lecture_seule=True)``). La conversation est enregistrée dans
l'historique du projet : l'utilisateur peut la reprendre dans l'IHM de l'agent (barre latérale).
"""
from __future__ import annotations

import contextlib
import getpass
import sys

from . import bitbucket, config, historique, outils, sources
from .session import Session

REPONSE_MAX = 20000   # caractères rendus au manager


def projets() -> str:
    """Les projets de l'agent, avec ce qu'il peut consulter pour chacun."""
    lignes = []
    for p in config.tous_les_profils():
        acces = ["sources"]
        if p.base:
            acces.append(f"base {p.base.type}")
        if p.dossiers_logs or p.groundcover_filtre or p.logs_ssh:
            acces.append("logs" + (" SSH" if p.logs_ssh else ""))
        if bitbucket.configure(p):
            acces.append("Bitbucket")
        origine = " (configuration commune du manager)" if p.gere else ""
        lignes.append(f"- {p.nom}{origine} : {p.description or 'sans description'} — accès : {', '.join(acces)}")
    return "Projets de l'agent Odatio :\n" + ("\n".join(lignes) if lignes else "(aucun)")


async def question(projet: str, texte: str, ticket: str = "") -> str:
    """Pose une question au projet, en lecture seule ; renvoie la réponse, son coût et la conversation.

    Tout affichage pendant la question part sur stderr : stdout est réservé au protocole MCP (le serveur stdio
    garde son propre flux, ouvert au démarrage)."""
    with contextlib.redirect_stdout(sys.stderr):
        return await _question(projet, texte, ticket)


async def _question(projet: str, texte: str, ticket: str = "") -> str:
    texte = (texte or "").strip()
    if not texte:
        raise outils.OutilErreur("Question vide.")
    try:
        profil = config.charger_profil(projet)
    except config.ConfigError as e:
        raise outils.OutilErreur(str(e))
    reglages = config.reglages()
    contexte_ticket = outils.jira_ticket(ticket.strip(), profil) if ticket.strip() else None
    prets = sources.preparer(profil, mettre_a_jour=False, journal=lambda m: print(m, file=sys.stderr))
    logs = sources.dossiers_logs(profil)
    session = Session(profil, prets, logs, reglages, getpass.getuser(), ticket=contexte_ticket, lecture_seule=True)
    await session.ouvrir()
    try:
        bilan = await session.demander(texte, lambda _: None, lambda *_: None)
    finally:
        await session.fermer()
    conversation = historique.enregistrer(profil.nom, {
        "reprise": bilan.session_id, "ticket": contexte_ticket,
        "messages": [{"role": "user", "texte": texte},
                     {"role": "assistant", "texte": bilan.reponse, "fichiers": [], "documentation": False}]})
    reponse = bilan.reponse or "(aucune réponse)"
    if len(reponse) > REPONSE_MAX:
        reponse = reponse[:REPONSE_MAX] + "\n… (réponse tronquée : la suite est dans l'agent Odatio)"
    cout = f"{bilan.cout_usd:.3f} $" if bilan.cout_usd is not None else "coût inconnu"
    etat = f" — interrompue : {bilan.message}" if bilan.erreur else ""
    return (f"{reponse}\n\n---\nAgent Odatio, projet « {profil.nom} » : {bilan.tours} étape(s), {cout}{etat}. "
            f"Conversation « {conversation} » enregistrée : elle se reprend dans l'agent Odatio "
            f"(http://localhost:8502, barre latérale).")
