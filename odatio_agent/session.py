"""
Session de conversation avec l'agent (Claude Agent SDK) : outils de fichiers limités à Read, Grep et Glob,
outils maison base, logs et fichiers d'interface exposés en serveur MCP dans le processus
(create_sdk_mcp_server), garde-fou sur les chemins, aucun réglage utilisateur de Claude Code chargé.

Écritures possibles : le document de documentation (documentation_demarrer puis documentation_enregistrer,
dans .cache/documentation) et fichier_ecrire, dans le dossier des fichiers générés du profil, après
generation_demarrer : cet appel annonce une génération demandée par l'utilisateur et porte le plafond d'accès
base de la question à reglages.requetes_base_generation.

Session de synthèse client (synthese={"mode": "lot" | "finale", "numero", "dossier"}) : outils de lecture et
synthese_demarrer / synthese_enregistrer seulement (rien d'autre n'écrit), plafonds propres à la synthèse
(reglages.requetes_base_synthese, reglages.synthese_max_tours), sans relever ceux des questions.
"""
from dataclasses import dataclass, field

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, ResultMessage,
                              TextBlock, ToolUseBlock, create_sdk_mcp_server, tool)

from pathlib import Path

from . import bitbucket, config, consignes, doc_odatio, documentation, ecran, fichiers, logs_ssh, outils, synthese as synth
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
    documentation: dict | None = None  # {"dossier", "sujet"} si un document a été enregistré pendant la question
    synthese: bool = False   # une partie de synthèse client a été enregistrée pendant la question


def _texte(texte: str, erreur: bool = False) -> dict:
    return {"content": [{"type": "text", "text": texte}], "is_error": erreur}


async def _vers_texte(coroutine) -> dict:
    return _texte(await coroutine)


