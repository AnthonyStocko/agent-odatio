"""Liens préremplis d'agent-manager-savoye : lecture des paramètres et application dans l'assistant."""
import contextlib
from types import SimpleNamespace
from unittest import mock

from odatio_agent import config, historique, ihm_assistant, lien_manager, outils

from .outillage import AvecDossierTemporaire

TACHE = {"cle": "ODA-1", "titre": "Tâche fictive", "texte": "Description fictive"}


class Relance(Exception):
    pass


class TestLire(AvecDossierTemporaire):
    def test_lire(self):
        self.assertEqual(lien_manager.lire({"projet": "local", "ticket": "ODA-1"}), {})   # pas du manager
        lien = lien_manager.lire({"source": "manager", "projet": "Client-X", "ticket": "oda-1",
                                  "conversation": "20261119-101500-000001", "question": " Pourquoi ? ",
                                  "contexte": "Résumé\nfictif", "page": "assistant"})
        self.assertEqual(lien, {"projet": "Client-X", "ticket": "ODA-1", "conversation": "20261119-101500-000001",
                                "question": "Pourquoi ?", "contexte": "Résumé fictif"})
        self.assertEqual(lien_manager.lire({"source": "manager", "ticket": "x y", "conversation": "../a"}), {})
        self.assertEqual(lien_manager.projet("client_x", ["autre", "Client-X"]), "Client-X")
        self.assertEqual(lien_manager.projet("inconnu", ["Client-X"]), "")


class TestAppliquer(AvecDossierTemporaire):
    def setUp(self):
        super().setUp()
        self.etat = SimpleNamespace(messages=[], reprise="", conversation_id="", profil_actif="autre", ticket=None,
                                    documentation=None, avis="", lien={}, lien_signature="", question_manager="")
        self.avertissements = []
        self.params = {}
        faux_st = SimpleNamespace(
            session_state=self.etat, query_params=SimpleNamespace(to_dict=lambda: dict(self.params)),
            spinner=lambda *_: contextlib.nullcontext(), warning=self.avertissements.append,
            rerun=mock.Mock(side_effect=Relance))
        patch = mock.patch.object(ihm_assistant, "st", faux_st)
        patch.start()
        self.addCleanup(patch.stop)
        mock.patch.object(config, "noms_profils", lambda: ["autre", "local"]).start()
        mock.patch.object(config, "jira_configure", lambda: True).start()
        self.addCleanup(mock.patch.stopall)
        self.lu = []
        mock.patch.object(outils, "jira_ticket", lambda cle, profil: self.lu.append(cle) or dict(TACHE, cle=cle)).start()

    def test_projet_tache_et_question_une_fois(self):
        self.params = {"source": "manager", "projet": "LOCAL", "ticket": "ODA-1", "question": "Comment tester ?"}
        ihm_assistant._lien_manager_projet()
        self.assertEqual(self.etat.profil_actif, "local")
        with self.assertRaises(Relance):
            ihm_assistant._lien_manager_suite(self.profil())
        self.assertEqual((self.lu, self.etat.ticket["cle"], self.etat.question_manager),
                         (["ODA-1"], "ODA-1", "Comment tester ?"))
        self.assertEqual(self.etat.messages, [])            # aucune question posée
        # Rechargement de la page : rien n'est réappliqué
        self.etat.profil_actif = "autre"
        ihm_assistant._lien_manager_projet()
        ihm_assistant._lien_manager_suite(self.profil())
        self.assertEqual((self.etat.profil_actif, self.lu), ("autre", ["ODA-1"]))

    def test_conversation_du_manager_reprise(self):
        ident = historique.enregistrer("local", {"reprise": "fil-1", "ticket": TACHE, "messages": [
            {"role": "user", "texte": "Question fictive"}, {"role": "assistant", "texte": "Réponse fictive"}]})
        self.params = {"source": "manager", "projet": "local", "conversation": ident, "ticket": "ODA-1"}
        ihm_assistant._lien_manager_projet()
        with self.assertRaises(Relance):
            ihm_assistant._lien_manager_suite(self.profil())
        self.assertEqual((self.etat.conversation_id, self.etat.reprise, len(self.etat.messages)), (ident, "fil-1", 2))
        self.assertEqual(self.etat.ticket["cle"], "ODA-1")   # tâche relue, conversation gardée

    def test_valeurs_inconnues(self):
        self.params = {"source": "manager", "projet": "inconnu", "conversation": "20261119-101500-000009"}
        ihm_assistant._lien_manager_projet()
        self.assertEqual(self.etat.profil_actif, "autre")
        self.assertIn("inconnu", self.etat.avis)
        self.etat.profil_actif = "local"
        with self.assertRaises(Relance):
            ihm_assistant._lien_manager_suite(self.profil())
        self.assertTrue(self.avertissements and "introuvable" in self.avertissements[0])
