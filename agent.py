"""
Agent fonctionnel Odatio — conversation en ligne de commande.

  python agent.py --liste                         profils disponibles
  python agent.py --profil local --test           vérifie sources, logs, base et groundcover
  python agent.py --profil local                  conversation
  python agent.py --profil local --question "Comment est calculée la date de préparation ?"
  python agent.py --profil local --ticket ODA-1234       conversation sur une tâche Jira à tester
  python agent.py --profil local --ticket ODA-1234 --documenter [--captures C:/captures]
                                                  documentation et vidéo de la tâche pour le consultant
  python agent.py --profil local --documenter "La libération des vagues"   idem, sur une fonctionnalité
  python agent.py --rendre .cache/documentation/<dossier>    refait document et vidéo après correction
  python agent.py --profil local --conversations          conversations enregistrées (les plus récentes)
  python agent.py --profil local --reprendre <id>         reprend une conversation là où elle s'était arrêtée
  python agent.py --profil local --poursuivre .cache/documentation/<dossier> [--question "ajoute un cas…"]
                                                  poursuit une documentation (avec sa conversation si connue)
En conversation aussi, « fais-moi une vidéo de cette fonctionnalité » produit la documentation.

En conversation : /nouveau repart d'une conversation vide, /quitter (ou Ctrl+D) termine.
"""
import argparse
import asyncio
import getpass
import os
import sys

from pathlib import Path

from odatio_agent import config, documentation, historique, outils, sources, video
from odatio_agent.etapes import resumer
from odatio_agent.session import Session

GRIS, GRAS, ROUGE, VERT, FIN = "\033[90m", "\033[1m", "\033[31m", "\033[32m", "\033[0m"


async def _poser(session: Session, question: str, racines: list, monter: bool = True):
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
    if monter and bilan.documentation:  # documentation demandée en cours de conversation
        rendre(Path(bilan.documentation["dossier"]))
    return bilan


async def converser(profil: config.Profil, reglages: config.Reglages, question: str, maj: bool,
                    ticket: dict | None = None, conversation: dict | None = None,
                    dossier_documentation: Path | None = None) -> int:
    """conversation : conversation de l'historique à reprendre ; dossier_documentation : documentation à
    poursuivre. Chaque échange est enregistré dans l'historique du profil."""
    prets = sources.preparer(profil, mettre_a_jour=maj)
    logs = sources.dossiers_logs(profil)
    racines = [chemin for _, chemin in prets] + logs
    utilisateur = getpass.getuser()
    suivi = dict(conversation or {})
    suivi.setdefault("messages", [])
    ticket = suivi.get("ticket") or ticket
    suivi["ticket"] = ticket
    if dossier_documentation:
        suivi["documentation"] = {"dossier": str(dossier_documentation),
                                  "sujet": documentation.sujet(dossier_documentation)}

    def nouvelle_session(reprise: str = "") -> Session:
        return Session(profil, prets, logs, reglages, utilisateur, reprise=reprise, ticket=ticket,
                       dossier_documentation=dossier_documentation)

    async def poser(texte: str) -> None:
        nonlocal session
        envoye = texte
        if suivi["messages"] and not suivi.get("reprise"):  # fil de l'agent perdu : récapitulatif
            envoye = historique.recapitulatif(suivi["messages"], texte)
        try:
            bilan = await _poser(session, envoye, racines, monter=False)
        except Exception:
            if not suivi.get("reprise"):
                raise
            print(f"{GRIS}Le fil de cette conversation n'est plus disponible côté agent : reprise avec le "
                  f"récapitulatif des échanges.{FIN}")
            await session.fermer()
            session, suivi["reprise"] = nouvelle_session(), ""
            await session.ouvrir()
            bilan = await _poser(session, historique.recapitulatif(suivi["messages"], texte), racines, monter=False)
        suivi["reprise"] = bilan.session_id or suivi.get("reprise", "")
        suivi["messages"] += [{"role": "user", "texte": texte},
                              {"role": "assistant", "texte": bilan.reponse, "fichiers": bilan.fichiers,
                               "documentation": bool(bilan.documentation)}]
        if bilan.documentation:
            suivi["documentation"] = bilan.documentation
            rendre(Path(bilan.documentation["dossier"]))
        suivi["id"] = historique.enregistrer(profil.nom, suivi)

    session = nouvelle_session(suivi.get("reprise", ""))
    await session.ouvrir()
    try:
        if question:
            await poser(question)
            print(f"{GRIS}Conversation {suivi['id']} : --reprendre {suivi['id']} pour la poursuivre.{FIN}")
            return 0
        if suivi["messages"]:
            print(f"{GRIS}Reprise de « {historique.titre(suivi['messages'], ticket)} » "
                  f"({sum(m['role'] == 'user' for m in suivi['messages'])} question(s)).{FIN}")
            dernier = next((m["texte"] for m in reversed(suivi["messages"]) if m["role"] == "assistant"), "")
            if dernier:
                print(f"{GRIS}Dernière réponse :{FIN}\n{dernier}\n")
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
                dossier_documentation = None
                suivi = {"messages": [], "ticket": ticket}
                session = nouvelle_session()
                await session.ouvrir()
                print(f"{GRIS}Nouvelle conversation.{FIN}\n")
                continue
            print()
            await poser(saisie)
    finally:
        await session.fermer()


