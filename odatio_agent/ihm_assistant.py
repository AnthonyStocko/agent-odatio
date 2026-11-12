"""
Page « Assistant » de l'IHM : choix du projet, tâche Jira à tester (facultative), conversation avec l'agent,
fichiers d'interface générés (téléchargement et chemin, pour les intégrer depuis l'écran de l'application).

Chaque question ouvre une session de l'agent qui reprend la conversation précédente (resume) : rien ne reste
en mémoire entre deux questions, ce qui convient au fonctionnement de Streamlit (le script est rejoué à chaque
interaction).

Chaque conversation est enregistrée après chaque réponse (historique.py) : la barre latérale permet d'en
reprendre une ancienne, et le bloc de documentation de rouvrir une documentation déjà produite pour la
poursuivre (ajouter un cas de test, des captures, corriger, traduire) avec la conversation qui l'a produite.
"""
import asyncio
import getpass
from datetime import datetime
from pathlib import Path

import streamlit as st

from . import config, documentation, historique, outils, sources, video
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
    etat.setdefault("conversation_id", "")  # identifiant dans l'historique (historique.py)
    etat.setdefault("profil_actif", "")
    etat.setdefault("preparation", {})    # profil -> (sources prêtes, dossiers de logs)
    etat.setdefault("ticket", None)       # {"cle", "titre", "texte"} de la tâche à tester
    etat.setdefault("documentation", None)  # {"dossier", "sujet", "fichiers"} de la dernière documentation produite
    return etat


def nouvelle_conversation() -> None:
    etat = st.session_state
    etat.messages, etat.reprise, etat.conversation_id, etat.documentation = [], "", "", None


def _sauvegarder() -> None:
    """Enregistre la conversation en cours dans l'historique du projet."""
    etat = st.session_state
    if not etat.messages or not etat.profil_actif:
        return
    doc = etat.documentation
    etat.conversation_id = historique.enregistrer(etat.profil_actif, {
        "id": etat.conversation_id, "reprise": etat.reprise, "ticket": etat.ticket, "messages": etat.messages,
        "documentation": {"dossier": doc["dossier"], "sujet": doc["sujet"]} if doc else None})


def _documentation_de(dossier: str) -> dict | None:
    """État d'affichage d'une documentation déjà produite (fichiers présents dans son dossier)."""
    if not (Path(dossier) / "document.json").is_file():
        return None
    return {"dossier": str(dossier), "sujet": documentation.sujet(Path(dossier)),
            "fichiers": {k: str(v) for k, v in documentation.fichiers(Path(dossier)).items()}, "voix": None}


def reprendre_conversation(identifiant: str) -> None:
    """Recharge une conversation de l'historique : échanges, fil de l'agent, tâche et documentation."""
    etat = st.session_state
    conversation = historique.charger(etat.profil_actif, identifiant)
    etat.messages = conversation.get("messages") or []
    etat.reprise = conversation.get("reprise") or ""
    etat.conversation_id = conversation["id"]
    etat.ticket = conversation.get("ticket")
    doc = conversation.get("documentation") or {}
    etat.documentation = _documentation_de(doc["dossier"]) if doc.get("dossier") else None


def ouvrir_documentation(dossier: str) -> None:
    """Rouvre une documentation pour la poursuivre, avec la conversation qui l'a produite si elle est connue."""
    etat = st.session_state
    conversation = historique.de_la_documentation(etat.profil_actif, dossier)
    if conversation:
        reprendre_conversation(conversation["id"])
    else:
        etat.messages, etat.reprise, etat.conversation_id = [], "", ""
    etat.documentation = _documentation_de(dossier)


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


