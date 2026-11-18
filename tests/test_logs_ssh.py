"""Logs lus par SSH : contrôles, commandes construites (lecture seule) et exécution réelle des commandes
distantes dans un shell local (sans SSH), sur des fichiers de logs fictifs."""
import gzip
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from odatio_agent import config, consignes, logs_ssh, outils
from odatio_agent.logs_ssh import LogsSsh, LogsSshErreur

from .outillage import AvecDossierTemporaire

SHELL_LOCAL = all(shutil.which(p) for p in ("sh", "find", "zgrep", "zcat")) and os.name != "nt"


def reponse(code=0, sortie="", erreur=""):
    return subprocess.CompletedProcess([], code, sortie.encode("utf-8"), erreur.encode("utf-8"))


class SshFactice:
    """Remplace subprocess.run : garde les lignes de commande, renvoie les réponses prévues dans l'ordre."""

    def __init__(self, *reponses):
        self.reponses, self.appels = list(reponses), []

    def __call__(self, arguments, **_):
        self.appels.append(arguments)
        return self.reponses.pop(0)


def executer_en_local(arguments, **_):
    """Exécute la commande distante (dernier argument de ssh) dans un shell local, comme le ferait le serveur."""
    return subprocess.run(["sh", "-c", arguments[-1]], capture_output=True, timeout=30)


def programmes(distante: str) -> set:
    """Premiers mots de chaque commande d'une ligne de commande distante."""
    morceaux = re.split(r"\||;|&&|\$\(", distante)
    mots = set()
    for m in morceaux:
        m = m.strip().removeprefix("N=").strip()
        m = re.sub(r"^(LC_ALL=C\s+)", "", m)
        if m and m != ")":
            mots.add(m.split()[0].rstrip(")"))
    return mots


