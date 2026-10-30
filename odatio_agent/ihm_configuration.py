"""
Page « Configuration » de l'IHM :
- Projets : les applications Odatio sur lesquelles tu travailles (sources, logs, base), écrites dans profils.toml ;
- Claude : compte utilisé (abonnement connecté ou clé API), modèle, effort et plafonds ;
- Accès : Jira, Bitbucket et groundcover, avec reprise possible des réglages d'agent-savoye.
Mots de passe, jetons et clés vont dans le .env, jamais dans profils.toml.
"""
import importlib.util
import os
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import dotenv_values

from . import comptes_claude, config, outils, sources

NOUVEAU = "Nouveau projet…"
TYPES_BASE = ["", "postgresql", "oracle", "sqlserver", "mysql"]
PILOTES = {"postgresql": (("psycopg2", "psycopg"), "psycopg2-binary"), "oracle": (("oracledb",), "oracledb"),
           "sqlserver": (("pymssql", "pyodbc"), "pymssql"), "mysql": (("pymysql",), "pymysql")}
COLONNES_SOURCES = ["nom", "chemin", "depot", "branche"]
REPRISE_SAVOYE = ["JIRA_DOMAIN", "JIRA_EMAIL", "JIRA_API_TOKEN", "BITBUCKET_HTTP_TOKEN", "GROUNDCOVER_API_KEY",
                  "GROUNDCOVER_BACKEND_ID", "GROUNDCOVER_API_URL", "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY"]
MODELES = ["", "sonnet", "opus", "haiku"]
EFFORTS = ["", "low", "medium", "high", "xhigh", "max"]


def _lignes(texte: str) -> list:
    return [l.strip() for l in (texte or "").splitlines() if l.strip()]


def _pilote_manquant(type_base: str) -> str:
    """Paquet pip à installer si aucun pilote de ce type de base n'est présent, sinon ''."""
    modules, paquet = PILOTES.get(type_base, ((), ""))
    if not modules or any(importlib.util.find_spec(m) for m in modules):
        return ""
    return paquet


def _afficher_resultats() -> None:
    for element, ok, message in st.session_state.pop("resultats_test", []):
        (st.success if ok else st.error)(f"{element} : {message}")


# ---------------------------------------------------------------------------
# Projets
# ---------------------------------------------------------------------------

