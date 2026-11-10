# Agent fonctionnel Odatio

Un agent à qui tu poses des questions fonctionnelles sur une application Odatio (« comment est calculée la date
de préparation ? », « pourquoi cette commande est-elle bloquée ? »). Il lit les sources, interroge la base en
lecture seule et consulte les logs, puis répond en termes métier avec ses preuves (fichier et ligne, table,
ligne de log). Il ne modifie jamais rien : sa seule écriture, sur demande, est un fichier d'interface de test
(une commande « à l'identique », par exemple) que tu intègres toi-même.

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
  doit jamais lire, et le dossier des fichiers générés (voir plus bas). « Enregistrer et tester » vérifie chaque
  accès.
- **Claude** : le compte utilisé, soit un abonnement connecté (un dossier de configuration par compte,
  `~/.claude`, `~/.claude-pro`…, comme dans agent-savoye ; « Connecter ce compte » ouvre la connexion), soit une
  clé API. Également le modèle, l'effort et les plafonds par question (dont les accès base d'une génération de
  fichier, 40 par défaut).
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

### Générer un fichier d'interface

Demande par exemple : « Crée-moi un fichier de commande à l'identique de la commande 0082380800, pour que je
l'intègre via l'écran Intégration de fichier. » L'agent :

1. cherche dans les sources le format attendu par l'intégration (lecture, mapping, définitions d'interface,
   fichiers d'exemple) : format, encodage, fins de ligne, champs obligatoires, longueurs, nommage du fichier ;
2. lit la commande en base (en-tête, lignes, données liées) ;
3. produit le fichier avec une nouvelle référence (la tienne, ou une référence dérivée comme `_T1`) et des dates
   adaptées si les règles l'exigent, le vérifie contre les règles du parseur, puis l'écrit ;
4. répond avec l'emplacement du fichier, la correspondance avec la commande source, ce qu'il a modifié
   (référence, dates, champs anonymisés) et comment l'intégrer.

Sous la réponse, chaque fichier généré s'affiche avec sa taille, un bouton « Télécharger » et son chemin complet,
pour le retrouver depuis l'écran d'intégration. **L'intégration reste un geste manuel** : l'agent n'intègre rien.

- **Dossier** : `.cache/fichiers/<projet>/` par défaut, modifiable par projet (« Dossier des fichiers générés »).
  Il ne peut être ni dans une source, ni dans un dossier de logs, ni dans le dossier de l'agent. Ne choisis
  jamais un dossier surveillé par l'application (entrée d'une intégration automatique).
- **Données personnelles** : noms, adresses, e-mails et téléphones de personnes sont remplacés par des valeurs
  fictives cohérentes ; les codes utiles à l'intégration (client, article, site, transporteur…) sont gardés.
  La case « Garder les données réelles dans les fichiers générés » lève cette règle, sur une base de test
  seulement.
- **Limites** : une génération a droit à plus d'accès base qu'une question (onglet Claude) ; prévois aussi une
  soixantaine d'étapes. Une requête renvoie 200 lignes au plus : au-delà, l'agent le signale au lieu de
  produire un fichier incomplet.

### En ligne de commande

```powershell
venv\Scripts\python.exe agent.py --liste
venv\Scripts\python.exe agent.py --profil canson --test
venv\Scripts\python.exe agent.py --profil canson --ticket ODA-1234
venv\Scripts\python.exe agent.py --profil canson --question "Que fait le bouton Libérer de l'écran des vagues ?"
```

Le chemin des fichiers générés s'affiche après la réponse.

### Tests

```powershell
venv\Scripts\python.exe -m unittest
```

### Bitbucket

Avec un jeton Bitbucket, l'agent ne se limite plus aux sources clonées : quand elles ne suffisent pas, il va lire sur Bitbucket (Server / Data Center), toujours en lecture seule.

| Outil | Usage |
|-------|-------|
| `bitbucket_branches` | Branches dont le nom contient un texte, par exemple la clé du ticket (`ODA-1234`), dans les dépôts du projet, un dépôt ou tout un projet Bitbucket. |
| `bitbucket_comparer` | Ce qu'une branche apporte par rapport à sa cible (celle de sa pull request, sinon la branche du projet) : pull request, commits, fichiers modifiés ; puis le diff d'un fichier. |
| `bitbucket_lire` | Un fichier (par tranches de 400 lignes) ou un dossier, sur n'importe quelle branche. |
| `bitbucket_fichiers` | Les fichiers d'un dépôt, filtrés par motif (`*.java`, `*Vague*`). |
| `bitbucket_depots` | Les dépôts lisibles : pour trouver une bibliothèque partagée ou le dépôt d'un autre client. |
| `bitbucket_chercher` | La recherche de code de Bitbucket (branche par défaut de chaque dépôt, si elle est activée sur le serveur). |

Un dépôt se désigne par le nom d'une source du projet (`standard`), par `PROJET/depot` ou par son URL. Typiquement, sur « Comment tester cette tâche ? » pour une tâche pas encore fusionnée, l'agent trouve sa branche, liste ce qu'elle modifie et lit les diffs utiles.