@mock.patch.object(logs_ssh.shutil, "which", lambda nom: "/usr/bin/" + nom)
class TestControles(unittest.TestCase):
    def setUp(self):
        self.c = LogsSsh(hote="serveur-logs", dossiers=["/var/log/odatio"])

    def test_configuration_valide_et_invalide(self):
        self.assertEqual(logs_ssh.erreurs(self.c), [])
        cas = {"hôte": LogsSsh(hote="serveur;rm", dossiers=["/var/log"]),
               "au moins un dossier": LogsSsh(hote="h"),
               "dossier invalide": LogsSsh(hote="h", dossiers=["var/log"]),
               "dossier invalide ": LogsSsh(hote="h", dossiers=["/var/log/../etc"]),
               "dossier invalide  ": LogsSsh(hote="h", dossiers=["/var/log odatio"]),
               "motif": LogsSsh(hote="h", dossiers=["/var/log"], motif="*.log;id"),
               "port": LogsSsh(hote="h", dossiers=["/var/log"], port="80a"),
               "largeur variable": LogsSsh(hote="h", dossiers=["/var/log"], horodatage="%d %B %Y %H:%M"),
               "%d, %H et %M": LogsSsh(hote="h", dossiers=["/var/log"], horodatage="%Y-%m-%d"),
               "rebond": LogsSsh(hote="h", dossiers=["/var/log"], rebond="moi@bastion;id")}
        for attendu, c in cas.items():
            with self.subTest(attendu):
                self.assertTrue(any(attendu.strip() in e for e in logs_ssh.erreurs(c)), logs_ssh.erreurs(c))

    def test_options_ssh(self):
        c = LogsSsh(hote="serveur-logs", dossiers=["/var/log"], utilisateur="lecture", port="2222",
                    cle="~/.ssh/cle_logs", rebond="moi@bastion")
        arguments = logs_ssh.commande_ssh(c, "find /var/log")
        texte = " ".join(arguments)
        for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", "ClearAllForwardings=yes", "ForwardAgent=no",
                       "RequestTTY=no", "IdentitiesOnly=yes", "-p 2222", "-l lecture", "-J moi@bastion"):
            self.assertIn(option, texte)
        self.assertEqual(arguments[-3:], ["--", "serveur-logs", "find /var/log"])
        self.assertEqual(arguments[arguments.index("-i") + 1], os.path.expanduser("~/.ssh/cle_logs"))

    def test_seuls_des_programmes_de_lecture(self):
        commandes = [logs_ssh.commande_lister(self.c),
                     logs_ssh.commande_chercher(["/var/log/odatio/a.log"], "CDE-0002", 5),
                     logs_ssh.commande_lire("/var/log/odatio/a.log", "^(2026)", 10)]
        for commande in commandes:
            with self.subTest(commande=commande):
                self.assertLessEqual(programmes(commande), set(logs_ssh.PROGRAMMES))
                self.assertNotIn(">", commande)
        injection = logs_ssh.commande_chercher(["/var/log/odatio/a.log"], "x' ; rm -rf / ; '", 0)
        self.assertIn("-e 'x'\"'\"' ; rm -rf / ; '\"'\"'' --", injection)   # texte entre guillemets, inerte

    def test_fichiers_refuses(self):
        executer = SshFactice()
        for fichier in ("/etc/passwd", "/var/log/odatio/../../etc/passwd", "var/log/odatio/a.log",
                        "/var/log/odatio/sous/a.log", "/var/log/odatio/config.properties",
                        "/var/log/odatio/a.log;id"):
            with self.subTest(fichier), self.assertRaises(LogsSshErreur):
                logs_ssh.lire(self.c, "2026-11-18T09:00:00", fichier=fichier, executer=executer)
        self.assertEqual(executer.appels, [])

    def test_parametres_refuses(self):
        executer = SshFactice()
        for texte, debut, fin in (("", "2026-11-18T09:00", "2026-11-18T10:00"),
                                  ("a\nb", "2026-11-18T09:00", "2026-11-18T10:00"),
                                  ("x" * 201, "2026-11-18T09:00", "2026-11-18T10:00"),
                                  ("x", "2026-11-18T10:00", "2026-11-18T09:00"),
                                  ("x", "2026-11-17T09:00", "2026-11-18T10:00"),
                                  ("x", "hier", "2026-11-18T10:00")):
            with self.subTest(texte=texte[:10], debut=debut), self.assertRaises(LogsSshErreur):
                logs_ssh.chercher(self.c, texte, debut, fin, executer=executer)
        self.assertEqual(executer.appels, [])

    def test_heure_utc_convertie_dans_l_heure_du_poste(self):
        utc = datetime(2026, 11, 18, 8, 41, tzinfo=timezone.utc)
        self.assertEqual(logs_ssh.heure("2026-11-18T08:41:00Z"), utc.astimezone().replace(tzinfo=None))
        self.assertEqual(logs_ssh.heure("2026-11-18T09:41:00"), datetime(2026, 11, 18, 9, 41))

    def test_erreurs_de_connexion(self):
        for erreur, attendu in (("Host key verification failed.", "connecte-toi une fois à la main"),
                                ("ssh: connect to host serveur-logs port 22: Connection refused", "Connexion SSH impossible")):
            with self.subTest(attendu), self.assertRaises(LogsSshErreur) as e:
                logs_ssh.lister(self.c, executer=SshFactice(reponse(255, erreur=erreur)))
            self.assertIn(attendu, str(e.exception))
        ok, message = logs_ssh.tester(self.c, executer=SshFactice(reponse(0, "")))
        self.assertFalse(ok)
        self.assertIn("aucun fichier", message)

    def test_liste_triee_et_lisible(self):
        sortie = "1763452800.5 2048 /var/log/odatio/a.log.1.gz\n1763456400.0 1048576 /var/log/odatio/a.log\nbruit\n"
        fichiers = logs_ssh.lister(self.c, executer=SshFactice(reponse(0, sortie)))
        self.assertEqual([f.chemin for f in fichiers], ["/var/log/odatio/a.log", "/var/log/odatio/a.log.1.gz"])
        self.assertIn("1.0 Mo", logs_ssh.texte_fichiers(fichiers))