def _onglet_projets() -> None:
    etat = st.session_state
    try:
        profils = config.tous_les_profils()
    except config.ConfigError as e:
        st.error(f"{e}\n\nCorrige profils.toml à la main, ou renomme-le pour repartir d'une configuration vide.")
        return
    noms = [p.nom for p in profils]
    if "projet_a_selectionner" in etat:
        etat.config_projet = etat.pop("projet_a_selectionner")
    if etat.get("config_projet") not in noms + [NOUVEAU]:
        etat.config_projet = noms[0] if noms else NOUVEAU

    choix = st.selectbox("Projet", noms + [NOUVEAU], key="config_projet")
    existant = next((p for p in profils if p.nom == choix), None)
    p = existant or config.Profil(nom="")
    base = p.base or config.Base(type="")
    mot_de_passe_connu = bool(p.base and config.secret(p.base.mot_de_passe_env))
    _afficher_resultats()

    with st.form(f"projet-{choix}"):
        c1, c2 = st.columns([1, 2])
        nom = c1.text_input("Nom court", value=p.nom, placeholder="canson-local",
                            help="Lettres, chiffres, - et _, sans espace. C'est le nom affiché dans l'assistant.")
        description = c2.text_input("Description", value=p.description, placeholder="Canson, branche dev, sur mon poste")

        st.markdown("**Sources**")
        st.caption("Une ligne par dépôt (par exemple le Standard et le spécifique client). Renseigne soit un "
                   "dossier local, soit l'URL d'un dépôt Git et sa branche : il sera cloné automatiquement.")
        lignes = [{k: getattr(s, k) for k in COLONNES_SOURCES} for s in p.sources] or [dict.fromkeys(COLONNES_SOURCES, "")]
        tableau = st.data_editor(
            pd.DataFrame(lignes, columns=COLONNES_SOURCES).astype(str), num_rows="dynamic",
            width="stretch", hide_index=True, key=f"sources-{choix}",
            column_config={
                "nom": st.column_config.TextColumn("Nom", help="Ex. standard, canson"),
                "chemin": st.column_config.TextColumn("Dossier local", help="Ex. C:/development/branch_dev", width="large"),
                "depot": st.column_config.TextColumn("Dépôt Git", help="Ex. https://git.kiscloud.io/scm/…/….git", width="large"),
                "branche": st.column_config.TextColumn("Branche", help="Pour un dépôt Git ; vide = branche par défaut"),
            })

        st.markdown("**Logs**")
        dossiers_logs = st.text_area("Dossiers de logs", value="\n".join(p.dossiers_logs), height=80,
                                     placeholder="C:/serveur/odatio/logs",
                                     help="Un dossier par ligne : ceux où l'application écrit ses fichiers .log.")
        groundcover = st.text_input("Filtre groundcover (facultatif)", value=p.groundcover_filtre,
                                    placeholder="namespace:canson-recette",
                                    help="Pour une application déployée : filtre gcQL imposé à toute recherche de logs.")

        st.markdown("**Base de données** (compte en lecture seule de préférence)")
        c1, c2, c3 = st.columns([1, 2, 1])
        type_base = c1.selectbox("Type", TYPES_BASE, index=TYPES_BASE.index(base.type) if base.type in TYPES_BASE else 0,
                                 format_func=lambda t: t or "Aucune base")
        hote = c2.text_input("Hôte", value=base.hote, placeholder="localhost")
        port = c3.text_input("Port", value=base.port, placeholder="5432")
        c1, c2 = st.columns(2)
        nom_base = c1.text_input("Base", value=base.nom, placeholder="oxygen25")
        schema = c2.text_input("Schéma", value=base.schema, placeholder="cansond")
        utilisateur = c1.text_input("Utilisateur", value=base.utilisateur)
        mot_de_passe = c2.text_input("Mot de passe", type="password",
                                     placeholder="inchangé" if mot_de_passe_connu else "",
                                     help="Enregistré dans le .env. Laisse vide pour garder le mot de passe actuel.")

        interdits = st.text_area("Fichiers que l'agent ne doit jamais lire", value="\n".join(p.fichiers_interdits),
                                 height=80, placeholder="hibernate*.properties",
                                 help="Un motif par ligne, en plus des .env, clés et keystores toujours refusés. "
                                      "Ajoute les fichiers de configuration qui contiennent des mots de passe.")

        c1, c2, _ = st.columns([1, 1, 2])
        enregistrer = c1.form_submit_button("Enregistrer", type="primary", width="stretch")
        tester = c2.form_submit_button("Enregistrer et tester", width="stretch")

    if type_base and _pilote_manquant(type_base):
        st.warning(f"Le pilote {type_base} n'est pas installé. Dans PowerShell : "
                   f"`venv\\Scripts\\python.exe -m pip install {_pilote_manquant(type_base)}`, puis relance l'IHM.")

    if enregistrer or tester:
        nom = nom.strip()
        liste_sources, erreurs, avertissements = [], [], []
        for i, ligne in enumerate(tableau.fillna("").to_dict("records"), start=1):
            ligne = {k: str(ligne.get(k, "")).strip() for k in COLONNES_SOURCES}
            if not ligne["chemin"] and not ligne["depot"]:
                continue
            if ligne["chemin"] and ligne["depot"]:
                erreurs.append(f"Source n° {i} : renseigne soit un dossier, soit un dépôt, pas les deux.")
                continue
            defaut = Path(ligne["chemin"]).name if ligne["chemin"] else ligne["depot"].rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
            liste_sources.append(config.Source(nom=ligne["nom"] or defaut, chemin=ligne["chemin"],
                                               depot=ligne["depot"], branche=ligne["branche"]))
            if ligne["chemin"] and not Path(ligne["chemin"]).expanduser().is_dir():
                avertissements.append((f"Source {ligne['nom'] or defaut}", False,
                                       f"le dossier {ligne['chemin']} n'existe pas sur ce poste"))
        if not liste_sources:
            erreurs.append("Ajoute au moins une source (dossier local ou dépôt Git).")
        if erreurs:
            for e in erreurs:
                st.error(e)
            return

        nouvelle_base = None
        if type_base:
            variable = config.nom_mot_de_passe(nom)
            if mot_de_passe:
                config.enregistrer_secrets({variable: mot_de_passe})
            elif existant and existant.base and existant.base.mot_de_passe_env != variable:
                # projet renommé : le mot de passe suit le nouveau nom
                config.enregistrer_secrets({variable: config.secret(existant.base.mot_de_passe_env)})
            nouvelle_base = config.Base(type=type_base, hote=hote.strip(), port=port.strip(), nom=nom_base.strip(),
                                        utilisateur=utilisateur.strip(), schema=schema.strip(), mot_de_passe_env=variable)
        nouveau = config.Profil(nom=nom, description=description.strip(), sources=liste_sources,
                                dossiers_logs=_lignes(dossiers_logs), groundcover_filtre=groundcover.strip(),
                                base=nouvelle_base, fichiers_interdits=_lignes(interdits))
        liste = [nouveau if x.nom == choix else x for x in profils] if existant else profils + [nouveau]
        try:
            config.enregistrer(config.reglages(), liste)
        except config.ConfigError as e:
            st.error(str(e))
            return
        etat.preparation = {}
        etat.projet_a_selectionner = nom
        resultats = [("Projet", True, f"« {nom} » enregistré")] + (avertissements if not tester else [])
        if tester:
            with st.spinner("Test des connexions…"):
                profil = config.charger_profil(nom)
                for s in profil.sources:
                    if s.chemin:
                        ok = Path(s.chemin).expanduser().is_dir()
                        resultats.append((f"Source {s.nom}", ok, "dossier trouvé" if ok else "dossier introuvable"))
                    else:
                        try:
                            sources.preparer(config.Profil(nom=nom, sources=[s]), journal=lambda _: None)
                            resultats.append((f"Source {s.nom}", True, "dépôt cloné ou à jour"))
                        except sources.SourceError as e:
                            resultats.append((f"Source {s.nom}", False, str(e)))
                for d in profil.dossiers_logs:
                    ok = Path(d).expanduser().is_dir()
                    resultats.append(("Logs", ok, f"{d} {'trouvé' if ok else 'introuvable'}"))
                resultats += outils.tester(profil)
        etat.resultats_test = resultats
        st.rerun()

    if existant:
        with st.popover("Supprimer ce projet"):
            st.markdown(f"Supprimer « {choix} » ? Ses sources et sa base ne sont pas touchées : seule sa "
                        f"configuration disparaît.")
            if st.button("Supprimer", type="primary"):
                config.enregistrer(config.reglages(), [x for x in profils if x.nom != choix])
                if existant.base:
                    config.enregistrer_secrets({existant.base.mot_de_passe_env: ""})
                autres = [x.nom for x in profils if x.nom != choix]
                etat.projet_a_selectionner = autres[0] if autres else NOUVEAU
                etat.preparation = {}
                st.rerun()


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300, show_spinner="Lecture du compte Claude…")
def _compte(dossier: str) -> str:
    return comptes_claude.compte_connecte(dossier)


