"""
Page « Assistant » de l'IHM : choix du projet, tâche Jira à tester (facultative), conversation avec l'agent,
fichiers d'interface générés (téléchargement et chemin, pour les intégrer depuis l'écran de l'application).

Chaque question ouvre une session de l'agent qui reprend la conversation précédente (resume) : rien ne reste
en mémoire entre deux questions, ce qui convient au fonctionnement de Streamlit (le script est rejoué à chaque
interaction).
"""
import asyncio
import getpass
from datetime import datetime
from pathlib import Path

import streamlit as st

from . import config, documentation, outils, sources, video
from .etapes import resumer
from .session import Session

QUESTION_TACHE = ("Explique-moi ce que change cette tâche dans l'application, puis comment la tester : écrans et "
                  "traitements concernés, paramétrage et données à préparer, cas à jouer (nominaux et limites) et ce "
                  "qu'il faut vérifier pour conclure.")
TYPES_MIME = {".xml": "application/xml", ".csv": "text/csv", ".json": "application/json"}


def _etat():
    etat = st.session_state
    etat.setdefault("messages", [])       # [{"role", "texte", "etapes", "bilan", "fichiers"}]
    etat.setdefault("reprise", "")        # identifiant de la conversation en cours côté agent
    etat.setdefault("profil_actif", "")
    etat.setdefault("preparation", {})    # profil -> (sources prêtes, dossiers de logs)
    etat.setdefault("ticket", None)       # {"cle", "titre", "texte"} de la tâche à tester
    etat.setdefault("documentation", None)  # {"dossier", "sujet", "fichiers"} de la dernière documentation produite
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
        lignes += [f"- Fichier généré : `{f['chemin']}` ({_taille(f['taille'])})" for f in m.get("fichiers") or []]
    return "\n".join(lignes)


def _taille(octets: int) -> str:
    return f"{octets} octets" if octets < 1024 else f"{octets / 1024:.1f} ko"


def _afficher_fichiers(fichiers: list, cle: str) -> None:
    """Fichiers générés pendant une question : nom, taille, téléchargement et chemin complet."""
    for i, f in enumerate(fichiers):
        chemin = Path(f["chemin"])
        with st.container(border=True):
            c1, c2 = st.columns([3, 1])
            c1.markdown(f":material/description: **{f['nom']}** — {_taille(f['taille'])}, {f['encodage']}, "
                        f"{f['fins_ligne'].upper()}")
            if chemin.is_file():
                c2.download_button("Télécharger", chemin.read_bytes(), file_name=f["nom"], key=f"{cle}-{i}",
                                   mime=TYPES_MIME.get(chemin.suffix.lower(), "text/plain"), width="stretch")
            else:
                c2.caption("Fichier supprimé")
            st.code(str(chemin), language=None)


async def _repondre(profil, prets, logs, question, sur_texte, sur_outil):
    etat = st.session_state
    session = Session(profil, prets, logs, config.reglages(), getpass.getuser(), reprise=etat.reprise,
                      ticket=etat.ticket)
    await session.ouvrir()
    try:
        return await session.demander(question, sur_texte, sur_outil)
    finally:
        await session.fermer()


# ---------------------------------------------------------------------------
# Documentation pour le consultant fonctionnel
# ---------------------------------------------------------------------------

async def _documenter(profil, prets, logs, dossier, question, sur_texte, sur_outil, reprise):
    session = Session(profil, prets, logs, config.reglages(), getpass.getuser(), reprise=reprise,
                      ticket=st.session_state.ticket, dossier_documentation=dossier)
    await session.ouvrir()
    try:
        return await session.demander(question, sur_texte, sur_outil)
    finally:
        await session.fermer()


