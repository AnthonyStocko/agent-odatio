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

from tools import db_tools, groundcover_tools
from tools.secret_tools import mask_secrets, register_secrets

from . import config

config.retirer_secrets_environnement()  # db_tools vient de charger le .env dans os.environ

FENETRE_LOGS_MAX = groundcover_tools.MAX_WINDOW
LIGNES_LOGS_MAX = groundcover_tools.MAX_LINES


class OutilErreur(Exception):
    pass


@contextmanager
def _environnement(profil: config.Profil):
    valeurs = {}
    if profil.base:
        b = profil.base
        mot_de_passe = config.secret(b.mot_de_passe_env)
        register_secrets([mot_de_passe])
        valeurs.update({"DB_TYPE": b.type, "DB_HOST": b.hote, "DB_PORT": b.port, "DB_NAME": b.nom,
                        "DB_USER": b.utilisateur, "DB_PASSWORD": mot_de_passe, "DB_SCHEMA": b.schema})
    if profil.groundcover_filtre:
        valeurs.update({"CUCUMBER_GROUNDCOVER_FILTER": profil.groundcover_filtre,
                        "GROUNDCOVER_API_KEY": config.secret("GROUNDCOVER_API_KEY"),
                        "GROUNDCOVER_BACKEND_ID": config.secret("GROUNDCOVER_BACKEND_ID"),
                        "GROUNDCOVER_API_URL": config.secret("GROUNDCOVER_API_URL")})
    avant = {k: os.environ.get(k) for k in valeurs}
    os.environ.update(valeurs)
    db_tools.set_profile("")
    try:
        yield
    finally:
        for nom, valeur in avant.items():
            if valeur is None:
                os.environ.pop(nom, None)
            else:
                os.environ[nom] = valeur


def _journal(profil: config.Profil, outil: str, details: dict, erreur: str = "") -> None:
    """Trace des appels (.cache/journal/outils.jsonl) : outil, tables lues, erreur ; jamais le texte d'une
    requête ni les données renvoyées, qui peuvent être nominatives."""
    try:
        dossier = config.CACHE / "journal"
        dossier.mkdir(parents=True, exist_ok=True)
        entree = {"date": datetime.now().isoformat(timespec="seconds"), "profil": profil.nom, "outil": outil,
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
        _journal(profil, outil, details, str(e))
        raise OutilErreur(mask_secrets(str(e))[0])
    except Exception as e:  # pilote absent, réseau…
        message = mask_secrets(f"{type(e).__name__} : {e}")[0]
        _journal(profil, outil, details, message)
        raise OutilErreur(message)
    _journal(profil, outil, details)
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
        _journal(profil, "logs_rechercher", details, str(e))
        raise OutilErreur(mask_secrets(str(e))[0])
    _journal(profil, "logs_rechercher", details)
    return "\n".join(groundcover_tools.format_row(r) for r in lignes) or "(aucune ligne)"


def tester(profil: config.Profil) -> list:
    """[(élément, ok, message)] : connexion base et groundcover, pour la commande --test."""
    resultats = []
    if profil.base:
        with _environnement(profil):
            ok, message = db_tools.test_connection(profil.base.schema)
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
