"""
Page « Assistant » de l'IHM : choix du projet, tâche Jira à tester (facultative), conversation avec l'agent.

Chaque question ouvre une session de l'agent qui reprend la conversation précédente (resume) : rien ne reste
en mémoire entre deux questions, ce qui convient au fonctionnement de Streamlit (le script est rejoué à chaque
interaction).
"""
import asyncio
import getpass
from datetime import datetime

import streamlit as st

from . import config, outils, sources
from .etapes import resumer
from .session import Session

QUESTION_TACHE = ("Explique-moi ce que change cette tâche dans l'application, puis comment la tester : écrans et "
                  "traitements concernés, paramétrage et données à préparer, cas à jouer (nominaux et limites) et ce "
                  "qu'il faut vérifier pour conclure.")


def _etat():
    etat = st.session_state
    etat.setdefault("messages", [])       # [{"role", "texte", "etapes", "bilan"}]
    etat.setdefault("reprise", "")        # identifiant de la conversation en cours côté agent
    etat.setdefault("profil_actif", "")
    etat.setdefault("preparation", {})    # profil -> (sources prêtes, dossiers de logs)
    etat.setdefault("ticket", None)       # {"cle", "titre", "texte"} de la tâche à tester
    return etat


def nouvelle_conversation() -> None:
    st.session_state.messages, st.session_state.reprise = [], ""


def _preparer(profil: config.Profil, mettre_a_jour: bool = False):
    etat = st.session_state
    if mettre_a_jour or profil.nom not in etat.preparation:
        with st.spinner("Préparation des sources (le premier clonage peut prendre quelques minutes)…"):
            etat.preparation[profil.nom] = (sources.preparer(profil, mettre_a_jour=True, journal=lambda _: None),
                                            sources.dossiers_logs(profil))
    return etat.preparation[profil.nom]


def _en_markdown(profil: str) -> str:
    etat = st.session_state
    lignes = [f"# Agent Odatio — {profil}", f"_{datetime.now():%d/%m/%Y %H:%M}_", ""]
    if etat.ticket:
        lignes += [f"Tâche : {etat.ticket['cle']} — {etat.ticket['titre']}", ""]
    for m in etat.messages:
        lignes += [f"## {m['texte']}" if m["role"] == "user" else m["texte"], ""]
    return "\n".join(lignes)


async def _repondre(profil, prets, logs, question, sur_texte, sur_outil):
    etat = st.session_state
    session = Session(profil, prets, logs, config.reglages(), getpass.getuser(), reprise=etat.reprise,
                      ticket=etat.ticket)
    await session.ouvrir()
    try:
        return await session.demander(question, sur_texte, sur_outil)
    finally:
        await session.fermer()


def _barre_laterale(page_configuration) -> config.Profil | None:
    etat = st.session_state
    with st.sidebar:
        noms = config.noms_profils()
        if not noms:
            st.info("Aucun projet configuré.")
            st.page_link(page_configuration, label="Définir un projet", icon=":material/settings:")
            return None

        nom = st.selectbox("Projet", noms, index=noms.index(etat.profil_actif) if etat.profil_actif in noms else 0)
        if nom != etat.profil_actif:
            etat.profil_actif = nom
            nouvelle_conversation()
        try:
            profil = config.charger_profil(nom)
        except config.ConfigError as e:
            st.error(str(e))
            return None
        if profil.description:
            st.caption(profil.description)

        st.divider()
        st.markdown("**Tâche à tester**")
        if not config.jira_configure():
            st.caption("Renseigne tes accès Jira pour donner une tâche à l'agent.")
            st.page_link(page_configuration, label="Configurer Jira", icon=":material/settings:")
        elif etat.ticket:
            st.markdown(f"[{etat.ticket['cle']}](https://{config.secret('JIRA_DOMAIN').removeprefix('https://')}"
                        f"/browse/{etat.ticket['cle']}) — {etat.ticket['titre']}")
            with st.expander("Contenu transmis à l'agent"):
                st.markdown(etat.ticket["texte"])
            if st.button("Retirer la tâche", width="stretch"):
                etat.ticket = None
                nouvelle_conversation()
                st.rerun()
        else:
            with st.form("formulaire-ticket", border=False):
                cle = st.text_input("Ticket Jira", placeholder="ODA-1234", label_visibility="collapsed")
                if st.form_submit_button("Charger la tâche", width="stretch") and cle:
                    try:
                        with st.spinner("Lecture du ticket…"):
                            etat.ticket = outils.jira_ticket(cle, profil)
                        nouvelle_conversation()
                        st.rerun()
                    except outils.OutilErreur as e:
                        st.error(str(e))

        st.divider()
        st.button("Nouvelle conversation", on_click=nouvelle_conversation, width="stretch",
                  disabled=not etat.messages)
        if etat.messages:
            st.download_button("Télécharger la conversation", _en_markdown(nom), width="stretch",
                               file_name=f"odatio-{nom}-{datetime.now():%Y%m%d-%H%M}.md", mime="text/markdown")
        if any(s.depot for s in profil.sources) and st.button("Mettre à jour les sources", width="stretch"):
            try:
                _preparer(profil, mettre_a_jour=True)
                st.success("Sources à jour.")
            except sources.SourceError as e:
                st.error(str(e))
    return profil