def rendre(dossier: Path, sans_video: bool = False) -> int:
    try:
        resultat = documentation.produire(dossier, documentation.sujet(dossier), video_aussi=not sans_video,
                                          journal=lambda m: print(f"{GRIS}  · {m}{FIN}", flush=True))
    except (documentation.DocumentationErreur, video.VideoErreur) as e:
        print(f"{ROUGE}{e}{FIN}")
        return 1
    print(f"\n{VERT}✔{FIN} Documentation prête dans {dossier}")
    for nom in ("markdown", "word", "video", "sous_titres"):
        if resultat.get(nom):
            print(f"  {Path(resultat[nom]).name}")
    if not sans_video and not resultat.get("voix"):
        print(f"{GRIS}Aucune voix trouvée pour la langue du document : vidéo muette, narration en sous-titres.{FIN}")
    return 0


async def documenter(profil: config.Profil, reglages: config.Reglages, sujet: str, ticket: dict | None, maj: bool,
                     dossier_captures: str = "", sans_video: bool = False) -> int:
    titre = f"{ticket['cle']} — {ticket['titre']}" if ticket else sujet
    dossier = documentation.nouveau_dossier(ticket["cle"] if ticket else sujet)
    (dossier / "sujet.txt").write_text(titre, encoding="utf-8")
    if dossier_captures:
        noms = documentation.copier_captures(dossier, Path(dossier_captures))
        print(f"{GRIS}{len(noms)} capture(s) : {', '.join(noms) or 'aucune'}{FIN}")
    prets = sources.preparer(profil, mettre_a_jour=maj)
    logs = sources.dossiers_logs(profil)
    racines = [chemin for _, chemin in prets] + logs
    session = Session(profil, prets, logs, reglages, getpass.getuser(), ticket=ticket, dossier_documentation=dossier)
    await session.ouvrir()
    try:
        bilan = await _poser(session, documentation.question(sujet, ticket), racines, monter=False)
    finally:
        await session.fermer()
    historique.enregistrer(profil.nom, {
        "reprise": bilan.session_id, "ticket": ticket, "documentation": bilan.documentation,
        "messages": [{"role": "user", "texte": f"Documentation : {titre}"},
                     {"role": "assistant", "texte": bilan.reponse, "documentation": bool(bilan.documentation)}]})
    if not bilan.documentation:
        print(f"{ROUGE}L'agent n'a pas enregistré de document (étapes ou budget épuisés ?). Relance avec un plafond "
              f"plus haut, ou précise le sujet.{FIN}")
        return 1
    return rendre(dossier, sans_video)


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
    parser.add_argument("--documenter", nargs="?", const="", default=None, metavar="SUJET",
                        help="documentation et vidéo pour le consultant (de la tâche --ticket, ou du SUJET)")
    parser.add_argument("--captures", default="", help="dossier de captures d'écran à intégrer à la documentation")
    parser.add_argument("--sans-video", action="store_true", help="documentation écrite seulement")
    parser.add_argument("--conversations", action="store_true", help="liste les conversations enregistrées")
    parser.add_argument("--reprendre", default="", metavar="ID", help="reprend une conversation enregistrée")
    parser.add_argument("--poursuivre", default="", metavar="DOSSIER",
                        help="poursuit une documentation déjà produite (avec --question pour la demande)")
    parser.add_argument("--rendre", default="", metavar="DOSSIER",
                        help="refait document et vidéo d'un dossier de documentation (après correction de document.json)")
    args = parser.parse_args()

    config.charger_secrets()
    if args.rendre:
        return rendre(Path(args.rendre), args.sans_video)
    try:
        if args.liste or not args.profil:
            noms = config.noms_profils()
            print("Profils : " + (", ".join(noms) or "aucun") + ("" if args.liste else
                  "\nIndique-en un avec --profil <nom>."))
            return 0 if args.liste else 2
        profil = config.charger_profil(args.profil)
        if args.test:
            return tester(profil, not args.sans_maj)
        if args.conversations:
            for c in historique.lister(profil.nom):
                doc = " [documentation]" if c["documentation"] else ""
                print(f"{c['id']}  {historique.libelle_date(c['maj_le'])}  {c['titre']}{doc}")
            return 0
        conversation, dossier = None, None
        if args.poursuivre:
            dossier = Path(args.poursuivre)
            if not (dossier / "document.json").is_file():
                print(f"{ROUGE}Aucune documentation dans {dossier}.{FIN}")
                return 2
            conversation = historique.de_la_documentation(profil.nom, str(dossier))
            if args.question:
                args.question = documentation.question_suite(args.question)
        if args.reprendre:
            conversation = historique.charger(profil.nom, args.reprendre)
        ticket = outils.jira_ticket(args.ticket, profil) if args.ticket else None
        if ticket:
            print(f"{GRIS}Tâche {ticket['cle']} : {ticket['titre']}{FIN}")
        if args.documenter is not None:
            if not ticket and not args.documenter.strip():
                print(f"{ROUGE}Indique la tâche (--ticket) ou la fonctionnalité à documenter (--documenter \"…\").{FIN}")
                return 2
            return asyncio.run(documenter(profil, config.reglages(), args.documenter.strip(), ticket,
                                          not args.sans_maj, args.captures, args.sans_video))
        return asyncio.run(converser(profil, config.reglages(), args.question, not args.sans_maj, ticket,
                                     conversation, dossier))
    except (config.ConfigError, sources.SourceError, outils.OutilErreur) as e:
        print(f"{ROUGE}{e}{FIN}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
