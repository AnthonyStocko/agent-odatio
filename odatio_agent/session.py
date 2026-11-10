"""
Session de conversation avec l'agent (Claude Agent SDK) : outils de fichiers limités à Read, Grep et Glob,
outils maison base, logs et fichiers d'interface exposés en serveur MCP dans le processus
(create_sdk_mcp_server), garde-fou sur les chemins, aucun réglage utilisateur de Claude Code chargé.

La seule écriture possible est fichier_ecrire, dans le dossier des fichiers générés du profil, après
generation_demarrer : cet appel annonce une génération demandée par l'utilisateur et porte le plafond d'accès
base de la question à reglages.requetes_base_generation.
"""
from dataclasses import dataclass, field

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, ResultMessage,
                              TextBlock, ToolUseBlock, create_sdk_mcp_server, tool)

from pathlib import Path

from . import bitbucket, config, consignes, documentation, fichiers, outils
from .garde import creer_garde

SERVEUR = "odatio"
OUTILS_FICHIERS = ["Read", "Grep", "Glob"]


@dataclass
class Bilan:
    cout_usd: float | None
    tours: int
    erreur: bool
    message: str = ""
    session_id: str = ""
    reponse: str = ""      # texte final de la réponse (sans les commentaires intermédiaires)
    fichiers: list = field(default_factory=list)  # fichiers écrits pendant la question (fichiers.ecrire)


def _texte(texte: str, erreur: bool = False) -> dict:
    return {"content": [{"type": "text", "text": texte}], "is_error": erreur}