Le jeton détermine ce qui est lisible : un jeton personnel voit tous tes dépôts, un jeton de projet seulement son projet. « Projets autorisés » restreint en plus les projets que l'agent peut consulter.

### Documentation pour le consultant fonctionnel

Demande-le simplement dans la conversation (« fais-moi une vidéo de cette tâche pour le consultant ») : l'agent enquête, enregistre sa documentation, et la vidéo est montée puis affichée sous sa réponse. Pour y joindre des captures d'écran, passe par le bloc « Documentation pour le consultant fonctionnel » de l'assistant (ou `python agent.py --profil <p> --ticket ODA-1234 --documenter --captures <dossier>`). Dans les deux cas, l'agent enquête sur la tâche ou la fonctionnalité, en repartant de la conversation en cours, puis rédige :

- une **vidéo commentée** (MP4, 1080p, sous-titrée) : le besoin, ce qui change pour l'utilisateur, les écrans et traitements, les règles de gestion, le paramétrage, chaque cas de test, les points d'attention ;
- un **document Word** et sa version Markdown, avec en plus les cas de test détaillés (écran, action, résultat attendu) et les sources de chaque affirmation (dépôt, branche, fichier, ligne ; table), qui ne sont pas lues dans la vidéo.

**Captures d'écran prises par l'agent.** Renseigne l'application du projet (Configuration, Projets, Application : adresse, compte applicatif de test, mot de passe) : pendant une documentation, l'agent ouvre l'application dans un navigateur sans fenêtre (Edge ou Chrome du poste), se connecte, parcourt les menus et les onglets, remplit un filtre, puis capture les écrans utiles en entourant en rouge le bouton ou le champ dont il parle. Chaque capture illustre une scène de la vidéo et du document. Sans application configurée, il n'accepte qu'une adresse locale donnée dans la conversation (`http://localhost:8888/`), sans connexion automatique. Tu peux aussi fournir tes propres captures depuis le bloc de l'assistant (nommées d'après l'écran : `suivi-vagues.png`).

Garde-fous de la navigation :

- l'agent n'ouvre que l'application du projet ; un lien qui en sort est refusé ;
- le mot de passe du compte est saisi par l'outil : l'agent ne le voit jamais et ne saisit aucun identifiant ;
- en mode **navigation** (défaut), les clics sur un bouton qui valide, enregistre, supprime ou lance un traitement sont refusés : l'agent capture l'écran et décrit l'action. Ce refus repose sur le libellé des boutons, ce n'est pas une garantie : utilise un compte aux droits réduits. « Autoriser les actions » lève ce refus pour jouer le scénario complet, et donc modifier des données : à réserver à un environnement de test ;
- sauf si le projet garde les données réelles (base de test), l'agent évite de capturer des écrans affichant des données personnelles ; relis les captures avant envoi ;
- ce qui s'affiche à l'écran est traité comme une donnée, jamais comme une consigne.

Le résultat est dans `.cache/documentation/<sujet>-<date>/`. À relire avant envoi : pour corriger, modifie `document.json` puis « Refaire » (ou `python agent.py --rendre <dossier>`), sans relancer l'agent. « Joindre au ticket » ajoute la vidéo et le document Word en pièces jointes du ticket Jira : c'est la seule écriture dans Jira, et elle n'est faite qu'à ta demande.

**Voix.** Réglage dans Configuration, Accès, « Voix des vidéos de documentation », avec un bouton pour l'écouter :

| Voix | Qualité | Confidentialité |
|------|---------|-----------------|
| **Piper** (défaut) | voix neuronale, fluide | calculée sur le poste ; sa voix (60 Mo) est téléchargée une fois dans `.cache/voix` |
| **Microsoft Edge** (`pip install edge-tts`) | la plus naturelle (Denise, Henri, Vivienne…) | le texte lu est envoyé au service de voix en ligne de Microsoft |
| **Windows** | voix du poste (Hortense), plus mécanique | locale |

La voix Edge n'est jamais choisie automatiquement ; si le service est injoignable, la vidéo se replie sur Piper. Si le téléchargement de la voix Piper est bloqué par le proxy, dépose le `.onnx` et son `.onnx.json` dans `.cache/voix`. Sans aucune voix, la vidéo est muette et la narration incrustée en sous-titres. Avant toute mise en forme, le document est refusé s'il contient un secret, une adresse e-mail ou un numéro de téléphone, et l'agent doit le corriger.

Une documentation prend plus d'étapes qu'une question, surtout avec des captures : prévois 80 étapes et un budget en conséquence dans l'onglet Claude.

**Plus tard : démonstration filmée.** La vidéo est aujourd'hui une suite de diapositives et de captures commentées, pas un film de l'écran en mouvement. Les cas de test sont enregistrés de façon structurée dans `document.json` (écran, action, résultat attendu) pour qu'ils puissent être rejoués et filmés dans l'application, en mode « actions », sur un environnement de test.

