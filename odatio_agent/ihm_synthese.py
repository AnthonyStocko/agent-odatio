"""
Page « Synthèse client » de l'IHM : dossier de synthèse d'un projet pour le démarrage et le passage au support
édition (synthese.py). Même projet que la page Assistant (st.session_state.profil_actif).

Arrêt et reprise : un clic pendant la génération (bouton « Arrêter », ou tout autre) fait rejouer le script par
Streamlit, qui interrompt la génération au prochain affichage de progression. L'état est écrit après chaque lot :
seul le lot en cours est perdu, et « Reprendre » repart du lot suivant. Fermer l'onglet ou un plantage ont le même
effet.
"""
import asyncio
import getpass
from pathlib import Path

import streamlit as st

from . import config, historique, ihm_assistant, sources
from . import synthese as synth
from .session import Session

MIME_WORD = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _creer_session(profil, prets, logs, dossier: Path):
    def creer(mode: str, numero: int) -> Session:
        return Session(profil, prets, logs, config.reglages(), getpass.getuser(),
                       synthese={"mode": mode, "numero": numero, "dossier": str(dossier)})
    return creer


def _executer(profil, dossier: Path, prets, logs) -> None:
    statut = st.status("Synthèse en cours…", expanded=True)
    etapes = []

    def sur_etape(texte: str) -> None:      # chaque affichage est aussi un point d'arrêt pour Streamlit
        etapes.append(texte)
        statut.write(texte)
        if texte.startswith("Synthèse :"):
            statut.update(label=texte)

    try:
        e = asyncio.run(synth.executer(dossier, _creer_session(profil, prets, logs, dossier), sur_etape))
    except synth.SyntheseErreur as err:
        statut.update(label="Synthèse interrompue", state="error")
        st.error(str(err))
        return
    except Exception as err:  # Claude Code introuvable, authentification…
        statut.update(label="Échec", state="error")
        st.error(f"La synthèse s'est arrêtée : {err}. Les lots terminés sont gardés : « Reprendre » repart de là.")
        return
    if e.get("fichiers"):
        statut.update(label="Synthèse prête" + (" (partielle : des lots restent à refaire)" if e.get("partiel")
                                                 else ""), state="complete", expanded=False)
    else:
        statut.update(label="Synthèse incomplète : « Reprendre » relance les étapes restantes", state="error")


def _nouvelle(profil, standard: str) -> None:
    try:
        prets, logs = ihm_assistant._preparer(profil)
    except sources.SourceError as e:
        st.error(str(e))
        return
    with st.spinner("Inventaire des éléments spécifiques (sans appel au modèle)…"):
        try:
            inventaire = synth.inventorier(profil.nom, [(n, str(c)) for n, c in prets], standard)
        except synth.SyntheseErreur as e:
            st.error(str(e))
            return
    if not inventaire["elements"]:
        st.warning("Aucun élément spécifique trouvé : les sources spécifiques sont identiques au Standard.")
        return
    dossier = synth.creer(profil.nom, inventaire, config.reglages().synthese_lot)
    st.session_state.synthese_dossier = str(dossier)
    st.info(f"{len(inventaire['elements'])} élément(s) spécifique(s) inventorié(s), "
            f"{len(synth.etat(dossier)['lots'])} lot(s) à expliquer.")
    _executer(profil, dossier, prets, logs)


def _reprendre(profil, dossier: Path) -> None:
    try:
        prets, logs = ihm_assistant._preparer(profil)
    except sources.SourceError as e:
        st.error(str(e))
        return
    _executer(profil, dossier, prets, logs)


def _afficher(dossier: Path) -> None:
    try:
        e = synth.etat(dossier)
        inventaire = synth.inventaire(dossier)
    except synth.SyntheseErreur as err:
        st.error(str(err))
        return
    faits = sum(1 for l in e["lots"] if l["statut"] == "fait")
    erreurs = [l for l in e["lots"] if l["statut"] == "erreur"]
    total = len(e["lots"])
    st.progress((faits + (e["finale"].get("statut") == "fait")) / (total + 1),
                text=f"{len(inventaire['elements'])} élément(s) · lots {faits}/{total} · partie finale "
                     f"{'faite' if e['finale'].get('statut') == 'fait' else 'à faire'} · "
                     f"{e.get('cout_usd') or 0:.2f} $ · {round((e.get('duree_s') or 0) / 60, 1)} min")
    for lot in erreurs:
        st.caption(f"Lot {lot['numero']} non enregistré : {lot.get('message') or 'à refaire'}.")

    rapport = Path(dossier) / "rapport.md"
    if not rapport.is_file():
        return
    texte = rapport.read_text(encoding="utf-8")
    st.warning("**À relire avant envoi au support** : le contrôle automatique écarte secrets, e-mails et "
               "téléphones, pas les noms de personnes ; vérifie aussi les lignes « déduit ».",
               icon=":material/rate_review:")
    if e.get("partiel"):
        st.caption("Synthèse partielle : des lots restent à refaire (« Reprendre »).")
    colonnes = st.columns(3)
    colonnes[0].download_button("Télécharger (.md)", texte, file_name=f"synthese-{_nom(dossier)}.md",
                                mime="text/markdown", width="stretch")
    word = Path(dossier) / "rapport.docx"
    if word.is_file():
        colonnes[1].download_button("Word", word.read_bytes(), file_name=f"synthese-{_nom(dossier)}.docx",
                                    mime=MIME_WORD, width="stretch")
    with st.expander("Copier (Markdown)"):
        st.code(texte, language="markdown")
    st.markdown(texte)