class Session:
    def __init__(self, profil: config.Profil, sources: list, logs: list, reglages: config.Reglages,
                 utilisateur: str = "", reprise: str = "", ticket: dict | None = None,
                 dossier_documentation: Path | None = None):
        """reprise : identifiant d'une conversation précédente à poursuivre (Bilan.session_id), utilisé par
        l'IHM qui ouvre une session par question. ticket : tâche Jira à tester (outils.jira_ticket), donnée
        à l'agent comme contexte. dossier_documentation : mode documentation (documentation.py), l'agent y
        enregistre son document avec l'outil documentation_enregistrer."""
        self.profil, self.reglages = profil, reglages
        self.requetes_base, self.generation, self.fichiers = 0, False, []
        self.dossier_documentation = Path(dossier_documentation) if dossier_documentation else None
        self.document = None   # document accepté par documentation_enregistrer
        racines = [chemin for _, chemin in sources] + list(logs)
        sortie = config.dossier_fichiers(profil)
        sortie.mkdir(parents=True, exist_ok=True)
        outils_maison = self._outils_maison()
        noms_maison = [f"mcp__{SERVEUR}__{t.name}" for t in outils_maison]

        self.options = ClaudeAgentOptions(
            system_prompt=consignes.construire(profil, sources, logs, utilisateur, reglages, ticket,
                                               documentation.captures(self.dossier_documentation)
                                               if self.dossier_documentation else None),
            tools=OUTILS_FICHIERS,                       # aucun autre outil intégré : ni Bash, ni Edit, ni Write
            allowed_tools=OUTILS_FICHIERS + noms_maison,  # autorisés sans confirmation
            permission_mode="dontAsk",                    # tout le reste est refusé
            mcp_servers={SERVEUR: create_sdk_mcp_server(SERVEUR, tools=outils_maison)} if outils_maison else {},
            strict_mcp_config=True,                       # ignore les serveurs MCP configurés sur le poste
            setting_sources=[],                           # ignore les réglages et CLAUDE.md du poste
            cwd=str(racines[0]),
            add_dirs=[str(r) for r in racines[1:]] + [str(sortie)],
            hooks={"PreToolUse": [HookMatcher(matcher="Read|Grep|Glob",
                                              hooks=[creer_garde(racines, profil.fichiers_interdits, sortie)])]},
            max_turns=reglages.max_tours or None,
            max_budget_usd=reglages.budget_usd or None,
            model=reglages.modele or None,
            effort=reglages.effort or None,
            env=config.env_claude(),                      # compte Claude choisi (abonnement ou clé API)
            resume=reprise or None,
        )
        self.client = ClaudeSDKClient(self.options)

    # -- Outils maison ------------------------------------------------------------------------------

    def plafond_base(self) -> int:
        return self.reglages.requetes_base_generation if self.generation else self.reglages.requetes_base

    def _outils_maison(self) -> list:
        profil, liste = self.profil, []

        async def executer(fonction, *args, base=False) -> dict:
            if base:
                if self.requetes_base >= self.plafond_base():
                    return _texte(f"Limite de {self.plafond_base()} accès base atteinte pour cette "
                                  f"question : réponds avec ce que tu as.", True)
                self.requetes_base += 1
            try:
                return _texte(fonction(profil, *args))
            except outils.OutilErreur as e:
                return _texte(f"ERREUR : {e}", True)

        if profil.base:
            @tool("base_tables", "Liste les tables et vues du schéma Odatio (lecture seule). "
                  "Filtre facultatif au format LIKE, ex. « ORD% ».",
                  {"type": "object", "properties": {"motif": {"type": "string", "description": "motif LIKE"}}})
            async def base_tables(args):
                return await executer(outils.base_tables, args.get("motif", ""), base=True)

            @tool("base_decrire", "Décrit une table : colonnes, types, clé primaire, clés étrangères, index.",
                  {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"]})
            async def base_decrire(args):
                return await executer(outils.base_decrire, args["table"], base=True)

            @tool("base_requete", "Exécute UNE requête SELECT ou WITH en lecture seule (colonnes nommées, clause "
                  "WHERE). Résultat plafonné à 'limite' lignes (50 par défaut, 200 au plus).",
                  {"type": "object", "properties": {"sql": {"type": "string"},
                                                    "limite": {"type": "integer", "minimum": 1, "maximum": 200}},
                   "required": ["sql"]})
            async def base_requete(args):
                return await executer(outils.base_requete, args["sql"], int(args.get("limite") or 50), base=True)

            liste += [base_tables, base_decrire, base_requete]

        if profil.groundcover_filtre:
            @tool("logs_rechercher", "Cherche dans les logs applicatifs groundcover de cet environnement. Heures "
                  "ISO 8601 en UTC (ex. 2026-10-28T17:28:00Z), fenêtre de 30 minutes au plus. 'recherche' : "
                  "texte libre (code, référence, exception) ou filtres gcQL (level:error, workload:x), sans pipe.",
                  {"type": "object", "properties": {"debut": {"type": "string"}, "fin": {"type": "string"},
                                                    "recherche": {"type": "string"},
                                                    "limite": {"type": "integer", "minimum": 1, "maximum": 200}},
                   "required": ["debut", "fin"]})
            async def logs_rechercher(args):
                return await executer(outils.logs_rechercher, args["debut"], args["fin"],
                                      args.get("recherche", ""), int(args.get("limite") or 50))

            liste.append(logs_rechercher)

        if config.jira_configure():
            @tool("jira_ticket", "Lit un ticket Jira (titre, description, épopée parente, fiches liées), en lecture "
                  "seule. Utile pour un ticket cité dans la question ou lié à la tâche étudiée.",
                  {"type": "object", "properties": {"cle": {"type": "string", "description": "ex. ODA-1234"}},
                   "required": ["cle"]})
            async def jira_ticket(args):
                try:
                    return _texte(outils.jira_ticket(args["cle"], profil)["texte"])
                except outils.OutilErreur as e:
                    return _texte(f"ERREUR : {e}", True)

            liste.append(jira_ticket)

        if bitbucket.configure(profil):
            liste += self._outils_bitbucket(executer)
        if self.dossier_documentation:
            liste.append(self._outil_documentation())
        liste += self._outils_fichiers()
        return liste

    def _outils_fichiers(self) -> list:
        profil = self.profil

        @tool("generation_demarrer", "À appeler UNE fois, avant toute recherche, quand l'utilisateur demande "
              "explicitement de créer un fichier d'interface (commande, article…) à intégrer. Porte le plafond "
              "d'accès base de la question au niveau d'une génération. Jamais pour une simple question.",
              {"type": "object", "properties": {"objet": {"type": "string",
                                                          "description": "ex. commande 0082380800"}},
               "required": ["objet"]})
        async def generation_demarrer(args):
            self.generation = True
            outils.journal(profil, "generation_demarrer", {"objet": str(args.get("objet", ""))[:200]})
            return _texte(f"Génération ouverte : jusqu'à {self.plafond_base()} accès base pour cette question "
                          f"({self.requetes_base} déjà utilisé(s)).")

        @tool("fichier_ecrire", "Écrit un fichier d'interface demandé par l'utilisateur dans le dossier des "
              "fichiers générés (seule écriture possible ; jamais d'écrasement : un suffixe horodaté est ajouté "
              "si le nom existe). Nom simple sans chemin, extension "
              + ", ".join(fichiers.EXTENSIONS) + ", 5 Mo au plus. Appelle d'abord generation_demarrer.",
              {"type": "object", "properties": {
                  "nom": {"type": "string", "description": "ex. CDE_0082380800_T1.xml"},
                  "contenu": {"type": "string"},
                  "encodage": {"type": "string", "enum": list(fichiers.ENCODAGES), "default": "utf-8"},
                  "fins_ligne": {"type": "string", "enum": list(fichiers.FINS_LIGNE), "default": "lf"}},
               "required": ["nom", "contenu"]})
        async def fichier_ecrire(args):
            if not self.generation:
                return _texte("Appelle d'abord generation_demarrer : l'écriture n'est permise que pour un "
                              "fichier d'interface demandé par l'utilisateur.", True)
            try:
                f = fichiers.ecrire(profil, args["nom"], args["contenu"], args.get("encodage") or "utf-8",
                                    args.get("fins_ligne") or "lf")
            except outils.OutilErreur as e:
                return _texte(f"ERREUR : {e}", True)
            self.fichiers.append(f)
            notes = [f"Fichier écrit : {f['chemin']} ({f['taille']} octets, {f['encodage']}, fins de ligne "
                     f"{f['fins_ligne'].upper()})."]
            if f["renomme"]:
                notes.append(f"Le nom demandé existait déjà : le fichier s'appelle {f['nom']}.")
            if f["secrets_masques"]:
                notes.append(f"{f['secrets_masques']} valeur(s) secrète(s) remplacée(s) par **** : signale-le.")
            notes.append("Relis-le avec Read pour le vérifier.")
            return _texte(" ".join(notes))

        return [generation_demarrer, fichier_ecrire]

    def _outil_documentation(self):
        @tool("documentation_enregistrer", "Enregistre la documentation fonctionnelle (document + scénario de la "
              "vidéo) destinée au consultant. À appeler une fois l'enquête terminée ; si le document est refusé, "
              "corrige les points signalés et renvoie-le en entier.", documentation.SCHEMA)
        async def documentation_enregistrer(args):
            try:
                self.document = documentation.enregistrer(self.dossier_documentation, args)
            except documentation.DocumentationErreur as e:
                outils.journal(self.profil, "documentation_enregistrer", {}, str(e)[:300])
                return _texte(str(e), True)
            outils.journal(self.profil, "documentation_enregistrer", {"scenes": len(self.document["scenes"])})
            return _texte(f"Document enregistré : {len(self.document['scenes'])} scènes, "
                          f"{len(self.document['cas_de_test'])} cas de test, vidéo d'environ "
                          f"{documentation.duree_estimee(self.document)} min. Termine par une phrase de synthèse, "
                          f"sans recopier le document.")

        return documentation_enregistrer

    def _outils_bitbucket(self, executer) -> list:
        depots_profil = ", ".join(s.nom for s in self.profil.sources if s.depot) or "aucun"
        depot_desc = (f"dépôt : nom d'une source du profil ({depots_profil}), « PROJET/depot » ou URL Bitbucket")

        def schema(proprietes: dict, requis: list) -> dict:
            return {"type": "object", "properties": proprietes, "required": requis}

        texte = {"type": "string"}
        depot = {"type": "string", "description": depot_desc}
        branche = {"type": "string", "description": "branche (défaut : celle du profil, sinon celle du dépôt)"}

        @tool("bitbucket_depots", "Bitbucket : liste les dépôts lisibles (filtre par nom et/ou clé de projet). "
              "Pour trouver une bibliothèque ou le dépôt d'un autre client absent des sources.",
              schema({"recherche": texte, "projet": {"type": "string", "description": "clé de projet, ex. STD"}}, []))
        async def bitbucket_depots(args):
            return await executer(outils.bitbucket_depots, args.get("recherche", ""), args.get("projet", ""))

        @tool("bitbucket_branches", "Bitbucket : cherche les branches dont le nom contient 'filtre' (ex. la clé du "
              "ticket ODA-1234), les plus récentes d'abord. Sans 'depot' ni 'projet' : dans les dépôts du profil.",
              schema({"filtre": texte, "depot": depot,
                      "projet": {"type": "string", "description": "tous les dépôts d'un projet (60 au plus)"}}, []))
        async def bitbucket_branches(args):
            return await executer(outils.bitbucket_branches, args.get("filtre", ""), args.get("depot", ""),
                                  args.get("projet", ""))

        @tool("bitbucket_fichiers", "Bitbucket : liste les fichiers d'un dépôt sur une branche (récursif depuis "
              "'dossier'), filtrés par 'motif' (*.java, *Vague*, texte contenu dans le chemin).",
              schema({"depot": depot, "branche": branche, "dossier": texte, "motif": texte}, ["depot"]))
        async def bitbucket_fichiers(args):
            return await executer(outils.bitbucket_fichiers, args["depot"], args.get("branche", ""),
                                  args.get("dossier", ""), args.get("motif", ""))

        @tool("bitbucket_lire", "Bitbucket : lit un fichier (lignes numérotées, 400 au plus à partir de 'debut') ou "
              "liste un dossier, sur une branche donnée. Pour du code absent des sources clonées.",
              schema({"depot": depot, "chemin": texte, "branche": branche,
                      "debut": {"type": "integer", "minimum": 1},
                      "nombre": {"type": "integer", "minimum": 1, "maximum": bitbucket.LIGNES_MAX}},
                     ["depot", "chemin"]))
        async def bitbucket_lire(args):
            return await executer(outils.bitbucket_lire, args["depot"], args["chemin"], args.get("branche", ""),
                                   int(args.get("debut") or 1), int(args.get("nombre") or bitbucket.LIGNES_MAX))

        @tool("bitbucket_comparer", "Bitbucket : ce qu'une branche apporte par rapport à sa cible ('vers' ; par "
              "défaut la cible de sa pull request, sinon la branche du profil) : pull request, commits et fichiers "
              "modifiés ; avec 'fichier', le diff de ce fichier.",
              schema({"depot": depot, "branche": texte, "vers": texte, "fichier": texte}, ["depot", "branche"]))
        async def bitbucket_comparer(args):
            return await executer(outils.bitbucket_comparer, args["depot"], args["branche"], args.get("vers", ""),
                                  args.get("fichier", ""))

        @tool("bitbucket_chercher", "Bitbucket : recherche de code dans tous les dépôts lisibles (ou un dépôt, un "
              "projet). N'indexe que la branche par défaut de chaque dépôt ; peut être indisponible sur le serveur.",
              schema({"texte": texte, "depot": depot, "projet": texte}, ["texte"]))
        async def bitbucket_chercher(args):
            return await executer(outils.bitbucket_chercher, args["texte"], args.get("depot", ""),
                                  args.get("projet", ""))

        return [bitbucket_depots, bitbucket_branches, bitbucket_fichiers, bitbucket_lire, bitbucket_comparer,
                bitbucket_chercher]

    # -- Conversation -------------------------------------------------------------------------------

    async def ouvrir(self) -> None:
        await self.client.connect()

    async def fermer(self) -> None:
        await self.client.disconnect()

    async def demander(self, question: str, sur_texte, sur_outil) -> Bilan:
        """Pose une question (le contexte des questions précédentes est conservé) ; diffuse le texte de la
        réponse et les outils utilisés au fil de l'eau."""
        self.requetes_base, self.generation, self.fichiers = 0, False, []
        await self.client.query(question)
        bilan = Bilan(cout_usd=None, tours=0, erreur=False)
        async for message in self.client.receive_response():
            if isinstance(message, AssistantMessage):
                for bloc in message.content:
                    if isinstance(bloc, TextBlock):
                        sur_texte(bloc.text)
                    elif isinstance(bloc, ToolUseBlock):
                        sur_outil(bloc.name, bloc.input)
            elif isinstance(message, ResultMessage):
                bilan = Bilan(cout_usd=message.total_cost_usd, tours=message.num_turns, erreur=message.is_error,
                              message="; ".join(message.errors or []) or (message.terminal_reason or ""),
                              session_id=message.session_id or "", reponse=message.result or "")
        bilan.fichiers = list(self.fichiers)
        return bilan
