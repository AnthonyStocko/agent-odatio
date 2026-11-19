"""Configuration commune d'agent-manager-savoye : variables transmises au lancement et applications."""
import os
from pathlib import Path
from unittest import mock

from odatio_agent import config

from .outillage import AvecDossierTemporaire

COMMUN = """
[acces]
JIRA_DOMAIN = "societe.atlassian.net"

[[applications]]
nom = "canson-prod"
description = "Production"
jira_projet = "CAN"
url = "https://appli.exemple.io"
utilisateur_application = "testeur"

[[applications.sources]]
nom = "standard"
depot = "https://git.exemple.io/scm/std/standard.git"
branche = "integration"

[applications.base]
type = "postgresql"
hote = "base.exemple.io"
nom = "odatio"
utilisateur = "lecture"

[applications.logs]
groundcover_filtre = "namespace:canson"

[applications.logs.ssh]
hote = "10.0.0.1"
utilisateur = "lecteur"
dossiers = ["/var/log/odatio"]
"""

LOCAL = """
[profils.Canson-prod]
description = "ancienne description"
fichiers_interdits = ["secret-local*.properties"]

[[profils.Canson-prod.sources]]
chemin = "C:/vieux"

[profils.Canson-prod.fichiers]
dossier = ""
donnees_reelles = true

[profils.Canson-prod.logs.ssh]
hote = "ancien"
dossiers = ["/ancien"]
horodatage = "%d/%m/%Y %H:%M:%S"

[profils.local]
[[profils.local.sources]]
chemin = "C:/dev/standard"
"""


class AvecManager(AvecDossierTemporaire):
    def setUp(self):
        super().setUp()
        for nom, fichier in (("FICHIER_PROFILS", "profils.toml"), ("FICHIER_SECRETS", ".env")):
            patch = mock.patch.object(config, nom, self.racine / fichier)
            patch.start()
            self.addCleanup(patch.stop)
        secrets = mock.patch.dict(config._SECRETS, clear=True)
        secrets.start()
        self.addCleanup(secrets.stop)
        self.commun = self.racine / "manager-config"
        self.commun.mkdir()
        (self.commun / "commun.toml").write_text(COMMUN, encoding="utf-8")
        config.FICHIER_PROFILS.write_text(LOCAL, encoding="utf-8")
        for cible in (config._GEREES, config._COMMUN):
            self.addCleanup(cible.clear)
            cible.clear()

    def lancer(self, variables: dict, dotenv: str = "") -> None:
        """Simule un lancement par le manager : variables dans l'environnement, puis lecture des secrets."""
        config.FICHIER_SECRETS.write_text(dotenv, encoding="utf-8")
        env = {**variables, "SAVOYE_CONFIG_DIR": str(self.commun),
               "SAVOYE_VARIABLES": ",".join(variables)}
        with mock.patch.dict(os.environ, env):
            config.charger_secrets()