@unittest.skipUnless(SHELL_LOCAL, "sh, find, zgrep et zcat nécessaires (Linux ou macOS)")
@mock.patch.object(logs_ssh.shutil, "which", lambda nom: "/usr/bin/" + nom)
class TestCommandesReelles(unittest.TestCase):
    """Les commandes distantes exécutées pour de vrai, dans un shell local, sur des logs fictifs."""

    def setUp(self):
        temporaire = tempfile.TemporaryDirectory()
        self.addCleanup(temporaire.cleanup)
        self.dossier = Path(temporaire.name) / "logs"
        self.dossier.mkdir()
        self.c = LogsSsh(hote="serveur-logs", dossiers=[str(self.dossier)])
        hier = ["2026-11-17 22:00:00,001 INFO  demarrage",
                "2026-11-17 23:59:58,500 ERROR CommandeException commande CDE-0001 refusee",
                "\tat com.exemple.Integration.lire(Integration.java:42)"]
        aujourd_hui = ["2026-11-18 09:40:59,999 INFO  avant la fenetre CDE-0002",
                       "2026-11-18 09:41:02,123 ERROR CommandeException commande CDE-0002 bloquee",
                       "\tat com.exemple.Vague.liberer(Vague.java:120)",
                       "\tat com.exemple.Vague.traiter(Vague.java:80)",
                       "2026-11-18 09:41:30,000 INFO  vague 12 liberee",
                       "[2026-11-18 09:45:00,000] WARN  stock insuffisant CDE-0002",
                       "2026-11-18 10:30:00,000 INFO  fin"]
        ancien = self.dossier / "app.log.1.gz"
        with gzip.open(ancien, "wt", encoding="utf-8") as f:
            f.write("\n".join(hier) + "\n")
        self.courant = self.dossier / "app.log"
        self.courant.write_text("\n".join(aujourd_hui) + "\n", encoding="utf-8")
        (self.dossier / "secrets.properties").write_text("motdepasse=1\n", encoding="utf-8")
        os.utime(ancien, (datetime(2026, 11, 18, 0, 0).timestamp(),) * 2)
        os.utime(self.courant, (datetime(2026, 11, 18, 10, 30).timestamp(),) * 2)

    def test_lister(self):
        fichiers = logs_ssh.lister(self.c, executer=executer_en_local)
        self.assertEqual([Path(f.chemin).name for f in fichiers], ["app.log", "app.log.1.gz"])
        self.assertEqual(fichiers[0].modifie, datetime(2026, 11, 18, 10, 30))
        ok, message = logs_ssh.tester(self.c, executer=executer_en_local)
        self.assertTrue(ok, message)

    def test_chercher_dans_la_fenetre_avec_contexte(self):
        texte = logs_ssh.chercher(self.c, "cde-0002", "2026-11-18T09:41:00", "2026-11-18T09:50:00",
                                  contexte=2, executer=executer_en_local)
        self.assertIn("app.log", texte.splitlines()[0])
        self.assertIn("CDE-0002 bloquee", texte)
        self.assertIn("Vague.liberer", texte)          # contexte : pile d'exception
        self.assertIn("stock insuffisant", texte)        # horodatage entre crochets
        self.assertNotIn("avant la fenetre", texte)
        self.assertNotIn("fin", texte.splitlines()[-1])

    def test_chercher_dans_un_fichier_compresse(self):
        texte = logs_ssh.chercher(self.c, "CommandeException", "2026-11-17T23:00:00", "2026-11-18T09:42:00",
                                  contexte=1, executer=executer_en_local)
        self.assertIn("CDE-0001 refusee", texte)
        self.assertIn("Integration.lire", texte)
        self.assertIn("CDE-0002 bloquee", texte)
        self.assertLess(texte.index("CDE-0001"), texte.index("CDE-0002"))   # du plus ancien au plus récent

    def test_texte_cherche_tel_quel_sans_injection(self):
        temoin = Path(self.dossier.parent) / "TEMOIN"
        for texte in (f"x'; touch {temoin}; echo '", f"$(touch {temoin})", f"`touch {temoin}`", ".*"):
            with self.subTest(texte):
                resultat = logs_ssh.chercher(self.c, texte, "2026-11-18T09:00:00", "2026-11-18T11:00:00",
                                             executer=executer_en_local)
                self.assertIn("aucune ligne", resultat)
        self.assertFalse(temoin.exists())

    def test_lire_une_periode(self):
        texte = logs_ssh.lire(self.c, "2026-11-18T09:41:01", minutes=5, executer=executer_en_local)
        lignes = texte.splitlines()
        self.assertEqual(lignes[0], f"Fichier : {self.courant}")
        self.assertIn("CDE-0002 bloquee", lignes[1])        # 09:40:59 écartée : avant l'heure demandée
        self.assertIn("Vague.traiter", texte)
        self.assertIn("stock insuffisant", texte)
        self.assertNotIn("fin", lignes[-1])                  # 10:30 : après la fenêtre
        self.assertIn("aucune ligne", logs_ssh.lire(self.c, "2026-11-18T08:00:00", minutes=10,
                                                    fichier=str(self.courant), executer=executer_en_local))

    def test_lire_un_fichier_compresse_et_limite(self):
        texte = logs_ssh.lire(self.c, "2026-11-17T23:59:00", minutes=5, fichier=str(self.dossier / "app.log.1.gz"),
                              limite=1, executer=executer_en_local)
        self.assertIn("CDE-0001 refusee", texte)
        self.assertNotIn("demarrage", texte)
        self.assertIn("ligne(s) de plus", texte)

    def test_fichier_hors_motif_refuse(self):
        with self.assertRaises(LogsSshErreur):
            logs_ssh.lire(self.c, "2026-11-18T09:00:00", fichier=str(self.dossier / "secrets.properties"),
                          executer=executer_en_local)


