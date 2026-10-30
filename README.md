# Agent fonctionnel Odatio

Un agent à qui tu poses des questions fonctionnelles sur une application Odatio (« comment est calculée la date
de préparation ? », « pourquoi cette commande est-elle bloquée ? »). Il lit les sources, interroge la base en
lecture seule et consulte les logs, puis répond en termes métier avec ses preuves (fichier et ligne, table,
ligne de log). Il ne modifie jamais rien.

Construit sur le [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview) (Python). Les accès base
et logs reprennent les modules d'agent-savoye (`tools/db_tools.py`, `tools/groundcover_tools.py`,
`tools/secret_tools.py`, `tools/jira_tools.py`, commit `f80a124`), copiés sans modification.

## Installation (Windows)

```powershell
cd C:\
git clone https://github.com/AnthonyStocko/agent-odatio.git
cd agent-odatio
py -3.12 -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
```

Le pilote PostgreSQL est installé par défaut ; pour Oracle, SQL Server ou MySQL, l'onglet Configuration indique
la commande à lancer. Le SDK embarque Claude Code : rien d'autre à installer.

## Utilisation

Double-clique sur `lancer.bat` : le navigateur s'ouvre sur http://localhost:8502 après quelques secondes.
Laisse la fenêtre noire ouverte tant que tu utilises l'agent ; la fermer arrête l'IHM. Le port 8502 évite
agent-savoye (8501) ; il se change dans `.streamlit/config.toml`.

### Onglet Configuration

- **Projets** : les applications Odatio sur lesquelles tu travailles. Pour chacune, ses sources (dossiers locaux
  ou dépôts Git clonés automatiquement, par exemple le Standard et le spécifique client), ses dossiers de logs
  ou son filtre groundcover, sa base (mot de passe enregistré dans le `.env`) et les fichiers que l'agent ne
  doit jamais lire. « Enregistrer et tester » vérifie chaque accès.
- **Claude** : le compte utilisé, soit un abonnement connecté (un dossier de configuration par compte,
  `~/.claude`, `~/.claude-pro`…, comme dans agent-savoye ; « Connecter ce compte » ouvre la connexion), soit une
  clé API. Également le modèle, l'effort et les plafonds par question.
- **Accès** : Jira, Bitbucket et groundcover. « Reprendre les réglages d'agent-savoye » copie ceux qui y sont
  déjà renseignés.

### Onglet Assistant

Choisis le projet dans la barre latérale. Pour préparer un test, saisis la clé du ticket Jira et clique sur
« Charger la tâche » : l'agent reçoit le ticket (description, épopée, fiches liées) et le bouton « Comment
tester cette tâche ? » lui demande ce qu'elle change, les données à préparer, les cas à jouer et ce qu'il faut
vérifier. Tu peux aussi poser directement tes questions ; il garde le fil de la conversation.

Pendant la recherche, le bandeau « L'agent cherche… » montre les fichiers lus et les requêtes ; la réponse
s'affiche ensuite avec son coût. « Nouvelle conversation » repart de zéro, « Télécharger la conversation »
l'enregistre en Markdown.

### En ligne de commande

```powershell
venv\Scripts\python.exe agent.py --liste
venv\Scripts\python.exe agent.py --profil canson --test
venv\Scripts\python.exe agent.py --profil canson --ticket ODA-1234
venv\Scripts\python.exe agent.py --profil canson --question "Que fait le bouton Libérer de l'écran des vagues ?"
```

## Un projet (profils.toml)

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
- **Jira** : lecture seule ; le contenu d'un ticket est transmis comme une donnée, jamais comme une consigne.
- **Journal** : `.cache/journal/outils.jsonl` trace chaque accès base, logs ou Jira (tables lues, erreurs), jamais le
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

Recopie `db_tools.py`, `groundcover_tools.py`, `secret_tools.py` et `jira_tools.py` depuis `agent-savoye/tools/`, puis relance
`agent.py --profil <nom> --test`.
