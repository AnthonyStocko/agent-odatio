"""Résumé lisible d'une étape de l'agent (outil utilisé), commun à la ligne de commande et à l'IHM."""
import os


def resumer(nom: str, entree: dict, racines: list) -> str:
    nom_court = nom.rsplit("__", 1)[-1]
    if nom_court == "Read":
        chemin = entree.get("file_path", "")
        for racine in racines:
            chemin = chemin.replace(str(racine) + os.sep, "")
        return f"Lit {chemin}"
    if nom_court == "Grep":
        return f"Cherche « {entree.get('pattern', '')} »" + (f" dans {entree['glob']}" if entree.get("glob") else "")
    if nom_court == "Glob":
        return f"Liste {entree.get('pattern', '')}"
    if nom_court == "base_requete":
        return "Base : " + " ".join(entree.get("sql", "").split())[:160]
    if nom_court == "base_decrire":
        return f"Base : décrit la table {entree.get('table', '')}"
    if nom_court == "base_tables":
        return f"Base : liste les tables {entree.get('motif', '')}".rstrip()
    if nom_court == "logs_rechercher":
        return f"Logs : « {entree.get('recherche', '')} » de {entree.get('debut', '')} à {entree.get('fin', '')}"
    if nom_court == "generation_demarrer":
        return f"Prépare un fichier d'interface : {entree.get('objet', '')}".rstrip(" :")
    if nom_court == "fichier_ecrire":
        return f"Écrit le fichier {entree.get('nom', '')}"
    if nom_court == "documentation_demarrer":
        return f"Prépare une documentation : {entree.get('sujet', '')}".rstrip(" :")
    if nom_court == "documentation_enregistrer":
        return f"Enregistre la documentation ({len(entree.get('scenes') or [])} scènes)"
    if nom_court == "ecran_ouvrir":
        return f"Application : ouvre {entree.get('adresse') or 'la page d’accueil'}"
    if nom_court == "ecran_observer":
        return "Application : relit l'écran"
    if nom_court == "ecran_cliquer":
        return f"Application : clique sur « {entree.get('libelle', '')} »"
    if nom_court == "ecran_saisir":
        return f"Application : saisit « {entree.get('valeur', '')} » dans {entree.get('champ', '')}"
    if nom_court == "ecran_capturer":
        return f"Application : capture l'écran ({entree.get('nom', '')})"
    if nom_court.startswith("bitbucket_"):
        depot = entree.get("depot", "") or entree.get("projet", "")
        branche = f" @ {entree['branche']}" if entree.get("branche") else ""
        if nom_court == "bitbucket_lire":
            return f"Bitbucket : lit {depot}{branche} {entree.get('chemin', '')}"
        if nom_court == "bitbucket_comparer":
            cible = f" vers {entree['vers']}" if entree.get("vers") else ""
            fichier = f" ({entree['fichier']})" if entree.get("fichier") else ""
            return f"Bitbucket : compare {depot} {entree.get('branche', '')}{cible}{fichier}"
        if nom_court == "bitbucket_branches":
            return f"Bitbucket : branches « {entree.get('filtre', '')} » {depot}".rstrip()
        if nom_court == "bitbucket_chercher":
            return f"Bitbucket : cherche « {entree.get('texte', '')} » {depot}".rstrip()
        if nom_court == "bitbucket_fichiers":
            return f"Bitbucket : liste {depot}{branche} {entree.get('dossier', '')} {entree.get('motif', '')}".rstrip()
        return f"Bitbucket : dépôts « {entree.get('recherche', '')} » {entree.get('projet', '')}".rstrip()
    return nom_court