def _generer_documentation(profil, sujet: str, captures, avec_video: bool) -> None:
    etat = st.session_state
    try:
        prets, logs = _preparer(profil)
    except sources.SourceError as e:
        st.error(str(e))
        return
    racines = [chemin for _, chemin in prets] + logs
    titre = f"{etat.ticket['cle']} — {etat.ticket['titre']}" if etat.ticket else sujet
    dossier = documentation.nouveau_dossier(etat.ticket["cle"] if etat.ticket else sujet)
    (dossier / "sujet.txt").write_text(titre, encoding="utf-8")
    try:
        for fichier in captures or []:
            documentation.ajouter_capture(dossier, fichier.name, fichier.getvalue())
    except documentation.DocumentationErreur as e:
        st.error(str(e))
        return

    statut = st.status("L'agent enquête et rédige la documentation…", expanded=True)
    etapes = []

    def sur_outil(nom_outil, entree):
        etapes.append(resumer(nom_outil, entree, racines))
        statut.write(etapes[-1])

    question = documentation.question(sujet, etat.ticket)
    try:  # reprend la conversation en cours si elle existe : l'enquête déjà faite sert de point de départ
        bilan = asyncio.run(_documenter(profil, prets, logs, dossier, question, lambda _: None, sur_outil,
                                                  etat.reprise))
    except Exception as e:
        statut.update(label="Échec", state="error")
        st.error(f"L'agent n'a pas pu rédiger la documentation : {e}")
        return
    if not bilan.documentation:
        statut.update(label="Aucun document enregistré", state="error")
        st.error("L'agent n'a pas enregistré de document" + (f" ({bilan.message})" if bilan.message else "") +
                 ". Augmente les étapes ou le budget par question dans la configuration, ou précise le sujet.")
        return

    cout = f"{bilan.cout_usd:.2f} $" if bilan.cout_usd is not None else "coût inconnu"
    if _mettre_en_forme(dossier, titre, avec_video, statut):
        statut.update(label=f"Documentation prête ({len(etapes)} étape(s), {cout})", state="complete", expanded=False)


def _mettre_en_forme(dossier, titre: str, avec_video: bool, statut) -> bool:
    """Monte le document et la vidéo d'un dossier où l'agent a enregistré son document ; résultat dans
    st.session_state.documentation."""
    statut.update(label="Mise en forme du document et de la vidéo…", state="running")
    try:
        fichiers = documentation.produire(Path(dossier), titre, video_aussi=avec_video, journal=statut.write)
    except (documentation.DocumentationErreur, video.VideoErreur) as e:
        statut.update(label="Échec de la mise en forme", state="error")
        st.error(str(e))
        return False
    st.session_state.documentation = {"dossier": str(dossier), "sujet": titre,
                                      "fichiers": {k: str(v) for k, v in fichiers.items() if v and k != "voix"},
                                      "voix": fichiers.get("voix", "")}
    return True