def _onglet_claude() -> None:
    compte = config.compte_claude()
    mode = st.radio("Compte utilisé", ["compte", "cle"], index=0 if compte["mode"] == "compte" else 1,
                    horizontal=True, format_func=lambda m: {"compte": "Mon abonnement Claude",
                                                            "cle": "Une clé API"}[m])
    dossier, cle = compte["dossier"], compte["cle"]
    defaut = os.path.join(os.path.expanduser("~"), ".claude")

    if mode == "compte":
        options = comptes_claude.dossiers() or [defaut]
        actuel = os.path.expanduser(dossier) if dossier else defaut
        if actuel not in options:
            options.append(actuel)

        def libelle(d: str) -> str:
            qui = _compte(d)
            return f"{d}  ({qui})" if qui else d

        dossier = st.selectbox(
            "Compte Claude", options, index=options.index(actuel), format_func=libelle, accept_new_options=True,
            help="Chaque compte (Pro, Max, Team) a son dossier de configuration : ~/.claude pour le compte par défaut, "
                 "~/.claude-<nom> pour les autres, comme dans agent-savoye. Saisis un nouveau dossier pour ajouter un "
                 "compte, puis connecte-le.")
        c1, c2, _ = st.columns([1, 1, 2])
        if c1.button("Actualiser", width="stretch"):
            _compte.clear()
            st.rerun()
        if c2.button("Connecter ce compte", width="stretch"):
            if comptes_claude.lancer_connexion(dossier):
                _compte.clear()
                st.info("Une fenêtre s'est ouverte : connecte-toi dans le navigateur, puis clique sur Actualiser.")
            else:
                st.info(f"Dans un terminal : `CLAUDE_CONFIG_DIR={dossier} {comptes_claude.executable_claude()} auth login`")
    else:
        cle = st.text_input("Clé API Anthropic", value=cle, type="password", placeholder="sk-ant-…",
                            help="Clé de l'organisation (console Anthropic). Facturée à l'usage.")

    st.markdown("**Modèle et coût**")
    reg = config.reglages()
    c1, c2 = st.columns(2)
    modele = c1.selectbox("Modèle", MODELES if reg.modele in MODELES else MODELES + [reg.modele],
                          index=(MODELES + [reg.modele]).index(reg.modele), accept_new_options=True,
                          format_func=lambda m: m or "Par défaut du compte",
                          help="sonnet : bon équilibre ; opus : questions difficiles ; haiku : rapide et économique.")
    effort = c2.selectbox("Effort de réflexion", EFFORTS, index=EFFORTS.index(reg.effort) if reg.effort in EFFORTS else 0,
                          format_func=lambda e: e or "Par défaut")
    c1, c2, c3 = st.columns(3)
    budget = c1.number_input("Coût maximal par question ($)", min_value=0.0, max_value=50.0, step=0.5,
                             value=float(reg.budget_usd), help="0 = aucun plafond. Sans effet sur un abonnement.")
    max_tours = c2.number_input("Étapes maximales par question", min_value=5, max_value=200, value=int(reg.max_tours))
    requetes = c3.number_input("Accès base par question", min_value=0, max_value=100, value=int(reg.requetes_base))

    if st.button("Enregistrer", type="primary", key="enregistrer-claude"):
        config.enregistrer_secrets({"CLAUDE_AUTH": mode, "CLAUDE_CONFIG_DIR": dossier if mode == "compte" else compte["dossier"],
                                    "ANTHROPIC_API_KEY": cle})
        try:
            config.enregistrer(config.Reglages(modele=modele or "", effort=effort, max_tours=int(max_tours),
                                               budget_usd=float(budget), requetes_base=int(requetes)),
                               config.tous_les_profils())
            st.success("Réglages Claude enregistrés : ils s'appliquent dès la prochaine question.")
        except config.ConfigError as e:
            st.error(str(e))


