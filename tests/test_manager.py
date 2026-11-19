"""Questions transmises par agent-manager-savoye : lecture seule, réponse et conversation enregistrée."""
import asyncio
import io
import json
from contextlib import redirect_stdout
from unittest import mock

from odatio_agent import config, consignes, historique, manager, outils, session
from odatio_agent.session import Bilan

from .outillage import AvecDossierTemporaire

ECRITURE = {"generation_demarrer", "fichier_ecrire", "documentation_demarrer", "documentation_enregistrer",
            "ecran_ouvrir", "ecran_cliquer", "ecran_saisir", "ecran_capturer", "ecran_observer"}


class TestLectureSeule(AvecDossierTemporaire):
    def outils(self, lecture_seule: bool) -> set:
        s = session.Session(self.profil(), [("standard", str(self.source))], [], config.Reglages(),
                            lecture_seule=lecture_seule)
        return {o.name for o in s._outils_maison()}, s.options.system_prompt

    def test_aucun_outil_qui_ecrit_ou_agit(self):
        with mock.patch.object(session.ecran, "disponible", return_value=True):
            normal, prompt_normal = self.outils(False)
            lecture, prompt_lecture = self.outils(True)
        self.assertTrue({"fichier_ecrire", "documentation_demarrer"} <= normal)
        self.assertFalse(lecture & ECRITURE)
        self.assertIn("Question transmise par le manager", prompt_lecture)
        self.assertNotIn("Question transmise par le manager", prompt_normal)
        self.assertTrue(prompt_lecture.startswith(prompt_normal))      # aucune règle existante retirée


class FausseSession:
    def __init__(self, profil, prets, logs, reglages, utilisateur, ticket=None, lecture_seule=False):
        FausseSession.vue = {"profil": profil.nom, "lecture_seule": lecture_seule, "ticket": ticket}

    async def ouvrir(self):
        print("bruit sur stdout")       # ne doit pas atteindre le protocole MCP

    async def demander(self, question, sur_texte, sur_outil):
        return Bilan(cout_usd=0.042, tours=3, erreur=False, session_id="sess-1", reponse=f"Réponse à : {question}")

    async def fermer(self):
        pass


class TestQuestion(AvecDossierTemporaire):
    def setUp(self):
        super().setUp()
        profil = self.profil()
        patches = [mock.patch.object(config, "charger_profil", return_value=profil),
                   mock.patch.object(manager.sources, "preparer", return_value=[("standard", str(self.source))]),
                   mock.patch.object(manager.sources, "dossiers_logs", return_value=[]),
                   mock.patch.object(manager, "Session", FausseSession),
                   mock.patch.object(config, "reglages", return_value=config.Reglages())]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_reponse_et_conversation_enregistree(self):
        sortie = io.StringIO()
        with redirect_stdout(sortie):
            texte = asyncio.run(manager.question("essai", "Comment est calculée la date ?"))
        self.assertEqual(sortie.getvalue(), "")                           # stdout intact
        self.assertTrue(FausseSession.vue["lecture_seule"])
        self.assertIn("Réponse à : Comment est calculée la date ?", texte)
        self.assertIn("3 étape(s), 0.042 $", texte)
        conversations = historique.lister("essai")
        self.assertEqual(len(conversations), 1)
        self.assertIn(conversations[0]["id"], texte)

    def test_question_vide_refusee(self):
        with self.assertRaises(outils.OutilErreur):
            asyncio.run(manager.question("essai", "  "))

    def test_projet_inconnu(self):
        with mock.patch.object(config, "charger_profil", side_effect=config.ConfigError("Profil « x » inconnu.")):
            with self.assertRaises(outils.OutilErreur) as e:
                asyncio.run(manager.question("x", "question"))
        self.assertIn("inconnu", str(e.exception))


class TestProjets(AvecDossierTemporaire):
    def test_liste(self):
        p = self.profil(groundcover_filtre="namespace:x", gere=True)
        with mock.patch.object(config, "tous_les_profils", return_value=[p]):
            texte = manager.projets()
        self.assertIn("essai (configuration commune du manager)", texte)
        self.assertIn("logs", texte)