def _nom(dossier: Path) -> str:
    return Path(dossier).name


def page() -> None:
    etat = ihm_assistant._etat()
    etat.setdefault("synthese_dossier", "")
    st.title("Synthèse client")
    st.caption("Dossier pour le démarrage et le passage au support édition : identité du projet, développements "
               "spécifiques, configuration en place, audit, points à vérifier, couverture. Lecture seule ; chaque "
               "ligne cite sa source et dit si elle est vérifiée ou déduite.")
    noms = config.noms_profils()
    if not noms:
        st.info("Aucun projet configuré : décris-le dans la page Configuration.")
        return
    nom = st.selectbox("Projet", noms, index=noms.index(etat.profil_actif) if etat.profil_actif in noms else 0)
    if nom != etat.profil_actif:          # même projet que la page Assistant
        etat.profil_actif = nom
        ihm_assistant.nouvelle_conversation()
        etat.synthese_dossier = ""
    try:
        profil = config.charger_profil(nom)
    except config.ConfigError as e:
        st.error(str(e))
        return

    noms_sources = [s.nom for s in profil.sources]
    if len(noms_sources) < 2:
        st.warning("Il faut au moins deux sources dans ce projet : le Standard et le spécifique du client.")
        return
    defaut = synth.source_standard([(n, "") for n in noms_sources])
    standard = st.selectbox("Source Standard", noms_sources,
                            index=noms_sources.index(defaut) if defaut in noms_sources else 0,
                            help="Les autres sources sont traitées comme spécifiques au client.")
    try:
        synth.verifier_standard(profil, standard)
        refus = ""
    except synth.SyntheseErreur as e:
        refus = str(e)
        st.error(refus)

    precedentes = synth.lister(nom)
    if precedentes and not etat.synthese_dossier:
        etat.synthese_dossier = precedentes[0]["dossier"]
    if precedentes:
        dossiers = [p["dossier"] for p in precedentes]
        choix = st.selectbox(
            "Synthèses du projet", precedentes,
            index=dossiers.index(etat.synthese_dossier) if etat.synthese_dossier in dossiers else 0,
            format_func=lambda p: f"{historique.libelle_date(p['cree_le'])} — "
                                  + ("terminée" if p["termine"] else f"lots {p['faits']}/{p['lots']}"))
        etat.synthese_dossier = choix["dossier"]

    dossier = Path(etat.synthese_dossier) if etat.synthese_dossier else None
    try:
        en_cours = dossier is not None and not synth.etat(dossier).get("termine")
    except synth.SyntheseErreur:
        dossier, en_cours, etat.synthese_dossier = None, False, ""
    with st.container(horizontal=True):
        nouvelle = st.button("Générer une nouvelle synthèse", type="primary", disabled=bool(refus))
        reprendre = en_cours and st.button("Reprendre", help="Repart du premier lot non terminé.")
        st.button("Arrêter", type="tertiary", help="Interrompt la génération : le lot en cours est perdu, les "
                                                    "lots terminés sont gardés.")
    st.caption(f"Réglages (page Configuration) : {config.reglages().synthese_lot} éléments par lot, "
               f"{config.reglages().requetes_base_synthese} accès base et {config.reglages().synthese_max_tours} "
               f"étapes au plus par session ; le coût maximal par question s'applique à chaque session.")

    if nouvelle:
        _nouvelle(profil, standard)
        dossier = Path(etat.synthese_dossier) if etat.synthese_dossier else None
    elif reprendre and dossier is not None:
        _reprendre(profil, dossier)
    if dossier is not None:
        _afficher(dossier)
