# Agent fonctionnel Odatio

Un agent à qui tu poses des questions fonctionnelles sur une application Odatio (« comment est calculée la date
de préparation ? », « pourquoi cette commande est-elle bloquée ? »). Il lit les sources, interroge la base en
lecture seule et consulte les logs, puis répond en termes métier avec ses preuves (fichier et ligne, table,
ligne de log). Il ne modifie jamais rien.

Construit sur le [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview) (Python). Les accès base
et logs reprennent les modules d'agent-savoye (`tools/db_tools.py`, `tools/groundcover_tools.py`,
`tools/secret_tools.py`, commit `f80a124`), copiés sans modification.

## Installation (Windows)

```powershell
cd C:\
git clone <ce dépôt> odatio-agent
cd odatio-agent
py -3.12 -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
copy profils.example.toml profils.toml
copy .env.example .env
```

Décommente dans `requirements.txt` le pilote de ta base (PostgreSQL par défaut ; Oracle, SQL Server, MySQL),
complète `profils.toml` (un profil par application à interroger) et mets les mots de passe et jetons dans `.env`.

Le SDK embarque Claude Code. Pour l'authentification : `ANTHROPIC_API_KEY` dans le `.env` (recommandé), ou à
défaut le compte Claude Code déjà connecté sur le poste.

## Utilisation

### Avec l'IHM (recommandé)

Double-clique sur `lancer.bat` : le navigateur s'ouvre sur http://localhost:8502 après quelques secondes.
Laisse la fenêtre noire ouverte tant que tu utilises l'agent ; la fermer arrête l'IHM.

Dans la barre latérale, choisis l'application (le profil), puis pose ta question en bas de la page. Pendant
la recherche, le bandeau « L'agent cherche… » se déplie pour montrer les fichiers lus et les requêtes ; la
réponse s'affiche ensuite avec son coût. Les questions suivantes gardent le fil de la conversation.
« Nouvelle conversation » repart de zéro, « Télécharger la conversation » l'enregistre en Markdown, et la
section Configuration permet de tester les connexions ou de mettre à jour les dépôts.

Le port 8502 est choisi pour ne pas gêner agent-savoye (8501) ; il se change dans `.streamlit/config.toml`.

### En ligne de commande

```powershell
venv\Scripts\python.exe agent.py --liste                  # profils disponibles
venv\Scripts\python.exe agent.py --profil local --test    # vérifie sources, logs, base, groundcover
venv\Scripts\python.exe agent.py --profil local           # conversation
venv\Scripts\python.exe agent.py --profil local --question "Que fait le bouton Libérer de l'écran des vagues ?"
```

En conversation, `/nouveau` repart de zéro, `/quitter` termine.

Les dépôts Git d'un profil sont clonés dans `.cache/sources/` à la première question, puis mis à jour à chaque
démarrage de l'IHM ou de la ligne de commande (`--sans-maj` pour l'éviter en ligne de commande).

## Un profil

| Section | Contenu |
|---|---|
| `sources` | Une ou plusieurs : `chemin` (dossier local ou partage) ou `depot` + `branche` (cloné avec `BITBUCKET_HTTP_TOKEN`). Avec le Standard et le spécifique client, l'agent distingue ce qui relève de chacun. |
| `logs` | `dossiers` (fichiers lus avec Grep/Read) et/ou `groundcover_filtre` (filtre gcQL imposé à toute recherche). |
| `base` | Connexion en lecture seule ; le mot de passe est dans le `.env`, sous le nom donné par `mot_de_passe_env`. |
| `fichiers_interdits` | Motifs de fichiers que l'agent ne lit jamais, en plus des `.env`, clés et keystores. |

## Sécurité

Plusieurs barrières, aucune ne suffit seule :

- **Outils** : seuls Read, Grep et Glob sont chargés (ni Bash, ni Edit, ni Write), plus les outils maison base et
  logs ; tout autre outil est refusé (`permission_mode="dontAsk"`). Les réglages, CLAUDE.md et serveurs MCP du
  poste sont ignorés.
- **Chemins** : un hook vérifie avant chaque lecture que le fichier est dans les sources ou les logs du profil,
  et refuse les fichiers de secrets.
- **Base** : garde-fous de `db_tools` (une seule instruction SELECT/WITH, mots-clés d'écriture refusés,
  transaction en lecture seule, résultat plafonné) et un nombre d'accès par question limité. **Utilise toujours
  un compte base en lecture seule** : c'est la seule garantie absolue.
- **Secrets** : lus une fois puis retirés de l'environnement, pour que le processus Claude Code n'en hérite pas ;
  posés le temps d'un appel base ou logs seulement, et masqués dans les résultats.
- **Journal** : `.cache/journal/outils.jsonl` trace chaque accès base ou logs (tables lues, erreurs), jamais le
  texte des requêtes ni les données.

Limite connue : Grep peut faire apparaître une ligne d'un fichier de configuration qui contient un mot de passe.
Ajoute ces fichiers à `fichiers_interdits` et préfère des comptes dont les droits sont réduits au minimum.

## Serveur MCP (facultatif)

`mcp_server.py` expose les mêmes outils base et logs en serveur MCP, pour les utiliser depuis Claude Code ou
Claude Desktop sans passer par l'agent :

```powershell
venv\Scripts\python.exe -m pip install "mcp>=2,<3"
claude mcp add odatio-local -- C:\odatio-agent\venv\Scripts\python.exe C:\odatio-agent\mcp_server.py --profil local
```

## Mettre à jour les modules repris d'agent-savoye

Recopie `db_tools.py`, `groundcover_tools.py` et `secret_tools.py` depuis `agent-savoye/tools/`, puis relance
`agent.py --profil <nom> --test`.