class TestIntegration(AvecDossierTemporaire):
    def test_outils_journal_sans_contenu_et_secrets_masques(self):
        profil = self.profil(logs_ssh=LogsSsh(hote="serveur-logs", dossiers=["/var/log/odatio"]))
        with mock.patch.object(logs_ssh, "chercher", return_value="ligne CDE-0002"):
            self.assertEqual(outils.logs_ssh_chercher(profil, "CDE-0002", "2026-11-18T09:00", "2026-11-18T10:00"),
                             "ligne CDE-0002")
        with mock.patch.object(logs_ssh, "lister", side_effect=LogsSshErreur("Connexion SSH impossible")):
            with self.assertRaises(outils.OutilErreur):
                outils.logs_ssh_fichiers(profil)
        journal = [json.loads(l) for l in (self.cache / "journal" / "outils.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([j["outil"] for j in journal], ["logs_ssh_chercher", "logs_ssh_fichiers"])
        self.assertNotIn("CDE-0002", json.dumps(journal))
        self.assertEqual(journal[0]["longueur_recherche"], 8)
        self.assertFalse(journal[1]["ok"])
        with self.assertRaises(outils.OutilErreur):
            outils.logs_ssh_fichiers(self.profil())

    def test_profil_lu_dans_profils_toml(self):
        brut = {"sources": [{"chemin": str(self.source)}],
                "logs": {"ssh": {"hote": "serveur-logs", "dossiers": ["/var/log/odatio"], "rebond": "moi@bastion"}}}
        profil = config._profil("production", brut, True)
        self.assertEqual((profil.logs_ssh.hote, profil.logs_ssh.dossiers, profil.logs_ssh.motif),
                         ("serveur-logs", ["/var/log/odatio"], "*.log*"))
        brut["logs"]["ssh"]["dossiers"] = ["../etc"]
        with self.assertRaises(config.ConfigError):
            config._profil("production", brut, True)
        self.assertIsNone(config._profil("local", {"sources": [{"chemin": str(self.source)}]}, True).logs_ssh)

    def test_consignes(self):
        profil = self.profil(logs_ssh=LogsSsh(hote="serveur-logs", dossiers=["/var/log/odatio"]))
        texte = consignes.construire(profil, [("standard", str(self.source))], [], "Anthony", config.Reglages())
        self.assertIn("logs_ssh_chercher", texte)
        self.assertIn("PRODUCTION", texte)
        self.assertIn("par […]", texte)
        self.assertNotIn("logs_ssh", consignes.construire(self.profil(), [("standard", str(self.source))], [],
                                                          "Anthony", config.Reglages()))


if __name__ == "__main__":
    unittest.main()


class FauxParamiko:
    """Module paramiko simulé : enregistre la connexion, renvoie une sortie ou lève l'erreur prévue."""

    class AuthenticationException(Exception):
        pass

    class SSHException(Exception):
        pass

    class BadHostKeyException(SSHException):
        pass

    class RejectPolicy:
        pass

    def __init__(self, sortie=b"", erreur=None):
        self.sortie, self.erreur, self.connexions, self.commandes = sortie, erreur, [], []
        faux = self

        class Canal:
            def recv_exit_status(self):
                return 0

        class Flux:
            def __init__(self, contenu):
                self.contenu, self.channel = contenu, Canal()

            def read(self):
                return self.contenu

        class SSHClient:
            def load_host_keys(self, chemin):
                faux.known_hosts = chemin

            def set_missing_host_key_policy(self, politique):
                faux.politique = politique

            def connect(self, hote, **options):
                faux.connexions.append((hote, options))
                if faux.erreur:
                    raise faux.erreur

            def exec_command(self, commande, timeout=None):
                faux.commandes.append(commande)
                return None, Flux(faux.sortie), Flux(b"")

            def close(self):
                faux.fermee = True

        self.SSHClient = SSHClient


class TestMotDePasse(unittest.TestCase):
    def setUp(self):
        logs_ssh._REFUSES.clear()
        self.c = logs_ssh.avec_mot_de_passe(
            LogsSsh(hote="10.0.0.1", utilisateur="DOMAINE\\lecteur", dossiers=["/var/log/odatio"],
                    mot_de_passe_env="SSH_PASSWORD_PROD"), "motdepasse-secret")

    def test_connexion_par_mot_de_passe(self):
        faux = FauxParamiko(sortie=b"1763456400.0 10 /var/log/odatio/a.log\n")
        with mock.patch.object(logs_ssh, "_paramiko", return_value=faux):
            fichiers = logs_ssh.lister(self.c)
        self.assertEqual([f.chemin for f in fichiers], ["/var/log/odatio/a.log"])
        hote, options = faux.connexions[0]
        self.assertEqual((hote, options["port"], options["username"], options["password"]),
                         ("10.0.0.1", 22, "DOMAINE\\lecteur", "motdepasse-secret"))
        self.assertFalse(options["look_for_keys"] or options["allow_agent"])
        self.assertIsInstance(faux.politique, FauxParamiko.RejectPolicy)      # serveur inconnu refusé
        self.assertTrue(faux.fermee)
        self.assertLessEqual(programmes(faux.commandes[0]), set(logs_ssh.PROGRAMMES))
        self.assertNotIn("motdepasse", repr(self.c))

    def test_mot_de_passe_refuse_jamais_reessaye(self):
        faux = FauxParamiko(erreur=FauxParamiko.AuthenticationException("refus"))
        with mock.patch.object(logs_ssh, "_paramiko", return_value=faux):
            for _ in range(3):
                with self.assertRaises(LogsSshErreur) as e:
                    logs_ssh.lister(self.c)
                self.assertIn("aucun nouvel essai", str(e.exception))
            self.assertEqual(len(faux.connexions), 1)
            faux.erreur = None
            logs_ssh.lister(logs_ssh.avec_mot_de_passe(self.c, "nouveau-mot-de-passe"))   # corrigé : nouvel essai
        self.assertEqual(len(faux.connexions), 2)

    def test_erreurs_de_connexion(self):
        cas = ((FauxParamiko.SSHException("Server '10.0.0.1' not found in known_hosts"), "connecte-toi une fois"),
               (FauxParamiko.BadHostKeyException("changée"), "a changé"),
               (OSError("No route to host"), "Connexion SSH impossible"))
        for erreur, attendu in cas:
            with self.subTest(attendu), mock.patch.object(logs_ssh, "_paramiko", return_value=FauxParamiko(erreur=erreur)):
                with self.assertRaises(LogsSshErreur) as e:
                    logs_ssh.lister(self.c)
                self.assertIn(attendu, str(e.exception))

    def test_rebond_et_utilisateur(self):
        for c in (replace(self.c, rebond="moi@bastion"), replace(self.c, utilisateur="")):
            with self.subTest(c.rebond or "sans utilisateur"), self.assertRaises(LogsSshErreur):
                logs_ssh.lister(c)
        self.assertIn("utilisateur invalide", logs_ssh.erreurs(replace(self.c, utilisateur="a;b"))[0])


class TestMotDePasseIntegration(AvecDossierTemporaire):
    def test_secret_du_env_transmis_et_masque(self):
        profil = self.profil(logs_ssh=LogsSsh(hote="10.0.0.1", utilisateur="lecteur", dossiers=["/var/log/odatio"],
                                              mot_de_passe_env="SSH_PASSWORD_PROD"))
        vu = {}

        def lister(c):
            vu["mot_de_passe"] = c.mot_de_passe
            raise LogsSshErreur("échec avec s3cr3t-ssh dedans")

        with mock.patch.dict(config._SECRETS, {"SSH_PASSWORD_PROD": "s3cr3t-ssh"}), \
                mock.patch.object(logs_ssh, "lister", side_effect=lister):
            with self.assertRaises(outils.OutilErreur) as e:
                outils.logs_ssh_fichiers(profil)
        self.assertEqual(vu["mot_de_passe"], "s3cr3t-ssh")
        self.assertNotIn("s3cr3t", str(e.exception))
        self.assertNotIn("s3cr3t", (self.cache / "journal" / "outils.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(config.nom_mot_de_passe_ssh("Canson-prod"), "SSH_PASSWORD_CANSON_PROD")

    def test_profil_avec_mot_de_passe(self):
        brut = {"sources": [{"chemin": str(self.source)}],
                "logs": {"ssh": {"hote": "10.0.0.1", "utilisateur": "lecteur", "dossiers": ["/var/log/odatio"],
                                 "mot_de_passe_env": "SSH_PASSWORD_PROD"}}}
        self.assertEqual(config._profil("prod", brut, True).logs_ssh.mot_de_passe_env, "SSH_PASSWORD_PROD")