async def _interroger(profil, prets, logs, question, sur_texte, sur_outil, dossier=None, precedents=None):
    """Pose la question en reprenant le fil de l'agent ; si ce fil n'est plus disponible (conversation reprise
    sur un autre compte ou un autre poste), repart avec le récapitulatif des échanges précédents."""
    etat = st.session_state
    precedents = etat.messages if precedents is None else precedents

    async def une_fois(reprise: str, texte: str):
        session = Session(profil, prets, logs, config.reglages(), getpass.getuser(), reprise=reprise,
                          ticket=etat.ticket, dossier_documentation=dossier)
        await session.ouvrir()
        try:
            return await session.demander(texte, sur_texte, sur_outil)
        finally:
            await session.fermer()

    if etat.reprise:
        try:
            bilan = await une_fois(etat.reprise, question)
            if not (bilan.erreur and bilan.tours == 0):
                return bilan
        except Exception:  # fil introuvable : repli sur le récapitulatif
            pass
    return await une_fois("", historique.recapitulatif(precedents, question) if precedents else question)


async def _repondre(profil, prets, logs, question, sur_texte, sur_outil):
    """Question de la conversation : une documentation ouverte (produite ou rechargée) peut être poursuivie."""
    doc = st.session_state.documentation
    return await _interroger(profil, prets, logs, question, sur_texte, sur_outil,
                             dossier=Path(doc["dossier"]) if doc else None,
                             precedents=st.session_state.messages[:-1])


# ---------------------------------------------------------------------------
# Documentation pour le consultant fonctionnel
# ---------------------------------------------------------------------------

def _generer_documentation(profil, sujet: str, captures, avec_video: bool, poursuivre: bool = False) -> None:
    etat = st.session_state
    try:
        prets, logs = _preparer(profil)
    except sources.SourceError as e:
        st.error(str(e))
        return
    racines = [chemin for _, chemin in prets] + logs
    if poursuivre:  # documentation ouverte : même dossier, même sujet, versions précédentes gardées
        dossier = Path(etat.documentation["dossier"])
        titre = documentation.sujet(dossier)
    else:
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

    question = documentation.question_suite(sujet) if poursuivre else documentation.question(sujet, etat.ticket)
    try:  # reprend la conversation en cours si elle existe : l'enquête déjà faite sert de point de départ
        bilan = asyncio.run(_interroger(profil, prets, logs, question, lambda _: None, sur_outil, dossier=dossier))
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
    # la demande et la réponse rejoignent la conversation, pour pouvoir la poursuivre ou la reprendre plus tard
    etat.reprise = bilan.session_id or etat.reprise
    etat.messages += [{"role": "user", "texte": (f"Poursuivre la documentation : {sujet}" if poursuivre else
                                                 f"Documentation : {titre}" + (f" ({sujet})" if sujet and etat.ticket
                                                                               else ""))},
                      {"role": "assistant", "texte": bilan.reponse or "_Documentation enregistrée._", "etapes": etapes,
                       "bilan": cout, "documentation": True}]
    if _mettre_en_forme(dossier, titre, avec_video, statut):
        statut.update(label=f"Documentation prête ({len(etapes)} étape(s), {cout})", state="complete", expanded=False)
    _sauvegarder()


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
                                      "fichiers": {k: str(v) for k, v in fichiers.items()
                                                   if v and k not in ("voix", "langue")},
                                      "voix": fichiers.get("voix", "")}
    return True


def _afficher_documentation() -> None:
    etat = st.session_state
    doc = etat.documentation
    fichiers = {k: Path(v) for k, v in doc["fichiers"].items() if Path(v).exists()}
    st.markdown(f"**{doc['sujet']}**")
    if fichiers.get("video"):
        st.video(str(fichiers["video"]), subtitles={"Sous-titres": str(fichiers["sous_titres"])}
                 if fichiers.get("sous_titres") else None)
        if doc.get("voix") == "":
            st.caption("Aucune voix de la langue du document sur ce poste : la narration est en sous-titres.")
    if fichiers.get("markdown"):
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
                doc["fichiers"] = {k: str(v) for k, v in rendu.items() if v and k not in ("voix", "langue")}
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