def page(page_configuration) -> None:
    etat = _etat()
    profil = _barre_laterale(page_configuration)

    st.title("Agent Odatio")
    if profil is None:
        st.markdown("Commence par décrire le projet sur lequel tu travailles : ses sources, ses logs et sa base.")
        st.page_link(page_configuration, label="Ouvrir la configuration", icon=":material/settings:")
        return

    contexte = f" sur la tâche {etat.ticket['cle']}" if etat.ticket else ""
    st.caption(f"Projet « {profil.nom} »{contexte}. L'agent lit les sources, la base et les logs, sans rien modifier.")

    question_auto = None
    if not etat.messages:
        if etat.ticket:
            st.markdown(f"La tâche **{etat.ticket['cle']}** est transmise à l'agent. Demande-lui de l'expliquer, ou "
                        f"pose directement ta question.")
            if st.button("Comment tester cette tâche ?", type="primary"):
                question_auto = QUESTION_TACHE
        else:
            st.markdown("Pose une question sur un écran, un statut, un traitement ou une donnée précise. Cite le "
                        "libellé exact, un message d'erreur ou une référence : l'agent trouvera plus vite. Pour "
                        "préparer un test, charge d'abord la tâche Jira dans la barre latérale.")

    for m in etat.messages:
        with st.chat_message(m["role"]):
            st.markdown(m["texte"])
            if m.get("etapes"):
                with st.expander(f"{len(m['etapes'])} étape(s) consultée(s)"):
                    st.markdown("\n".join(f"- {e}" for e in m["etapes"]))
            if m.get("bilan"):
                st.caption(m["bilan"])

    question = st.chat_input("Ta question sur l'application…") or question_auto
    if not question:
        return
    try:
        prets, logs = _preparer(profil)
    except sources.SourceError as e:
        st.error(str(e))
        return
    racines = [chemin for _, chemin in prets] + logs

    etat.messages.append({"role": "user", "texte": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        statut = st.status("L'agent cherche…", expanded=False)
        zone = st.empty()
        etapes, textes = [], []

        def sur_outil(nom_outil, entree):
            etapes.append(resumer(nom_outil, entree, racines))
            statut.write(etapes[-1])
            statut.update(label=f"L'agent cherche… ({len(etapes)} étape(s))")

        def sur_texte(texte):
            textes.append(texte)
            zone.markdown("\n\n".join(textes))

        try:
            bilan = asyncio.run(_repondre(profil, prets, logs, question, sur_texte, sur_outil))
        except Exception as e:  # Claude Code introuvable, authentification, conversation à reprendre perdue…
            statut.update(label="Échec", state="error")
            st.error(f"L'agent n'a pas pu répondre : {e}\n\nVérifie le compte Claude dans la configuration ; si le "
                     f"problème persiste, démarre une nouvelle conversation.")
            etat.messages.pop()
            return

        reponse = bilan.reponse or "\n\n".join(textes) or "_Pas de réponse._"
        zone.markdown(reponse)
        statut.update(label=f"{len(etapes)} étape(s) consultée(s)", state="error" if bilan.erreur else "complete")
        cout = f"{bilan.cout_usd:.3f} $" if bilan.cout_usd is not None else "coût inconnu"
        resume_bilan = (f"Interrompu : {bilan.message}, " if bilan.erreur else "") + cout
        st.caption(resume_bilan)

    etat.reprise = bilan.session_id or etat.reprise
    etat.messages.append({"role": "assistant", "texte": reponse, "etapes": etapes, "bilan": resume_bilan})
