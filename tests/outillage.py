"""Outillage commun des tests : un dossier temporaire tient lieu de .cache (journal, fichiers générés) et de
sources, pour ne jamais toucher à la configuration ni au journal réels."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from odatio_agent import config


class AvecDossierTemporaire(unittest.TestCase):
    def setUp(self):
        temporaire = tempfile.TemporaryDirectory()
        self.addCleanup(temporaire.cleanup)
        self.racine = Path(temporaire.name).resolve()
        self.cache = self.racine / "cache"
        self.source = self.racine / "sources" / "standard"
        self.source.mkdir(parents=True)
        patch = mock.patch.object(config, "CACHE", self.cache)
        patch.start()
        self.addCleanup(patch.stop)

    def profil(self, **valeurs) -> config.Profil:
        valeurs.setdefault("sources", [config.Source(nom="standard", chemin=str(self.source))])
        return config.Profil(nom="essai", **valeurs)
