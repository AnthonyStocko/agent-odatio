"""
Outils en lecture seule exposés à l'agent (et au serveur MCP) : base de données et logs groundcover.

Ce module ne fait qu'adapter les modules repris d'agent-savoye (tools/db_tools.py, tools/groundcover_tools.py),
qui lisent leur configuration dans les variables d'environnement : les variables du profil (dont le mot de
passe) y sont posées le temps d'un appel, puis retirées. Chaque fonction renvoie un texte, masqué, ou lève
OutilErreur avec un message sans secret.
"""
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import re

import requests

from tools import db_tools, groundcover_tools, jira_tools
from tools.secret_tools import mask_secrets, register_secrets

from . import config

config.retirer_secrets_environnement()  # db_tools et jira_tools viennent de charger le .env dans os.environ

FENETRE_LOGS_MAX = groundcover_tools.MAX_WINDOW
LIGNES_LOGS_MAX = groundcover_tools.MAX_LINES
TICKET_MAX = 12000                   # caractères du contexte d'un ticket Jira donné à l'agent
CLE_JIRA = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


class OutilErreur(Exception):
    pass


@contextmanager
def _environnement(profil: config.Profil | None):
    valeurs = {"JIRA_DOMAIN": config.secret("JIRA_DOMAIN"), "JIRA_EMAIL": config.secret("JIRA_EMAIL"),
               "JIRA_API_TOKEN": config.secret("JIRA_API_TOKEN")}
    if profil is not None and profil.base:
        b = profil.base
        mot_de_passe = config.secret(b.mot_de_passe_env)
        register_secrets([mot_de_passe])
        valeurs.update({"DB_TYPE": b.type, "DB_HOST": b.hote, "DB_PORT": b.port, "DB_NAME": b.nom,
                        "DB_USER": b.utilisateur, "DB_PASSWORD": mot_de_passe, "DB_SCHEMA": b.schema})
    if profil is not None and profil.groundcover_filtre:
        valeurs.update({"CUCUMBER_GROUNDCOVER_FILTER": profil.groundcover_filtre,
                        "GROUNDCOVER_API_KEY": config.secret("GROUNDCOVER_API_KEY"),
                        "GROUNDCOVER_BACKEND_ID": config.secret("GROUNDCOVER_BACKEND_ID"),
                        "GROUNDCOVER_API_URL": config.secret("GROUNDCOVER_API_URL")})
    avant = {k: os.environ.get(k) for k in valeurs}
    os.environ.update({k: v or "" for k, v in valeurs.items()})
    db_tools.set_profile("")
    try:
        yield
    finally:
        for nom, valeur in avant.items():
            if valeur is None:
                os.environ.pop(nom, None)
            else:
                os.environ[nom] = valeur