class TestVariablesDuManager(AvecManager):
    def test_le_manager_l_emporte_sur_le_env(self):
        self.lancer({"JIRA_API_TOKEN": "du-manager", "JIRA_DOMAIN": "manager.atlassian.net"},
                    dotenv="JIRA_API_TOKEN=du-env\nJIRA_EMAIL=moi@exemple.com\n")
        self.assertEqual(config.secret("JIRA_API_TOKEN"), "du-manager")
        self.assertEqual(config.secret("JIRA_DOMAIN"), "manager.atlassian.net")
        self.assertEqual(config.secret("JIRA_EMAIL"), "moi@exemple.com")       # non transmis : le .env reste
        self.assertTrue(config.geree("JIRA_API_TOKEN"))
        self.assertNotIn("JIRA_API_TOKEN", os.environ)                          # retiré de l'environnement
        self.assertEqual(config.dossier_commun(), self.commun)
        config.charger_secrets()                                                # nouvelle lecture : toujours le manager
        self.assertEqual(config.secret("JIRA_API_TOKEN"), "du-manager")

    def test_variable_geree_non_ecrite_dans_le_env(self):
        self.lancer({"JIRA_API_TOKEN": "du-manager"}, dotenv="JIRA_API_TOKEN=du-env\n")
        config.enregistrer_secrets({"JIRA_API_TOKEN": "saisi-ici", "JIRA_EMAIL": "moi@exemple.com"})
        self.assertEqual(config.secret("JIRA_API_TOKEN"), "du-manager")
        texte = config.FICHIER_SECRETS.read_text(encoding="utf-8")
        self.assertNotIn("saisi-ici", texte)
        self.assertIn("JIRA_EMAIL=moi@exemple.com", texte)

    def test_lance_seul_rien_ne_change(self):
        config.FICHIER_SECRETS.write_text("JIRA_API_TOKEN=du-env\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"JIRA_API_TOKEN": "du-shell"}):
            config.charger_secrets()
        self.assertEqual(config.secret("JIRA_API_TOKEN"), "du-env")
        self.assertFalse(config.gerees())
        self.assertIsNone(config.dossier_commun())
        self.assertEqual(config.noms_profils(), ["Canson-prod", "local"])


class TestApplicationsDuManager(AvecManager):
    def test_application_fusionnee_avec_les_reglages_propres(self):
        self.lancer({"DB_PASSWORD_CANSON_PROD": "mdp"})
        self.assertEqual(config.noms_profils(), ["canson-prod", "local"])
        p = config.charger_profil("canson-prod")
        self.assertTrue(p.gere)
        self.assertEqual(p.description, "Production")
        self.assertEqual([(s.nom, s.depot, s.branche) for s in p.sources],
                         [("standard", "https://git.exemple.io/scm/std/standard.git", "integration")])
        self.assertEqual((p.base.hote, p.base.mot_de_passe_env), ("base.exemple.io", "DB_PASSWORD_CANSON_PROD"))
        self.assertEqual(config.secret(p.base.mot_de_passe_env), "mdp")
        self.assertEqual(p.groundcover_filtre, "namespace:canson")
        self.assertEqual((p.logs_ssh.hote, p.logs_ssh.dossiers, p.logs_ssh.mot_de_passe_env),
                         ("10.0.0.1", ["/var/log/odatio"], "SSH_PASSWORD_CANSON_PROD"))
        self.assertEqual(p.logs_ssh.horodatage, "%d/%m/%Y %H:%M:%S")        # réglage propre gardé
        self.assertTrue(p.donnees_reelles)
        self.assertIn("secret-local*.properties", p.fichiers_interdits)
        self.assertEqual((p.application.url, p.application.mot_de_passe_env),
                         ("https://appli.exemple.io", "APP_PASSWORD_CANSON_PROD"))
        self.assertFalse(config.charger_profil("local").gere)

    def test_enregistrer_garde_les_entrees_locales_des_applications_du_manager(self):
        self.lancer({})
        tomli_w = mock.MagicMock()
        ecrit = {}
        tomli_w.dump.side_effect = lambda donnees, f: ecrit.update(donnees)
        with mock.patch.dict("sys.modules", {"tomli_w": tomli_w}):
            config.enregistrer(config.Reglages(), config.tous_les_profils())
        self.assertEqual(set(ecrit["profils"]), {"local", "Canson-prod"})
        self.assertEqual(ecrit["profils"]["Canson-prod"]["description"], "ancienne description")   # intacte
        with mock.patch.dict("sys.modules", {"tomli_w": tomli_w}), self.assertRaises(config.ConfigError):
            config.enregistrer(config.Reglages(), [config.Profil(nom="canson_prod",
                                                                 sources=[config.Source(nom="s", chemin="C:/x")])])

    def test_sans_profils_toml(self):
        config.FICHIER_PROFILS.unlink()
        self.lancer({})
        self.assertEqual(config.noms_profils(), ["canson-prod"])
        self.assertTrue(config.tous_les_profils()[0].gere)