def _afficher_documentation() -> None:
    etat = st.session_state
    doc = etat.documentation
    fichiers = {k: Path(v) for k, v in doc["fichiers"].items() if Path(v).exists()}
    st.markdown(f"**{doc['sujet']}**")
    if fichiers.get("video"):
        st.video(str(fichiers["video"]), subtitles={"Français": str(fichiers["sous_titres"])}
                 if fichiers.get("sous_titres") else None)
        if not doc.get("voix"):
            st.caption("Aucune voix de la langue du document sur ce poste : la narration est en sous-titres.")
    with st.expander("Document écrit"):
        st.markdown(fichiers["markdown"].read_text(encoding="utf-8"))
    st.caption("À relire avant envoi. Pour corriger : modifie document.json dans "
               f"{doc['dossier']}, puis « Refaire à partir de document.json ».")

    colonnes = st.columns(4)
    for colonne, (cle, libelle, mime) in zip(colonnes, (
            ("video", "Vidéo", "video/mp4"),
            ("word", "Word", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            ("markdown", "Markdown", "text/markdown"))):
        if fichiers.get(cle):
            colonne.download_button(libelle, fichiers[cle].read_bytes(), file_name=fichiers[cle].name, mime=mime,
                                    width="stretch", key=f"telecharger-{cle}")
    if colonnes[3].button("Refaire", width="stretch", help="Refait document et vidéo à partir de document.json"):
        with st.spinner("Mise en forme…"):
            try:
                rendu = documentation.produire(Path(doc["dossier"]), doc["sujet"], journal=lambda _: None)
                doc["fichiers"] = {k: str(v) for k, v in rendu.items() if v and k != "voix"}
                st.rerun()
            except (documentation.DocumentationErreur, video.VideoErreur) as e:
                st.error(str(e))

    if etat.ticket and config.jira_configure():
        a_joindre = [fichiers[k] for k in ("video", "word") if fichiers.get(k)]
        if st.button(f"Joindre au ticket {etat.ticket['cle']}", help="Ajoute la vidéo et le document Word en pièces "
                     "jointes du ticket Jira (seule action d'écriture, faite à ta demande)."):
            try:
                with st.spinner("Envoi vers Jira…"):
                    resultats = outils.jira_joindre(etat.ticket["cle"], a_joindre)
                for nom, ok, message in resultats:
                    (st.success if ok else st.error)(f"{nom} : {message}")
            except outils.OutilErreur as e:
                st.error(str(e))


def _bloc_documentation(profil) -> None:
    etat = st.session_state
    with st.expander("Documentation pour le consultant fonctionnel", icon=":material/movie:",
                     expanded=etat.documentation is not None):
        if etat.documentation:
            _afficher_documentation()
            st.divider()
        st.caption("L'agent enquête comme pour une question (en repartant de la conversation en cours), puis rédige "
                   "un document et une vidéo commentée : besoin, écrans, règles de gestion, paramétrage, cas de test. "
                   "Tu peux aussi le demander directement dans la conversation (« fais-moi une vidéo de cette "
                   "tâche »). Si l'application du projet est renseignée dans la configuration, l'agent y prend "
                   "lui-même les captures d'écran ; ce formulaire sert à joindre les tiennes.")
        with st.form("formulaire-documentation", border=False):
            sujet = st.text_input("Fonctionnalité à documenter", value="" if etat.ticket else "",
                                  placeholder=(f"La tâche {etat.ticket['cle']} (ou précise un angle)" if etat.ticket
                                               else "ex. La libération manuelle des vagues"))
            captures = st.file_uploader("Captures d'écran (facultatif)", type=["png", "jpg", "jpeg", "webp"],
                                        accept_multiple_files=True,
                                        help="Nomme-les d'après l'écran (suivi-vagues.png) : l'agent les place "
                                             "dans les bonnes scènes.")
            avec_video = st.checkbox("Produire la vidéo", value=True)
            lancer = st.form_submit_button("Générer la documentation", type="primary")
        if lancer:
            if not etat.ticket and not sujet.strip():
                st.error("Charge une tâche Jira ou indique la fonctionnalité à documenter.")
            else:
                _generer_documentation(profil, sujet.strip(), captures, avec_video)
                if etat.documentation:
                    st.rerun()


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
    st.caption(f"Projet « {profil.nom} »{contexte}. L'agent lit les sources, la base et les logs, sans rien modifier ; "
               f"il n'écrit que les fichiers d'interface que tu lui demandes, dans {config.dossier_fichiers(profil)}.")
    _bloc_documentation(profil)

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

    for n, m in enumerate(etat.messages):
        with st.chat_message(m["role"]):
            st.markdown(m["texte"])
            if m.get("fichiers"):
                _afficher_fichiers(m["fichiers"], f"fichier-{n}")
            if m.get("video") and Path(m["video"]).exists():
                st.video(m["video"])
            if m.get("documentation"):
                st.caption("Document Word, téléchargements, corrections et envoi au ticket : bloc « Documentation "
                           "pour le consultant fonctionnel », en haut de la page.")
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
        if bilan.fichiers:
            _afficher_fichiers(bilan.fichiers, f"fichier-{len(etat.messages)}")
        statut.update(label=f"{len(etapes)} étape(s) consultée(s)", state="error" if bilan.erreur else "complete")
        video_message = ""
        if bilan.documentation:  # documentation demandée dans la conversation : la vidéo est montée ici
            montage = st.status("Mise en forme du document et de la vidéo…", expanded=True)
            if _mettre_en_forme(bilan.documentation["dossier"], bilan.documentation["sujet"], True, montage):
                montage.update(label="Vidéo et document prêts", state="complete", expanded=False)
                video_message = etat.documentation["fichiers"].get("video", "")
        cout = f"{bilan.cout_usd:.3f} $" if bilan.cout_usd is not None else "coût inconnu"
        resume_bilan = (f"Interrompu : {bilan.message}, " if bilan.erreur else "") + cout
        st.caption(resume_bilan)

    etat.reprise = bilan.session_id or etat.reprise
    etat.messages.append({"role": "assistant", "texte": reponse, "etapes": etapes, "bilan": resume_bilan,
                          "fichiers": bilan.fichiers, "video": video_message,
                          "documentation": bool(bilan.documentation)})
    if bilan.documentation:
        st.rerun()  # réaffiche la page : vidéo sous la réponse, téléchargements dans le bloc de documentation