class Session:
    def __init__(self, profil: config.Profil, sources: list, logs: list, reglages: config.Reglages,
                 utilisateur: str = "", reprise: str = "", ticket: dict | None = None,
                 dossier_documentation: Path | None = None, lecture_seule: bool = False,
                 synthese: dict | None = None):
        """reprise : identifiant d'une conversation précédente à poursuivre (Bilan.session_id), utilisé par
        l'IHM qui ouvre une session par question. ticket : tâche Jira à tester (outils.jira_ticket), donnée
        à l'agent comme contexte. dossier_documentation : dossier déjà préparé (captures d'écran déposées), ou
        documentation déjà enregistrée à poursuivre (documentation_demarrer renvoie alors le document existant) ;
        sinon documentation_demarrer en crée un quand l'utilisateur en demande une. lecture_seule : question
        transmise par le manager (mcp_server.py --manager) : ni fichier d'interface, ni documentation, ni
        navigation dans l'application, seulement lire et répondre. synthese : étape d'une synthèse client
        ({"mode": "lot" | "finale", "numero", "dossier"}, synthese.py) : lecture et outils de synthèse seulement."""
        self.profil, self.reglages, self.ticket = profil, reglages, ticket
        self.lecture_seule = lecture_seule
        self.synthese = synthese
        self.synthese_enregistree = False
        self.requetes_base, self.generation, self.fichiers = 0, False, []
        self.dossier_fourni = Path(dossier_documentation) if dossier_documentation else None
        self.dossier_documentation = self.dossier_fourni
        self.documentation_ouverte = False
        self.document = None   # document accepté par documentation_enregistrer
        self.navigateur = None  # ecran.Navigateur, démarré à la première capture
        racines = [chemin for _, chemin in sources] + list(logs)
        sortie = config.dossier_fichiers(profil)
        sortie.mkdir(parents=True, exist_ok=True)
        outils_maison = self._outils_maison()
        noms_maison = [f"mcp__{SERVEUR}__{t.name}" for t in outils_maison]

        self.options = ClaudeAgentOptions(
            system_prompt=consignes.construire(profil, sources, logs, utilisateur, reglages, ticket, lecture_seule,
                                               synthese=bool(synthese)),
            tools=OUTILS_FICHIERS,                       # aucun autre outil intégré : ni Bash, ni Edit, ni Write
            allowed_tools=OUTILS_FICHIERS + noms_maison,  # autorisés sans confirmation
            permission_mode="dontAsk",                    # tout le reste est refusé
            mcp_servers={SERVEUR: create_sdk_mcp_server(SERVEUR, tools=outils_maison)} if outils_maison else {},
            strict_mcp_config=True,                       # ignore les serveurs MCP configurés sur le poste
            setting_sources=[],                           # ignore les réglages et CLAUDE.md du poste
            cwd=str(racines[0] if racines else sortie),   # aucune source locale : synthèse lue sur Bitbucket
            add_dirs=[str(r) for r in racines[1:]] + ([str(sortie)] if racines else []),
            hooks={"PreToolUse": [HookMatcher(matcher="Read|Grep|Glob",
                                              hooks=[creer_garde(racines, profil.fichiers_interdits, sortie)])]},
            max_turns=(reglages.synthese_max_tours if synthese else reglages.max_tours) or None,
            max_budget_usd=reglages.budget_usd or None,
            model=reglages.modele or None,
            effort=reglages.effort or None,
            env=config.env_claude(),                      # compte Claude choisi (abonnement ou clé API)
            resume=reprise or None,
        )
        self.client = ClaudeSDKClient(self.options)

    # -- Outils maison ------------------------------------------------------------------------------

    def plafond_base(self) -> int:
        if self.synthese:
            return self.reglages.requetes_base_synthese
        large = self.generation or self.documentation_ouverte
        return self.reglages.requetes_base_generation if large else self.reglages.requetes_base

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

        if profil.logs_ssh:
            liste += self._outils_logs_ssh(executer)

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
        if doc_odatio.configuree(profil) and not self.synthese:
            liste += self._outils_doc(executer)
        if self.synthese:               # étape de synthèse client : lecture + outils de synthèse, rien d'autre
            return liste + self._outils_synthese()
        if self.lecture_seule:          # question du manager : aucun outil qui écrit ou agit
            return liste
        liste += self._outils_documentation()
        if ecran.disponible():
            liste += self._outils_ecran()
        liste += self._outils_fichiers()
        return liste

    def _outils_doc(self, executer) -> list:
        """Documentation Odatio (produit Standard), lue dans ses sources sur Bitbucket : lecture seule."""
        @tool("doc_sommaire", "Documentation Odatio (Standard) : fichiers d'un dossier de la doc ('' = racine), "
              "filtrés par un motif facultatif (*.rst, *vague*…).",
              {"type": "object", "properties": {"dossier": {"type": "string"}, "motif": {"type": "string"}}})
        async def doc_sommaire(args):
            return await executer(outils.doc_sommaire, args.get("dossier", ""), args.get("motif", ""))

        @tool("doc_lire", "Documentation Odatio : lit un fichier (lignes numérotées, 400 au plus à partir de "
              "'debut'). 'chemin' : donné par doc_sommaire ou doc_chercher, ou adresse d'une page du site publié "
              "(…/development/index.html).",
              {"type": "object", "properties": {
                  "chemin": {"type": "string"}, "debut": {"type": "integer", "minimum": 1},
                  "nombre": {"type": "integer", "minimum": 1, "maximum": bitbucket.LIGNES_MAX}},
               "required": ["chemin"]})
        async def doc_lire(args):
            return await executer(outils.doc_lire, args["chemin"], int(args.get("debut") or 1),
                                  int(args.get("nombre") or bitbucket.LIGNES_MAX))

        @tool("doc_chercher", "Documentation Odatio : recherche plein texte (branche par défaut du dépôt de la "
              "doc, si la recherche de Bitbucket est activée).",
              {"type": "object", "properties": {"texte": {"type": "string"}}, "required": ["texte"]})
        async def doc_chercher(args):
            return await executer(outils.doc_chercher, args["texte"])

        return [doc_sommaire, doc_lire, doc_chercher]

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

    def _outils_logs_ssh(self, executer) -> list:
        """Logs d'un serveur (ex. la production) lus par SSH : commandes fixes en lecture seule (logs_ssh.py)."""
        heures = ("Heures ISO 8601 à l'heure des logs (heure locale, ex. 2026-11-18T09:41:00) ; une heure en UTC "
                  "(suffixe Z) est convertie.")

        @tool("logs_ssh_fichiers", "Liste les fichiers de logs du serveur (lecture seule par SSH) : dernière "
              "modification, taille, chemin complet. Utile pour choisir le fichier d'une période.",
              {"type": "object", "properties": {}})
        async def logs_ssh_fichiers(args):
            return await executer(outils.logs_ssh_fichiers)

        @tool("logs_ssh_chercher", "Cherche un texte (code, référence, nom d'exception ; sans tenir compte de la "
              "casse) dans les logs du serveur, entre deux heures (24 heures au plus). " + heures + " 'contexte' : "
              "lignes gardées après chaque ligne trouvée (pile d'exception), 30 au plus. 'fichier' : chemin complet "
              "donné par logs_ssh_fichiers (vide = les fichiers modifiés depuis le début de la fenêtre).",
              {"type": "object", "properties": {
                  "texte": {"type": "string"}, "debut": {"type": "string"}, "fin": {"type": "string"},
                  "fichier": {"type": "string"},
                  "contexte": {"type": "integer", "minimum": 0, "maximum": 30},
                  "limite": {"type": "integer", "minimum": 1, "maximum": logs_ssh.LIGNES_MAX}},
               "required": ["texte", "debut", "fin"]})
        async def logs_ssh_chercher(args):
            return await executer(outils.logs_ssh_chercher, args["texte"], args["debut"], args["fin"],
                                  args.get("fichier", ""), int(args.get("contexte") or 0),
                                  int(args.get("limite") or 50))

        @tool("logs_ssh_lire", "Lit les logs du serveur à partir d'une heure, sans filtre, sur quelques minutes "
              "(60 au plus), pour voir ce qui s'est passé autour d'un événement. " + heures + " 'fichier' : chemin "
              "complet donné par logs_ssh_fichiers (vide = le fichier qui couvre l'heure demandée).",
              {"type": "object", "properties": {
                  "debut": {"type": "string"}, "minutes": {"type": "integer", "minimum": 1,
                                                            "maximum": logs_ssh.MINUTES_LECTURE_MAX},
                  "fichier": {"type": "string"},
                  "limite": {"type": "integer", "minimum": 1, "maximum": logs_ssh.LIGNES_MAX}},
               "required": ["debut"]})
        async def logs_ssh_lire(args):
            return await executer(outils.logs_ssh_lire, args["debut"], int(args.get("minutes") or 10),
                                  args.get("fichier", ""), int(args.get("limite") or logs_ssh.LIGNES_MAX))

        return [logs_ssh_fichiers, logs_ssh_chercher, logs_ssh_lire]

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

    def _outils_documentation(self) -> list:
        @tool("documentation_demarrer", "À appeler UNE fois, avant toute recherche, quand l'utilisateur demande une "
              "documentation, une vidéo, une démo, un tutoriel ou un support de présentation d'une fonctionnalité "
              "ou d'une tâche. Renvoie la marche à suivre et le format du document. Jamais pour une simple question.",
              {"type": "object", "properties": {
                  "sujet": {"type": "string", "description": "la fonctionnalité, en quelques mots"},
                  "nouvelle": {"type": "boolean", "description": "true pour une documentation sur un AUTRE sujet "
                               "que celle déjà en cours (sinon la documentation en cours est poursuivie)"}},
               "required": ["sujet"]})
        async def documentation_demarrer(args):
            sujet = str(args.get("sujet", "")).strip() or "documentation"
            titre = f"{self.ticket['cle']} — {self.ticket['titre']}" if self.ticket else sujet
            if args.get("nouvelle") or self.dossier_documentation is None:
                self.dossier_documentation = documentation.nouveau_dossier(self.ticket["cle"] if self.ticket else sujet)
            dossier = self.dossier_documentation
            if not (dossier / "sujet.txt").exists():   # documentation poursuivie : son sujet est gardé
                (dossier / "sujet.txt").write_text(titre, encoding="utf-8")
            self.documentation_ouverte = True
            outils.journal(self.profil, "documentation_demarrer", {"sujet": sujet[:200]})
            return _texte(consignes.documentation(documentation.captures(dossier), self.plafond_base(),
                                                  ecran.decrire(self.profil), self.profil.donnees_reelles,
                                                  documentation.existant(dossier)))

        @tool("documentation_enregistrer", "Enregistre la documentation fonctionnelle (document + scénario de la "
              "vidéo) destinée au consultant, après documentation_demarrer et une fois l'enquête terminée. La "
              "vidéo et le document Word sont montés automatiquement ensuite. Si le document est refusé, corrige "
              "les points signalés et renvoie-le en entier.", documentation.SCHEMA)
        async def documentation_enregistrer(args):
            if not self.documentation_ouverte or self.dossier_documentation is None:
                return _texte("Appelle d'abord documentation_demarrer, puis mène l'enquête.", True)
            try:
                self.document = documentation.enregistrer(self.dossier_documentation, args)
            except documentation.DocumentationErreur as e:
                outils.journal(self.profil, "documentation_enregistrer", {}, str(e)[:300])
                return _texte(str(e), True)
            outils.journal(self.profil, "documentation_enregistrer", {"scenes": len(self.document["scenes"])})
            return _texte(f"Document enregistré : {len(self.document['scenes'])} scènes, "
                          f"{len(self.document['cas_de_test'])} cas de test, vidéo d'environ "
                          f"{documentation.duree_estimee(self.document)} min, montée automatiquement après ta "
                          f"réponse. Termine par 3 à 5 lignes : ce que couvre la documentation, ce qui reste déduit "
                          f"ou non vérifié. Ne recopie pas le document.")

        return [documentation_demarrer, documentation_enregistrer]

    def _outils_synthese(self) -> list:
        """Étape d'une synthèse client (synthese.py) : la marche à suivre et les données du lot, puis
        l'enregistrement validé de la partie (seule écriture, dans le dossier de la synthèse)."""
        mode, dossier, numero = self.synthese["mode"], Path(self.synthese["dossier"]), int(self.synthese.get("numero")
                                                                                         or 0)

        @tool("synthese_demarrer", "Synthèse client : renvoie la marche à suivre et les données de cette étape (lot "
              "d'éléments à expliquer, ou partie finale). À appeler UNE fois, avant toute recherche.",
              {"type": "object", "properties": {}})
        async def synthese_demarrer(args):
            try:
                donnees = synth.donnees_lot(dossier, numero) if mode == "lot" else synth.donnees_finale(dossier)
            except (synth.SyntheseErreur, StopIteration, KeyError) as e:
                return _texte(f"ERREUR : synthèse illisible ({e}).", True)
            outils.journal(self.profil, "synthese_demarrer", {"mode": mode, "lot": numero})
            return _texte(consignes.synthese(mode, donnees, self.plafond_base()))

        @tool("synthese_enregistrer", "Synthèse client : enregistre la réponse complète de cette étape, contrôlée "
              "(source et statut sur chaque ligne, aucun secret, e-mail ni téléphone). Si elle est refusée, corrige "
              "les points signalés et renvoie-la en entier.",
              synth.SCHEMA_LOT if mode == "lot" else synth.SCHEMA_FINALE)
        async def synthese_enregistrer(args):
            try:
                if mode == "lot":
                    partie = synth.enregistrer_lot(dossier, numero, args)
                    resume = (f"{len(partie['elements'])} élément(s) expliqué(s), {len(partie['non_expliques'])} non "
                              f"expliqué(s)")
                else:
                    partie = synth.enregistrer_finale(dossier, args)
                    resume = (f"{len(partie['contexte'])} ligne(s) de contexte, {len(partie['configuration'])} de "
                              f"configuration, {len(partie['audit'])} constat(s)")
            except synth.SyntheseErreur as e:
                outils.journal(self.profil, "synthese_enregistrer", {"mode": mode, "lot": numero}, str(e)[:300])
                return _texte(str(e), True)
            self.synthese_enregistree = True
            outils.journal(self.profil, "synthese_enregistrer", {"mode": mode, "lot": numero})
            return _texte(f"Enregistré : {resume}. Termine par une ligne, sans recopier la synthèse.")

        return [synthese_demarrer, synthese_enregistrer]

    def _outils_ecran(self) -> list:
        """Navigation dans l'application pour les captures d'écran d'une documentation (ecran.py)."""

        async def jouer(outil: str, details: dict, action) -> dict:
            if not self.documentation_ouverte or self.dossier_documentation is None:
                return _texte("La navigation dans l'application sert aux captures d'une documentation : appelle "
                              "d'abord documentation_demarrer.", True)
            if self.navigateur is None:
                self.navigateur = ecran.Navigateur(self.profil)
            try:
                resultat = await action(self.navigateur)
            except ecran.EcranErreur as e:
                outils.journal(self.profil, outil, details, str(e)[:300])
                return _texte(f"ERREUR : {e}", True)
            except Exception as e:  # incident du navigateur
                message = f"{type(e).__name__} : {str(e).splitlines()[0][:200] if str(e) else ''}"
                outils.journal(self.profil, outil, details, message)
                return _texte(f"ERREUR : {message}", True)
            outils.journal(self.profil, outil, details)
            return resultat

        def schema(proprietes: dict, requis: list) -> dict:
            return {"type": "object", "properties": proprietes, "required": requis}

        texte = {"type": "string"}

        @tool("ecran_ouvrir", "Application : ouvre une page de l'application du projet (adresse complète ou chemin "
              "relatif ; vide = page d'accueil) et renvoie ce qui s'affiche. La connexion est faite par l'outil.",
              schema({"adresse": texte}, []))
        async def ecran_ouvrir(args):
            adresse = str(args.get("adresse", ""))
            return await jouer("ecran_ouvrir", {"adresse": adresse[:200]},
                               lambda n: _vers_texte(n.ouvrir(adresse)))

        @tool("ecran_observer", "Application : relit l'écran affiché (titre, adresse, éléments avec leur rôle et "
              "leur libellé exact).", schema({}, []))
        async def ecran_observer(args):
            return await jouer("ecran_observer", {}, lambda n: _vers_texte(n.observer()))

        @tool("ecran_cliquer", "Application : clique sur un menu, un onglet, un lien, un bouton ou une ligne, "
              "désigné par son libellé exact, et renvoie le nouvel écran. En mode navigation, les libellés qui "
              "valident, enregistrent, suppriment ou lancent un traitement sont refusés.",
              schema({"libelle": texte}, ["libelle"]))
        async def ecran_cliquer(args):
            return await jouer("ecran_cliquer", {"libelle": str(args["libelle"])[:100]},
                               lambda n: _vers_texte(n.cliquer(args["libelle"])))

        @tool("ecran_saisir", "Application : remplit un champ (filtre, recherche) désigné par son libellé ; "
              "'entree' appuie sur Entrée pour lancer la recherche. Jamais de mot de passe.",
              schema({"champ": texte, "valeur": texte, "entree": {"type": "boolean"}}, ["champ", "valeur"]))
        async def ecran_saisir(args):
            return await jouer("ecran_saisir", {"champ": str(args["champ"])[:100]},
                               lambda n: _vers_texte(n.saisir(args["champ"], args["valeur"],
                                                              bool(args.get("entree")))))

        @tool("ecran_capturer", "Application : capture l'écran affiché pour la documentation et renvoie le nom du "
              "fichier (à mettre dans le champ capture de la scène) avec un aperçu. 'surligner' entoure en rouge "
              "l'élément dont on parle (libellé exact) ; 'pleine_page' capture toute la hauteur de la page.",
              schema({"nom": {"type": "string", "description": "nom parlant, ex. suivi-vagues-bouton-liberer"},
                      "surligner": texte, "pleine_page": {"type": "boolean"}}, ["nom"]))
        async def ecran_capturer(args):
            async def capturer(n):
                nom, apercu = await n.capturer(self.dossier_documentation, args["nom"],
                                               str(args.get("surligner") or ""), bool(args.get("pleine_page")))
                return {"content": [{"type": "text", "text": f"Capture enregistrée : {nom}. Mets ce nom dans le "
                                                             f"champ capture de la scène qu'elle illustre."},
                                    {"type": "image", "data": apercu, "mimeType": "image/jpeg"}],
                        "is_error": False}
            return await jouer("ecran_capturer", {"capture": str(args["nom"])[:100]}, capturer)

        return [ecran_ouvrir, ecran_observer, ecran_cliquer, ecran_saisir, ecran_capturer]

    # -- Conversation -------------------------------------------------------------------------------

    async def ouvrir(self) -> None:
        await self.client.connect()

    async def fermer(self) -> None:
        if self.navigateur is not None:
            await self.navigateur.fermer()
            self.navigateur = None
        await self.client.disconnect()

    async def demander(self, question: str, sur_texte, sur_outil) -> Bilan:
        """Pose une question (le contexte des questions précédentes est conservé) ; diffuse le texte de la
        réponse et les outils utilisés au fil de l'eau."""
        self.requetes_base, self.generation, self.fichiers = 0, False, []
        self.document, self.documentation_ouverte = None, False
        self.synthese_enregistree = False
        self.dossier_documentation = self.dossier_fourni   # documentation en cours, ou nouveau dossier
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
        bilan.synthese = self.synthese_enregistree
        if self.document and self.dossier_documentation:
            bilan.documentation = {"dossier": str(self.dossier_documentation),
                                   "sujet": documentation.sujet(self.dossier_documentation)}
        return bilan