def _choisir_documentation() -> None:
    """Rouvre une documentation déjà produite (tous projets confondus), pour la relire ou la poursuivre."""
    etat = st.session_state
    precedentes = [d for d in documentation.lister()
                   if not etat.documentation or d["dossier"] != etat.documentation["dossier"]]
    if not precedentes:
        return
    c1, c2 = st.columns([4, 1], vertical_alignment="bottom")
    choix = c1.selectbox("Rouvrir une documentation", precedentes, index=None, placeholder="Documentations précédentes",
                         format_func=lambda d: f"{d['sujet']} — {historique.libelle_date(d['date'])}"
                                               + ("" if d["video"] else " (sans vidéo)"))
    if c2.button("Ouvrir", width="stretch", disabled=choix is None, key="ouvrir-documentation"):
        ouvrir_documentation(choix["dossier"])
        st.rerun()


def _bloc_documentation(profil) -> None:
    etat = st.session_state
    with st.expander("Documentation pour le consultant fonctionnel", icon=":material/movie:",
                     expanded=etat.documentation is not None):
        if etat.documentation:
            _afficher_documentation()
            st.divider()
        _choisir_documentation()
        st.caption("L'agent enquête comme pour une question (en repartant de la conversation en cours), puis rédige "
                   "un document et une vidéo commentée : besoin, écrans, règles de gestion, paramétrage, cas de test. "
                   "Tu peux aussi le demander directement dans la conversation (« fais-moi une vidéo de cette "
                   "tâche »). Si l'application du projet est renseignée dans la configuration, l'agent y prend "
                   "lui-même les captures d'écran ; ce formulaire sert à joindre les tiennes.")
        with st.form("formulaire-documentation", border=False):
            poursuivre = False
            if etat.documentation:
                choix_suite = f"Poursuivre « {etat.documentation['sujet']} »"
                poursuivre = st.radio("Documentation", [choix_suite, "Nouvelle documentation"], horizontal=True,
                                      label_visibility="collapsed") == choix_suite
            sujet = st.text_input("Fonctionnalité à documenter, ou ce qu'il faut changer si tu poursuis",
                                  value="",
                                  placeholder=("ex. ajoute un cas de test sur la purge, fais-la en anglais…"
                                               if etat.documentation else
                                               f"La tâche {etat.ticket['cle']} (ou précise un angle)" if etat.ticket
                                               else "ex. La libération manuelle des vagues"))
            captures = st.file_uploader("Captures d'écran (facultatif)", type=["png", "jpg", "jpeg", "webp"],
                                        accept_multiple_files=True,
                                        help="Nomme-les d'après l'écran (suivi-vagues.png) : l'agent les place "
                                             "dans les bonnes scènes.")
            avec_video = st.checkbox("Produire la vidéo", value=True)
            lancer = st.form_submit_button("Générer la documentation", type="primary")
        if lancer:
            if not poursuivre and not etat.ticket and not sujet.strip():
                st.error("Charge une tâche Jira ou indique la fonctionnalité à documenter.")
            else:
                _generer_documentation(profil, sujet.strip(), captures, avec_video, poursuivre)
                if etat.documentation:
                    st.rerun()


def _historique(profil: str) -> None:
    """Conversations précédentes du projet, à reprendre là où elles s'étaient arrêtées."""
    etat = st.session_state
    conversations = [c for c in historique.lister(profil) if c["id"] != etat.conversation_id]
    if not conversations:
        return
    choix = st.selectbox("Reprendre une conversation", conversations, index=None, placeholder="Conversations précédentes",
                         format_func=lambda c: f"{historique.libelle_date(c['maj_le'])} · {c['titre']}"
                                               + (" 🎬" if c["documentation"] else ""))
    c1, c2 = st.columns(2)
    if c1.button("Reprendre", width="stretch", disabled=choix is None, type="primary"):
        try:
            reprendre_conversation(choix["id"])
        except config.ConfigError as e:
            st.error(str(e))
        else:
            st.rerun()
    if c2.button("Supprimer", width="stretch", disabled=choix is None,
                 help="Retire cette conversation de l'historique (la documentation produite est gardée)."):
        historique.supprimer(profil, choix["id"])
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
                  disabled=not etat.messages and not etat.documentation)
        _historique(nom)
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
    _sauvegarder()
    if bilan.documentation:
        st.rerun()  # réaffiche la page : vidéo sous la réponse, téléchargements dans le bloc de documentation
