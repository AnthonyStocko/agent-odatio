"""
Agent fonctionnel Odatio — conversation en ligne de commande.

  python agent.py --liste                         profils disponibles
  python agent.py --profil local --test           vérifie sources, logs, base et groundcover
  python agent.py --profil local                  conversation
  python agent.py --profil local --question "Comment est calculée la date de préparation ?"
  python agent.py --profil local --ticket ODA-1234       conversation sur une tâche Jira à tester

En conversation : /nouveau repart d'une conversation vide, /quitter (ou Ctrl+D) termine.
"""
import argparse
import asyncio
import getpass
import os
import sys

from odatio_agent import config, outils, sources
from odatio_agent.etapes import resumer
from odatio_agent.session import Session

GRIS, GRAS, ROUGE, VERT, FIN = "\033[90m", "\033[1m", "\033[31m", "\033[32m", "\033[0m"


async def _poser(session: Session, question: str, racines: list) -> None:
    def sur_texte(texte):
        print(texte, flush=True)

    def sur_outil(nom, entree):
        print(f"{GRIS}  · {resumer(nom, entree, racines)}{FIN}", flush=True)

    bilan = await session.demander(question, sur_texte, sur_outil)
    for f in bilan.fichiers:
        print(f"{VERT}Fichier généré :{FIN} {f['chemin']} ({f['taille']} octets)")
    cout = f"{bilan.cout_usd:.3f} $" if bilan.cout_usd is not None else "coût inconnu"
    etat = f"{ROUGE}interrompu : {bilan.message}{FIN} — " if bilan.erreur else ""
    print(f"{GRIS}— {etat}{bilan.tours} étape(s), {cout}{FIN}\n")


async def converser(profil: config.Profil, reglages: config.Reglages, question: str, maj: bool,
                    ticket: dict | None = None) -> int:
    prets = sources.preparer(profil, mettre_a_jour=maj)
    logs = sources.dossiers_logs(profil)
    racines = [chemin for _, chemin in prets] + logs
    utilisateur = getpass.getuser()

    session = Session(profil, prets, logs, reglages, utilisateur, ticket=ticket)
    await session.ouvrir()
    try:
        if question:
            await _poser(session, question, racines)
            return 0
        print(f"{GRAS}Agent Odatio — profil « {profil.nom} »{FIN} {GRIS}(/nouveau, /quitter){FIN}\n")
        while True:
            try:
                saisie = (await asyncio.to_thread(input, f"{GRAS}> {FIN}")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not saisie:
                continue
            if saisie in ("/quitter", "/q", "exit"):
                return 0
            if saisie == "/nouveau":
                await session.fermer()
                session = Session(profil, prets, logs, reglages, utilisateur, ticket=ticket)
                await session.ouvrir()
                print(f"{GRIS}Nouvelle conversation.{FIN}\n")
                continue
            print()
            await _poser(session, saisie, racines)
    finally:
        await session.fermer()


def tester(profil: config.Profil, maj: bool) -> int:
    ok_global = True
    try:
        for nom, chemin in sources.preparer(profil, mettre_a_jour=maj):
            print(f"{VERT}✔{FIN} Source « {nom} » : {chemin}")
    except sources.SourceError as e:
        print(f"{ROUGE}✘{FIN} Sources : {e}")
        ok_global = False
    for dossier in sources.dossiers_logs(profil):
        print(f"{VERT}✔{FIN} Logs : {dossier}")
    erreur = config.erreur_dossier_fichiers(profil)
    print(f"{ROUGE + '✘' if erreur else VERT + '✔'}{FIN} Fichiers générés : {erreur or config.dossier_fichiers(profil)}")
    ok_global &= not erreur
    for element, ok, message in outils.tester(profil):
        print(f"{VERT + '✔' if ok else ROUGE + '✘'}{FIN} {element} : {message}")
        ok_global &= ok
    return 0 if ok_global else 1


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if os.name == "nt":
        os.system("")  # active les couleurs ANSI dans la console Windows

    parser = argparse.ArgumentParser(description="Agent fonctionnel Odatio (lecture seule, sauf les fichiers "
                                                 "d'interface demandés)")
    parser.add_argument("--profil", help="profil de profils.toml")
    parser.add_argument("--question", default="", help="pose une seule question puis termine")
    parser.add_argument("--test", action="store_true", help="vérifie la configuration du profil")
    parser.add_argument("--liste", action="store_true", help="liste les profils")
    parser.add_argument("--ticket", default="", help="tâche Jira à tester, donnée à l'agent comme contexte")
    parser.add_argument("--sans-maj", action="store_true", help="ne met pas à jour les dépôts déjà clonés")
    args = parser.parse_args()

    config.charger_secrets()
    try:
        if args.liste or not args.profil:
            noms = config.noms_profils()
            print("Profils : " + (", ".join(noms) or "aucun") + ("" if args.liste else
                  "\nIndique-en un avec --profil <nom>."))
            return 0 if args.liste else 2
        profil = config.charger_profil(args.profil)
        if args.test:
            return tester(profil, not args.sans_maj)
        ticket = outils.jira_ticket(args.ticket, profil) if args.ticket else None
        if ticket:
            print(f"{GRIS}Tâche {ticket['cle']} : {ticket['titre']}{FIN}")
        return asyncio.run(converser(profil, config.reglages(), args.question, not args.sans_maj, ticket))
    except (config.ConfigError, sources.SourceError, outils.OutilErreur) as e:
        print(f"{ROUGE}{e}{FIN}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
