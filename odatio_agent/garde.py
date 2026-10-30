"""
Garde-fou des outils de fichiers (hook PreToolUse du SDK, appelé avant CHAQUE utilisation d'un outil) :
Read, Grep et Glob ne peuvent viser que les sources et les dossiers de logs du profil, et Read refuse les
fichiers de secrets (.env, clés, keystores…). Tout autre outil intégré est déjà exclu par la configuration de
la session (seuls Read, Grep et Glob sont chargés) ; les outils maison (base, logs) ont leurs propres garde-fous.
"""
import os
from fnmatch import fnmatch
from pathlib import Path


def _normaliser(chemin) -> str:
    return os.path.normcase(os.path.realpath(chemin))


def creer_garde(racines: list, interdits: list):
    racines_norm = [_normaliser(r) for r in racines]
    motifs = [m.lower() for m in interdits]

    def dans_racines(chemin: str) -> bool:
        cible = _normaliser(chemin)
        return any(cible == r or cible.startswith(r.rstrip(os.sep) + os.sep) for r in racines_norm)

    def interdit(chemin: str) -> bool:
        nom = Path(chemin).name.lower()
        return any(fnmatch(nom, m) for m in motifs)

    def refus(raison: str) -> dict:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": raison}}

    async def garde(entree, tool_use_id, contexte) -> dict:
        outil = entree.get("tool_name", "")
        args = entree.get("tool_input") or {}
        if outil not in ("Read", "Grep", "Glob"):
            return {}
        base = entree.get("cwd") or str(racines[0])

        if outil == "Read":
            chemin = args.get("file_path", "")
        else:
            chemin = args.get("path") or base
            motif = args.get("pattern" if outil == "Glob" else "glob") or ""
            if ".." in motif.replace("\\", "/").split("/") or os.path.isabs(motif):
                return refus("Motif interdit : reste dans les dossiers autorisés (pas de chemin absolu ni de « .. »).")
        if not os.path.isabs(chemin):
            chemin = os.path.join(base, chemin)

        if not dans_racines(chemin):
            return refus(f"Accès refusé : {chemin} est hors des sources et des logs autorisés.")
        if interdit(chemin):
            return refus("Accès refusé : fichier de secrets (mots de passe, clés). Ne cherche pas à le lire.")
        return {}

    return garde