# ---------------------------------------------------------------------------
# Accès : Jira, Bitbucket, groundcover
# ---------------------------------------------------------------------------

def _dossier_savoye_probable() -> str:
    for candidat in (Path("C:/agent-savoye"), config.RACINE.parent / "agent-savoye", Path.home() / "agent-savoye"):
        if (candidat / ".env").exists():
            return str(candidat)
    return ""


def _onglet_acces() -> None:
    with st.expander("Reprendre les réglages d'agent-savoye"):
        st.caption("Copie les accès Jira, Bitbucket, groundcover et le compte Claude déjà renseignés dans agent-savoye.")
        dossier = st.text_input("Dossier d'agent-savoye", value=_dossier_savoye_probable(), placeholder="C:/agent-savoye")
        if st.button("Reprendre"):
            fichier = Path(dossier) / ".env"
            if not fichier.exists():
                st.error(f"Aucun fichier .env dans {dossier}.")
            else:
                valeurs = {k: v for k, v in dotenv_values(fichier).items() if k in REPRISE_SAVOYE and (v or "").strip()}
                if valeurs.get("ANTHROPIC_API_KEY") or valeurs.get("CLAUDE_CONFIG_DIR"):
                    valeurs["CLAUDE_AUTH"] = "cle" if valeurs.get("ANTHROPIC_API_KEY") else "compte"
                config.enregistrer_secrets(valeurs)
                st.success(f"{len(valeurs)} réglage(s) repris : " + ", ".join(sorted(valeurs)))

    with st.form("jira"):
        st.markdown("**Jira**")
        st.caption("Pour charger une tâche à tester dans l'assistant. Lecture seule.")
        domaine = st.text_input("Domaine", value=config.secret("JIRA_DOMAIN"), placeholder="savoye.atlassian.net")
        email = st.text_input("E-mail", value=config.secret("JIRA_EMAIL"), placeholder="prenom.nom@savoye.com")
        jeton = st.text_input("Jeton d'API", value=config.secret("JIRA_API_TOKEN"), type="password",
                              help="id.atlassian.com, Sécurité, Créer et gérer les jetons d'API.")
        c1, c2, _ = st.columns([1, 1, 2])
        enregistrer = c1.form_submit_button("Enregistrer", type="primary", width="stretch")
        tester = c2.form_submit_button("Enregistrer et tester", width="stretch")
    if enregistrer or tester:
        config.enregistrer_secrets({"JIRA_DOMAIN": domaine.strip().removeprefix("https://").strip("/"),
                                    "JIRA_EMAIL": email, "JIRA_API_TOKEN": jeton})
        if tester:
            ok, message = outils.tester_jira()
            (st.success if ok else st.error)(f"Jira : {message}")
        else:
            st.success("Accès Jira enregistrés.")

    with st.form("autres"):
        st.markdown("**Bitbucket**")
        st.caption("Jeton HTTP en lecture, pour cloner les dépôts Git des projets.")
        bitbucket = st.text_input("Jeton HTTP Bitbucket", value=config.secret("BITBUCKET_HTTP_TOKEN"), type="password")
        st.markdown("**groundcover**")
        st.caption("Pour les projets qui ont un filtre groundcover.")
        c1, c2 = st.columns(2)
        gc_cle = c1.text_input("Clé d'API", value=config.secret("GROUNDCOVER_API_KEY"), type="password")
        gc_backend = c2.text_input("Backend (comptes multi-backend)", value=config.secret("GROUNDCOVER_BACKEND_ID"))
        gc_url = st.text_input("URL de l'API", value=config.secret("GROUNDCOVER_API_URL"),
                               placeholder="https://api.groundcover.com")
        if st.form_submit_button("Enregistrer", type="primary"):
            config.enregistrer_secrets({"BITBUCKET_HTTP_TOKEN": bitbucket, "GROUNDCOVER_API_KEY": gc_cle,
                                        "GROUNDCOVER_BACKEND_ID": gc_backend, "GROUNDCOVER_API_URL": gc_url})
            st.success("Accès enregistrés.")


def page() -> None:
    st.title("Configuration")
    projets, claude, acces = st.tabs(["Projets", "Claude", "Accès"])
    with projets:
        _onglet_projets()
    with claude:
        _onglet_claude()
    with acces:
        _onglet_acces()