def journal(profil: config.Profil | None, outil: str, details: dict, erreur: str = "") -> None:
    """Trace des appels (.cache/journal/outils.jsonl) : outil, tables lues, erreur ; jamais le texte d'une
    requête ni les données renvoyées, qui peuvent être nominatives."""
    try:
        dossier = config.CACHE / "journal"
        dossier.mkdir(parents=True, exist_ok=True)
        entree = {"date": datetime.now().isoformat(timespec="seconds"), "profil": profil.nom if profil else "", "outil": outil,
                  **details, "ok": not erreur, "erreur": erreur[:300]}
        with open(dossier / "outils.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entree, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _base(profil: config.Profil, outil: str, details: dict, appel) -> str:
    if not profil.base:
        raise OutilErreur("Aucune base configurée pour ce profil.")
    try:
        with _environnement(profil):
            texte = appel(profil.base.schema)
    except db_tools.DbError as e:
        journal(profil, outil, details, str(e))
        raise OutilErreur(mask_secrets(str(e))[0])
    except Exception as e:  # pilote absent, réseau…
        message = mask_secrets(f"{type(e).__name__} : {e}")[0]
        journal(profil, outil, details, message)
        raise OutilErreur(message)
    journal(profil, outil, details)
    return mask_secrets(texte)[0]


def base_tables(profil: config.Profil, motif: str = "") -> str:
    return _base(profil, "base_tables", {"motif": motif},
                 lambda schema: db_tools.list_tables(schema, motif))


def base_decrire(profil: config.Profil, table: str) -> str:
    return _base(profil, "base_decrire", {"tables": [table]},
                 lambda schema: db_tools.describe_table(table, schema))


def base_requete(profil: config.Profil, sql: str, limite: int = db_tools.DEFAULT_LIMIT) -> str:
    tables = db_tools.query_tables(sql, profil.base.type if profil.base else "")
    return _base(profil, "base_requete", {"tables": tables},
                 lambda schema: db_tools.run_query(sql, schema, limite))


def _heure(valeur: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(valeur.strip().replace("Z", "+00:00"))
    except ValueError:
        raise OutilErreur(f"Heure illisible : « {valeur} » (ISO 8601 attendu, ex. 2026-10-28T17:28:00Z).")
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def logs_rechercher(profil: config.Profil, debut: str, fin: str, recherche: str = "", limite: int = 50) -> str:
    if not profil.groundcover_filtre:
        raise OutilErreur("groundcover n'est pas configuré pour ce profil (logs.groundcover_filtre).")
    start, end = _heure(debut), _heure(fin)
    if end <= start or end - start > FENETRE_LOGS_MAX:
        raise OutilErreur(f"Fenêtre invalide : la fin doit suivre le début, "
                          f"{int(FENETRE_LOGS_MAX / timedelta(minutes=1))} minutes au plus.")
    details = {"debut": start.isoformat(), "fin": end.isoformat(), "recherche": recherche[:200]}
    try:
        with _environnement(profil):
            lignes = groundcover_tools.search(start, end, recherche, max(1, min(int(limite), LIGNES_LOGS_MAX)))
    except groundcover_tools.GroundcoverError as e:
        journal(profil, "logs_rechercher", details, str(e))
        raise OutilErreur(mask_secrets(str(e))[0])
    journal(profil, "logs_rechercher", details)
    return "\n".join(groundcover_tools.format_row(r) for r in lignes) or "(aucune ligne)"


# ---------------------------------------------------------------------------
# Jira (lecture seule)
# ---------------------------------------------------------------------------

def jira_ticket(cle: str, profil: config.Profil | None = None) -> dict:
    """Ticket Jira et son contexte (épopée parente, fiches COST liées) :
    {"cle", "titre", "texte"} où texte est un Markdown borné à TICKET_MAX caractères."""
    cle = (cle or "").strip().upper()
    if not CLE_JIRA.match(cle):
        raise OutilErreur(f"Clé de ticket invalide : « {cle} » (format attendu : PROJET-123).")
    if not config.jira_configure():
        raise OutilErreur("Jira n'est pas configuré (onglet Configuration).")
    with _environnement(None):
        donnees = jira_tools.get_full_jira_issue_context(cle)
    if donnees.get("error"):
        journal(profil, "jira_ticket", {"ticket": cle}, donnees["error"])
        raise OutilErreur(mask_secrets(donnees["error"])[0])
    journal(profil, "jira_ticket", {"ticket": cle})
    blocs = [f"# {cle} — {donnees.get('summary', '')}"]
    if donnees.get("components"):
        blocs.append("Composants : " + ", ".join(donnees["components"]))
    blocs.append(donnees.get("description") or "(pas de description)")
    epopee = donnees.get("epic") or {}
    if epopee:
        blocs.append(f"## Épopée {epopee.get('key')} — {epopee.get('summary', '')}\n{epopee.get('description') or ''}")
    for fiche in donnees.get("costs") or []:
        blocs.append(f"## Fiche liée {fiche.get('key')} — {fiche.get('summary', '')}\n{fiche.get('description') or ''}")
    texte = mask_secrets("\n\n".join(blocs))[0]
    if len(texte) > TICKET_MAX:
        texte = texte[:TICKET_MAX] + "\n\n[… contexte tronqué]"
    return {"cle": cle, "titre": donnees.get("summary", ""), "texte": texte}


def tester_jira() -> tuple:
    """(ok, message) : identifiants Jira valides ?"""
    if not config.jira_configure():
        return False, "Domaine, e-mail et jeton Jira sont nécessaires."
    domaine = re.sub(r"^https?://", "", config.secret("JIRA_DOMAIN")).strip("/")
    try:
        reponse = requests.get(f"https://{domaine}/rest/api/3/myself", timeout=15,
                               auth=(config.secret("JIRA_EMAIL"), config.secret("JIRA_API_TOKEN")))
    except requests.RequestException as e:
        return False, f"Jira injoignable : {type(e).__name__}"
    if reponse.status_code != 200:
        return False, f"Jira a répondu HTTP {reponse.status_code} (vérifie l'e-mail et le jeton)."
    return True, f"Connecté en tant que {reponse.json().get('displayName', '?')}"


def tester(profil: config.Profil) -> list:
    """[(élément, ok, message)] : connexion base et groundcover, pour la commande --test."""
    resultats = []
    if profil.base:
        with _environnement(profil):
            if db_tools.is_db_configured():
                ok, message = db_tools.test_connection(profil.base.schema)
            else:
                ok, message = False, "connexion incomplète : renseigne le type, l'hôte, la base et l'utilisateur"
        resultats.append(("Base", ok, mask_secrets(message)[0]))
    if profil.groundcover_filtre:
        maintenant = datetime.now(timezone.utc)
        try:
            logs_rechercher(profil, (maintenant - timedelta(minutes=5)).isoformat(), maintenant.isoformat(),
                            limite=1)
            resultats.append(("groundcover", True, "Connexion OK"))
        except OutilErreur as e:
            resultats.append(("groundcover", False, str(e)))
    return resultats
