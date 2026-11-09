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
    return nom_court
