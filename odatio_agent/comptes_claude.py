"""
Comptes Claude du poste : un compte d'abonnement (Pro, Max, Team) par dossier de configuration de Claude Code
(~/.claude, ~/.claude-*), comme dans agent-savoye. Le compte connecté est lu avec `claude auth status`, en
utilisant le Claude Code embarqué par le SDK.
"""
import glob
import json
import os
import shutil
import subprocess
from pathlib import Path


def executable_claude() -> str:
    """Claude Code embarqué par le SDK, à défaut celui du PATH."""
    try:
        import claude_agent_sdk
        nom = "claude.exe" if os.name == "nt" else "claude"
        embarque = Path(claude_agent_sdk.__file__).parent / "_bundled" / nom
        if embarque.exists():
            return str(embarque)
    except ImportError:
        pass
    return shutil.which("claude") or "claude"


def dossiers() -> list:
    """Dossiers de configuration présents : ~/.claude puis ~/.claude-*."""
    maison = os.path.expanduser("~")
    candidats = [os.path.join(maison, ".claude")] + sorted(glob.glob(os.path.join(maison, ".claude-*")))
    return [d for d in candidats if os.path.isdir(d)]


def _env(dossier: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR")}
    if dossier:
        env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(dossier)
    return env


def compte_connecte(dossier: str) -> str:
    """« e-mail, organisation » du compte connecté dans ce dossier, « non connecté », ou '' si inconnu."""
    try:
        resultat = subprocess.run([executable_claude(), "auth", "status"], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", env=_env(dossier), timeout=30)
        etat = json.loads(resultat.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    if not etat.get("loggedIn"):
        return "non connecté"
    return ", ".join(v for v in (etat.get("email"), etat.get("orgName")) if v) or "connecté"


def lancer_connexion(dossier: str) -> bool:
    """Ouvre une console qui lance `claude auth login` pour ce dossier (Windows) ; False ailleurs."""
    if os.name != "nt":
        return False
    if dossier:
        os.makedirs(os.path.expanduser(dossier), exist_ok=True)
    subprocess.Popen([executable_claude(), "auth", "login"], env=_env(dossier),
                     creationflags=subprocess.CREATE_NEW_CONSOLE)
    return True