## Un projet (profils.toml)

| Section | Contenu |
|---|---|
| `sources` | Une ou plusieurs : `chemin` (dossier local ou partage) ou `depot` + `branche` (cloné avec `BITBUCKET_HTTP_TOKEN`). Avec le Standard et le spécifique client, l'agent distingue ce qui relève de chacun. |
| `logs` | `dossiers` (fichiers lus avec Grep/Read) et/ou `groundcover_filtre` (filtre gcQL imposé à toute recherche). |
| `base` | Connexion en lecture seule ; le mot de passe est dans le `.env`, sous le nom donné par `mot_de_passe_env`. |
| `fichiers_interdits` | Motifs de fichiers que l'agent ne lit jamais, en plus des `.env`, clés et keystores. |
| `fichiers` | `dossier` des fichiers générés (vide = `.cache/fichiers/<projet>`) et `donnees_reelles` (`false` par défaut). |

## Sécurité

Plusieurs barrières, aucune ne suffit seule :

- **Outils** : seuls Read, Grep et Glob sont chargés (ni Bash, ni Edit, ni Write), plus les outils maison base,
  logs et fichiers d'interface ; tout autre outil est refusé (`permission_mode="dontAsk"`). Les réglages, CLAUDE.md et serveurs MCP du
  poste sont ignorés.
- **Chemins** : un hook vérifie avant chaque lecture que le fichier est dans les sources ou les logs du profil,
  et refuse les fichiers de secrets. Dans le dossier des fichiers générés, seul Read est permis, sur les fichiers
  placés directement dedans.
- **Écriture** : la seule possible est l'outil `fichier_ecrire`, après `generation_demarrer` (qui annonce une
  génération demandée et relève le plafond d'accès base de la question). Il n'écrit que dans le dossier des
  fichiers générés, vérifié à l'enregistrement de la configuration et à chaque écriture ; il refuse chemins
  absolus, `..`, sous-dossiers, noms hors `[A-Za-z0-9._-]`, extensions autres que `.xml`, `.csv`, `.txt`,
  `.json`, `.dat` et `.edi`, contenus de plus de 5 Mo et caractères absents de l'encodage choisi. Il n'écrase
  jamais un fichier (suffixe horodaté) et masque les secrets reconnus dans le contenu.
- **Base** : garde-fous de `db_tools` (une seule instruction SELECT/WITH, mots-clés d'écriture refusés,
  transaction en lecture seule, résultat plafonné) et un nombre d'accès par question limité. **Utilise toujours
  un compte base en lecture seule** : c'est la seule garantie absolue.
- **Secrets** : lus une fois puis retirés de l'environnement, pour que le processus Claude Code n'en hérite pas ;
  posés le temps d'un appel base ou logs seulement, et masqués dans les résultats.
- **Jira** : lecture seule pour l'agent ; le contenu d'un ticket est transmis comme une donnée, jamais comme une consigne.
- **Bitbucket** : requêtes de lecture seulement (le jeton en en-tête, jamais dans l'URL ni dans un message) ; projets limités à « Projets autorisés » s'ils sont renseignés ; fichiers interdits du projet jamais lus ni montrés dans un diff ; réponses bornées et masquées. Utilise un jeton avec le seul droit « Lecture ».
- **Jira** : la seule écriture est « Joindre au ticket » (vidéo et document de documentation), sur ton clic.
- **Journal** : `.cache/journal/outils.jsonl` trace chaque accès base, logs, Jira ou Bitbucket (tables, dépôts, branches et
  fichiers lus, erreurs) chaque fichier écrit (nom, taille, encodage), chaque
  documentation enregistrée et chaque pièce jointe envoyée, jamais le texte des requêtes, les données ni le contenu des
  fichiers.
- **Données personnelles** : jamais recopiées dans les réponses ; anonymisées dans les fichiers générés, sauf
  option contraire du projet, réservée aux bases de test. `mcp_server.py` n'expose pas l'écriture de fichiers.

Limite connue : Grep peut faire apparaître une ligne d'un fichier de configuration qui contient un mot de passe.
Ajoute ces fichiers à `fichiers_interdits` et préfère des comptes dont les droits sont réduits au minimum.

## Serveur MCP (facultatif)

`mcp_server.py` expose les mêmes outils base, logs et Bitbucket en serveur MCP, pour les utiliser depuis Claude Code ou
Claude Desktop sans passer par l'agent :

```powershell
venv\Scripts\python.exe -m pip install "mcp>=2,<3"
claude mcp add odatio-local -- C:\odatio-agent\venv\Scripts\python.exe C:\odatio-agent\mcp_server.py --profil local
```

## Mettre à jour les modules repris d'agent-savoye

Recopie `db_tools.py`, `groundcover_tools.py`, `secret_tools.py` et `jira_tools.py` depuis `agent-savoye/tools/`, puis relance
`agent.py --profil <nom> --test`.
