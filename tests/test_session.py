"""Construction de la session : chaque famille d'outils maison est bien déclarée et autorisée."""
from unittest import mock

from odatio_agent import bitbucket, config, session
from odatio_agent.logs_ssh import LogsSsh

from .outillage import AvecDossierTemporaire

BITBUCKET = ["bitbucket_depots", "bitbucket_branches", "bitbucket_fichiers", "bitbucket_lire", "bitbucket_comparer",
             "bitbucket_chercher"]
LOGS_SSH = ["logs_ssh_fichiers", "logs_ssh_chercher", "logs_ssh_lire"]


class TestOutilsDeLaSession(AvecDossierTemporaire):
    def noms(self, profil) -> tuple:
        s = session.Session(profil, [("standard", str(self.source))], [], config.Reglages())
        return [o.name for o in s._outils_maison()], s.options.allowed_tools

    def test_bitbucket_et_logs_ssh(self):
        profil = self.profil(logs_ssh=LogsSsh(hote="serveur-logs", dossiers=["/var/log/odatio"]))
        with mock.patch.object(bitbucket, "configure", return_value=True):
            noms, autorises = self.noms(profil)
        for nom in BITBUCKET + LOGS_SSH:
            self.assertIn(nom, noms)
            self.assertIn(f"mcp__{session.SERVEUR}__{nom}", autorises)

    def test_sans_bitbucket_ni_logs_ssh(self):
        with mock.patch.object(bitbucket, "configure", return_value=False):
            noms, _ = self.noms(self.profil())
        self.assertFalse(set(noms) & set(BITBUCKET + LOGS_SSH))
        self.assertIn("fichier_ecrire", noms)
