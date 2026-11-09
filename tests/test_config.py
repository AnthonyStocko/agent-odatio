"""Tests de la configuration du dossier des fichiers générés (odatio_agent.config)."""
import tomllib
from unittest import mock

from odatio_agent import config

from .outillage import AvecDossierTemporaire


class TestDossierFichiers(AvecDossierTemporaire):
    def test_defaut(self):
        self.assertEqual(config.dossier_fichiers(self.profil()), self.cache / "fichiers" / "essai")
        self.assertEqual(config.erreur_dossier_fichiers(self.profil()), "")

    def test_dossier_a_part_accepte(self):
        self.assertEqual(config.erreur_dossier_fichiers(self.profil(dossier_fichiers=str(self.racine / "sortie"))), "")

    def test_refus_sous_une_source(self):
        erreur = config.erreur_dossier_fichiers(self.profil(dossier_fichiers=str(self.source / "in")))
        self.assertIn("source « standard »", erreur)

    def test_refus_d_une_source_elle_meme_ou_d_un_parent(self):
        for dossier in (self.source, self.source.parent, self.racine):
            with self.subTest(dossier=dossier):
                self.assertNotEqual(config.erreur_dossier_fichiers(self.profil(dossier_fichiers=str(dossier))), "")

    def test_refus_sous_un_depot_clone(self):
        profil = self.profil(sources=[config.Source(nom="std", depot="https://git/x/std.git")],
                             dossier_fichiers=str(self.cache / "sources" / "essai" / "std" / "in"))
        self.assertIn("source « std »", config.erreur_dossier_fichiers(profil))

    def test_refus_sous_un_dossier_de_logs(self):
        logs = self.racine / "logs"
        profil = self.profil(dossiers_logs=[str(logs)], dossier_fichiers=str(logs / "in"))
        self.assertIn("logs", config.erreur_dossier_fichiers(profil))

    def test_refus_dans_le_dossier_de_l_agent(self):
        self.assertNotEqual(config.erreur_dossier_fichiers(self.profil(dossier_fichiers=str(config.RACINE))), "")
        self.assertNotEqual(config.erreur_dossier_fichiers(
            self.profil(dossier_fichiers=str(config.RACINE / "odatio_agent"))), "")

    def test_enregistrer_refuse_et_relit(self):
        fichier = self.racine / "profils.toml"
        with mock.patch.object(config, "FICHIER_PROFILS", fichier):
            with self.assertRaisesRegex(config.ConfigError, "essai"):
                config.enregistrer(config.Reglages(), [self.profil(dossier_fichiers=str(self.source / "in"))])
            self.assertFalse(fichier.exists())

            sortie = str(self.racine / "sortie")
            config.enregistrer(config.Reglages(requetes_base_generation=55),
                               [self.profil(dossier_fichiers=sortie, donnees_reelles=True)])
            with open(fichier, "rb") as f:
                self.assertEqual(tomllib.load(f)["profils"]["essai"]["fichiers"],
                                 {"dossier": sortie, "donnees_reelles": True})
            profil = config.charger_profil("essai")
            self.assertEqual((profil.dossier_fichiers, profil.donnees_reelles), (sortie, True))
            self.assertEqual(config.reglages().requetes_base_generation, 55)


if __name__ == "__main__":
    import unittest
    unittest.main()
