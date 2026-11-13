"""Tests de l'assistant (logique seule, sans rendu Streamlit) : reprise de la conversation d'une tâche Jira."""
from types import SimpleNamespace
from unittest import mock

from odatio_agent import historique, ihm_assistant

from .outillage import AvecDossierTemporaire

TACHE = {"cle": "ODA-1", "titre": "Tâche fictive", "texte": "Description fictive enregistrée"}
MESSAGES = [{"role": "user", "texte": "Comment tester cette tâche ?"},
            {"role": "assistant", "texte": "Préparer une commande fictive, puis libérer la vague."}]


class TestChargerTache(AvecDossierTemporaire):
    def setUp(self):
        super().setUp()
        self.etat = SimpleNamespace(messages=[], reprise="", conversation_id="", profil_actif="local",
                                    ticket=None, documentation=None, avis="")
        patch = mock.patch.object(ihm_assistant, "st", SimpleNamespace(session_state=self.etat))
        patch.start()
        self.addCleanup(patch.stop)

    def test_charger_tache_reprend_la_conversation(self):
        ident = historique.enregistrer("local", {"reprise": "fil-1", "ticket": TACHE, "messages": MESSAGES})
        relu = dict(TACHE, texte="Description fictive mise à jour")
        reprise = ihm_assistant.charger_tache(relu)
        self.assertEqual(reprise["id"], ident)
        self.assertEqual((self.etat.messages, self.etat.reprise, self.etat.conversation_id),
                         (MESSAGES, "fil-1", ident))
        self.assertEqual(self.etat.ticket, relu)   # ticket fraîchement lu, pas la copie enregistrée

    def test_charger_tache_sans_historique_demarre_vide(self):
        historique.enregistrer("local", {"reprise": "fil-1", "ticket": TACHE, "messages": MESSAGES})
        tache = dict(TACHE, cle="ODA-9")
        self.assertIsNone(ihm_assistant.charger_tache(tache))
        self.assertEqual((self.etat.messages, self.etat.reprise, self.etat.conversation_id, self.etat.documentation),
                         ([], "", "", None))
        self.assertEqual(self.etat.ticket, tache)

    def test_charger_tache_remplace_la_conversation_en_cours(self):
        ident_tache = historique.enregistrer("local", {"reprise": "fil-1", "ticket": TACHE, "messages": MESSAGES})
        en_cours = [{"role": "user", "texte": "Question fictive sans tâche"},
                    {"role": "assistant", "texte": "Réponse fictive"}]
        ident_en_cours = historique.enregistrer("local", {"reprise": "fil-2", "messages": en_cours})
        self.etat.messages, self.etat.reprise, self.etat.conversation_id = list(en_cours), "fil-2", ident_en_cours
        ihm_assistant.charger_tache(dict(TACHE))
        self.assertEqual((self.etat.messages, self.etat.reprise, self.etat.conversation_id),
                         (MESSAGES, "fil-1", ident_tache))
        self.assertEqual(historique.charger("local", ident_en_cours)["messages"], en_cours)   # toujours là
