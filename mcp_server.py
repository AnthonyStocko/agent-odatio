"""
Serveur MCP (stdio) : les mêmes outils base, logs et Bitbucket, utilisables depuis Claude Code, Claude Desktop ou tout
client MCP, pour un profil donné. Facultatif : l'agent (agent.py) n'en a pas besoin.

  pip install "mcp>=2,<3"
  claude mcp add odatio-recette -- C:\\odatio-agent\\venv\\Scripts\\python.exe C:\\odatio-agent\\mcp_server.py --profil recette-x

Les sources ne sont pas exposées ici : dans Claude Code, lance simplement `claude` dans le dossier des sources.
"""
import argparse
import sys

from mcp.server.mcpserver import MCPServer

from odatio_agent import bitbucket, config, outils


def creer_serveur(profil: config.Profil) -> MCPServer:
    serveur = MCPServer(f"odatio-{profil.nom}",
                        instructions=f"Accès en lecture seule à l'environnement Odatio « {profil.nom} ». "
                                     f"Ne recopie aucune donnée nominative.")

    if profil.base:
        @serveur.tool(description="Liste les tables et vues du schéma Odatio (motif LIKE facultatif).")
        def base_tables(motif: str = "") -> str:
            return outils.base_tables(profil, motif)

        @serveur.tool(description="Décrit une table : colonnes, types, clés, index.")
        def base_decrire(table: str) -> str:
            return outils.base_decrire(profil, table)

        @serveur.tool(description="Exécute UNE requête SELECT ou WITH en lecture seule (200 lignes au plus).")
        def base_requete(sql: str, limite: int = 50) -> str:
            return outils.base_requete(profil, sql, limite)

    if profil.groundcover_filtre:
        @serveur.tool(description="Cherche dans les logs groundcover de l'environnement (heures ISO en UTC, "
                                  "30 minutes au plus ; texte libre ou filtres gcQL, sans pipe).")
        def logs_rechercher(debut: str, fin: str, recherche: str = "", limite: int = 50) -> str:
            return outils.logs_rechercher(profil, debut, fin, recherche, limite)

    if bitbucket.configure(profil):
        @serveur.tool(description="Bitbucket : dépôts lisibles (filtre par nom, clé de projet).")
        def bitbucket_depots(recherche: str = "", projet: str = "") -> str:
            return outils.bitbucket_depots(profil, recherche, projet)

        @serveur.tool(description="Bitbucket : branches dont le nom contient 'filtre' (ex. clé de ticket), dans un "
                                  "dépôt, un projet ou par défaut les dépôts du profil.")
        def bitbucket_branches(filtre: str = "", depot: str = "", projet: str = "") -> str:
            return outils.bitbucket_branches(profil, filtre, depot, projet)

        @serveur.tool(description="Bitbucket : fichiers d'un dépôt sur une branche, filtrés par motif.")
        def bitbucket_fichiers(depot: str, branche: str = "", dossier: str = "", motif: str = "") -> str:
            return outils.bitbucket_fichiers(profil, depot, branche, dossier, motif)

        @serveur.tool(description="Bitbucket : lit un fichier (lignes numérotées) ou liste un dossier, sur une branche.")
        def bitbucket_lire(depot: str, chemin: str, branche: str = "", debut: int = 1, nombre: int = 400) -> str:
            return outils.bitbucket_lire(profil, depot, chemin, branche, debut, nombre)

        @serveur.tool(description="Bitbucket : pull request, commits et fichiers modifiés d'une branche par rapport "
                                  "à sa cible, ou diff d'un fichier.")
        def bitbucket_comparer(depot: str, branche: str, vers: str = "", fichier: str = "") -> str:
            return outils.bitbucket_comparer(profil, depot, branche, vers, fichier)

        @serveur.tool(description="Bitbucket : recherche de code (branche par défaut de chaque dépôt).")
        def bitbucket_chercher(texte: str, depot: str = "", projet: str = "") -> str:
            return outils.bitbucket_chercher(profil, texte, depot, projet)

    return serveur


def main() -> int:
    parser = argparse.ArgumentParser(description="Serveur MCP Odatio (lecture seule)")
    parser.add_argument("--profil", required=True)
    args = parser.parse_args()
    config.charger_secrets()
    try:
        profil = config.charger_profil(args.profil)
    except config.ConfigError as e:
        print(e, file=sys.stderr)  # stdout est réservé au protocole MCP
        return 2
    creer_serveur(profil).run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
