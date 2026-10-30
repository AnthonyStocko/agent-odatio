"""
IHM de l'agent fonctionnel Odatio (Streamlit).

  venv\\Scripts\\python.exe -m streamlit run app.py      (ou double-clic sur lancer.bat)

Chaque question ouvre une session de l'agent qui reprend la conversation précédente (resume) : rien ne reste
en mémoire entre deux questions, ce qui convient au fonctionnement de Streamlit (le script est rejoué à chaque
interaction).
"""
import asyncio
import getpass
from datetime import datetime

import streamlit as st

from odatio_agent import config, outils, sources
from odatio_agent.etapes import resumer
from odatio_agent.session import Session

st.set_page_config(page_title="Agent Odatio", page_icon="🔎", layout="centered")


@st.cache_resource
def _initialiser() -> bool:
    config.charger_secrets()
    return True


_initialiser()
etat = st.session_state
etat.setdefault("messages", [])        # [{"role", "texte", "etapes", "bilan"}]
etat.setdefault("reprise", "")         # identifiant de la conversation en cours côté agent
etat.setdefault("profil_actif", "")
etat.setdefault("preparation", {})     # profil -> (sources prêtes, dossiers de logs)


def nouvelle_conversation() -> None:
    etat.messages, etat.reprise = [], ""


def preparer(profil: config.Profil, mettre_a_jour: bool = False):
    if mettre_a_jour or profil.nom not in etat.preparation:
        with st.spinner("Préparation des sources (le premier clonage peut prendre quelques minutes)…"):
            etat.preparation[profil.nom] = (sources.preparer(profil, mettre_a_jour=True, journal=lambda _: None),
                                            sources.dossiers_logs(profil))
    return etat.preparation[profil.nom]


def en_markdown(profil: str) -> str:
    lignes = [f"# Agent Odatio — {profil}", f"_{datetime.now():%d/%m/%Y %H:%M}_", ""]
    for m in etat.messages:
        lignes += [f"## {m['texte']}" if m["role"] == "user" else m["texte"], ""]
    return "\n".join(lignes)


async def repondre(profil, prets, logs, question, sur_texte, sur_outil):
    session = Session(profil, prets, logs, config.reglages(), getpass.getuser(), reprise=etat.reprise)
    await session.ouvrir()
    try:
        return await session.demander(question, sur_texte, sur_outil)
    finally:
        await session.fermer()


# ---------------------------------------------------------------------------
# Barre latérale : application, conversation, configuration
# ---------------------------------------------------------------------------
with st.sidebar:
    try:
        noms = config.noms_profils()
    except config.ConfigError as e:
        st.error(str(e))
        st.stop()
    if not noms:
        st.error("Aucun profil dans profils.toml.")
        st.stop()

    nom = st.selectbox("Application", noms, index=noms.index(etat.profil_actif) if etat.profil_actif in noms else 0)
    if nom != etat.profil_actif:
        etat.profil_actif = nom
        nouvelle_conversation()
    try:
        profil = config.charger_profil(nom)
    except config.ConfigError as e:
        st.error(str(e))
        st.stop()
    if profil.description:
        st.caption(profil.description)

    st.button("Nouvelle conversation", on_click=nouvelle_conversation, use_container_width=True,
              disabled=not etat.messages)
    if etat.messages:
        st.download_button("Télécharger la conversation", en_markdown(nom), use_container_width=True,
                           file_name=f"odatio-{nom}-{datetime.now():%Y%m%d-%H%M}.md", mime="text/markdown")

    with st.expander("Configuration"):
        for source in profil.sources:
            st.markdown(f"**Source {source.nom}**  \n`{source.chemin or source.depot}`")
        for dossier in profil.dossiers_logs:
            st.markdown(f"**Logs**  \n`{dossier}`")
        if profil.groundcover_filtre:
            st.markdown(f"**Logs groundcover**  \n`{profil.groundcover_filtre}`")
        if profil.base:
            st.markdown(f"**Base**  \n{profil.base.type}, base {profil.base.nom}, schéma {profil.base.schema or '(défaut)'}")
        if st.button("Tester la connexion", use_container_width=True):
            with st.spinner("Test en cours…"):
                for element, ok, message in outils.tester(profil):
                    (st.success if ok else st.error)(f"{element} : {message}")
        if any(s.depot for s in profil.sources) and st.button("Mettre à jour les sources", use_container_width=True):
            try:
                preparer(profil, mettre_a_jour=True)
                st.success("Sources à jour.")
            except sources.SourceError as e:
                st.error(str(e))

# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------
st.title("Agent Odatio")
st.caption(f"Questions fonctionnelles sur « {nom} ». L'agent lit les sources, la base et les logs, sans rien modifier.")

if not etat.messages:
    st.markdown("Pose une question sur un écran, un statut, un traitement ou une donnée précise. Cite le libellé "
                "exact, un message d'erreur ou une référence : l'agent trouvera plus vite.")
    st.markdown("Par exemple : *Dans quels cas une commande passe-t-elle au statut Bloquée ?* ou "
                "*Que se passe-t-il quand on clique sur Libérer dans l'écran des vagues ?*")

for m in etat.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["texte"])
        if m.get("etapes"):
            with st.expander(f"{len(m['etapes'])} étape(s) consultée(s)"):
                st.markdown("\n".join(f"- {e}" for e in m["etapes"]))
        if m.get("bilan"):
            st.caption(m["bilan"])

question = st.chat_input("Ta question sur l'application…")
if question:
    try:
        prets, logs = preparer(profil)
    except sources.SourceError as e:
        st.error(str(e))
        st.stop()
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
            bilan = asyncio.run(repondre(profil, prets, logs, question, sur_texte, sur_outil))
        except Exception as e:  # Claude Code introuvable, authentification, conversation à reprendre perdue…
            statut.update(label="Échec", state="error")
            st.error(f"L'agent n'a pas pu répondre : {e}\n\nSi le problème persiste, démarre une nouvelle "
                     f"conversation.")
            etat.messages.pop()
            st.stop()

        reponse = bilan.reponse or "\n\n".join(textes) or "_Pas de réponse._"
        zone.markdown(reponse)
        statut.update(label=f"{len(etapes)} étape(s) consultée(s)", state="error" if bilan.erreur else "complete")
        cout = f"{bilan.cout_usd:.3f} $" if bilan.cout_usd is not None else "coût inconnu"
        resume_bilan = (f"Interrompu : {bilan.message}, " if bilan.erreur else "") + cout
        st.caption(resume_bilan)

    etat.reprise = bilan.session_id or etat.reprise
    etat.messages.append({"role": "assistant", "texte": reponse, "etapes": etapes, "bilan": resume_bilan})
