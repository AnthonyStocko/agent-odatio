"""
Session de conversation avec l'agent (Claude Agent SDK) : outils de fichiers limités à Read, Grep et Glob,
outils maison base et logs exposés en serveur MCP dans le processus (create_sdk_mcp_server), garde-fou sur
les chemins, aucun réglage utilisateur de Claude Code chargé.
"""
from dataclasses import dataclass

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, ResultMessage,
                              TextBlock, ToolUseBlock, create_sdk_mcp_server, tool)

from . import config, consignes, outils
from .garde import creer_garde

SERVEUR = "odatio"
OUTILS_FICHIERS = ["Read", "Grep", "Glob"]


@dataclass
class Bilan:
    cout_usd: float | None
    tours: int
    erreur: bool
    message: str = ""


def _texte(texte: str, erreur: bool = False) -> dict:
    return {"content": [{"type": "text", "text": texte}], "is_error": erreur}


class Session:
    def __init__(self, profil: config.Profil, sources: list, logs: list, reglages: config.Reglages,
                 utilisateur: str = ""):
        self.profil, self.reglages = profil, reglages
        self.requetes_base = 0
        racines = [chemin for _, chemin in sources] + list(logs)
        outils_maison = self._outils_maison()
        noms_maison = [f"mcp__{SERVEUR}__{t.name}" for t in outils_maison]

        cle_api = config.secret("ANTHROPIC_API_KEY")
        self.options = ClaudeAgentOptions(
            system_prompt=consignes.construire(profil, sources, logs, utilisateur, reglages),
            tools=OUTILS_FICHIERS,                       # aucun autre outil intégré : ni Bash, ni Edit, ni Write
            allowed_tools=OUTILS_FICHIERS + noms_maison,  # autorisés sans confirmation
            permission_mode="dontAsk",                    # tout le reste est refusé
            mcp_servers={SERVEUR: create_sdk_mcp_server(SERVEUR, tools=outils_maison)} if outils_maison else {},
            strict_mcp_config=True,                       # ignore les serveurs MCP configurés sur le poste
            setting_sources=[],                           # ignore les réglages et CLAUDE.md du poste
            cwd=str(racines[0]),
            add_dirs=[str(r) for r in racines[1:]],
            hooks={"PreToolUse": [HookMatcher(matcher="Read|Grep|Glob",
                                              hooks=[creer_garde(racines, profil.fichiers_interdits)])]},
            max_turns=reglages.max_tours or None,
            max_budget_usd=reglages.budget_usd or None,
            model=reglages.modele or None,
            effort=reglages.effort or None,
            env={"ANTHROPIC_API_KEY": cle_api} if cle_api else {},
        )
        self.client = ClaudeSDKClient(self.options)

    # -- Outils maison ------------------------------------------------------------------------------

    def _outils_maison(self) -> list:
        profil, liste = self.profil, []

        async def executer(fonction, *args, base=False) -> dict:
            if base:
                if self.requetes_base >= self.reglages.requetes_base:
                    return _texte(f"Limite de {self.reglages.requetes_base} accès base atteinte pour cette "
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
        return liste

    # -- Conversation -------------------------------------------------------------------------------

    async def ouvrir(self) -> None:
        await self.client.connect()

    async def fermer(self) -> None:
        await self.client.disconnect()

    async def demander(self, question: str, sur_texte, sur_outil) -> Bilan:
        """Pose une question (le contexte des questions précédentes est conservé) ; diffuse le texte de la
        réponse et les outils utilisés au fil de l'eau."""
        self.requetes_base = 0
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
                              message="; ".join(message.errors or []) or (message.terminal_reason or ""))
        return bilan
