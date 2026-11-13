"""Tests de la reprise : historique des conversations, documentation poursuivie."""
import asyncio
import copy
import json
from unittest import mock

from odatio_agent import config, documentation, ecran, historique
from odatio_agent.session import Session

from .outillage import AvecDossierTemporaire
from .test_documentation import DOCUMENT

MESSAGES = [{"role": "user", "texte": "Comment est calculée la date de préparation ?"},
            {"role": "assistant", "texte": "Elle part de la date de livraison moins le délai du transporteur."}]


class TestHistorique(AvecDossierTemporaire):
    def test_enregistrer_lister_charger(self):
        ident = historique.enregistrer("local", {"reprise": "abc", "messages": MESSAGES,
                                                 "ticket": {"cle": "ODA-1", "titre": "x", "texte": ""}})
        liste = historique.lister("local")
        self.assertEqual([c["id"] for c in liste], [ident])
        self.assertEqual(liste[0]["titre"], "ODA-1 · Comment est calculée la date de préparation ?")
        self.assertEqual(liste[0]["questions"], 1)
        conversation = historique.charger("local", ident)
        self.assertEqual(conversation["reprise"], "abc")
        self.assertEqual(conversation["messages"], MESSAGES)
        self.assertEqual(historique.lister("autre"), [])

    def test_mise_a_jour_garde_l_identifiant_et_la_creation(self):
        ident = historique.enregistrer("local", {"messages": MESSAGES[:1]})
        cree = historique.charger("local", ident)["cree_le"]
        self.assertEqual(historique.enregistrer("local", {"id": ident, "messages": MESSAGES, "reprise": "r2"}), ident)
        conversation = historique.charger("local", ident)
        self.assertEqual((conversation["cree_le"], conversation["reprise"], len(conversation["messages"])),
                         (cree, "r2", 2))

    def test_identifiant_invalide(self):
        with self.assertRaises(config.ConfigError):
            historique.charger("local", "../../.env")
        historique.supprimer("local", "../../.env")  # sans effet

    def test_supprimer(self):
        ident = historique.enregistrer("local", {"messages": MESSAGES})
        historique.supprimer("local", ident)
        self.assertEqual(historique.lister("local"), [])

    def test_conversation_d_une_documentation(self):
        dossier = documentation.nouveau_dossier("ODA-1")
        historique.enregistrer("local", {"messages": MESSAGES})
        ident = historique.enregistrer("local", {"messages": MESSAGES,
                                                 "documentation": {"dossier": str(dossier), "sujet": "x"}})
        self.assertEqual(historique.de_la_documentation("local", str(dossier))["id"], ident)
        self.assertIsNone(historique.de_la_documentation("local", str(dossier.parent / "autre")))

    def _dater(self, profil: str, ident: str, maj_le: str) -> None:
        """Fixe la date de mise à jour d'une conversation (plusieurs enregistrements tombent dans la même seconde)."""
        chemin = historique._dossier(profil) / f"{ident}.json"
        donnees = json.loads(chemin.read_text(encoding="utf-8"))
        donnees["maj_le"] = maj_le
        chemin.write_text(json.dumps(donnees, ensure_ascii=False), encoding="utf-8")

    def test_lister_expose_la_cle_du_ticket(self):
        avec = historique.enregistrer("local", {"messages": MESSAGES,
                                                "ticket": {"cle": "ODA-1", "titre": "Tâche fictive", "texte": ""}})
        sans = historique.enregistrer("local", {"messages": MESSAGES})
        ancien = historique._dossier("local") / "20250101-080000-000000.json"   # format sans clé « ticket »
        ancien.write_text(json.dumps({"id": "20250101-080000-000000", "titre": "Ancienne", "maj_le": "2025-01-01T08:00:00",
                                      "messages": MESSAGES}), encoding="utf-8")
        tickets = {c["id"]: c["ticket"] for c in historique.lister("local")}
        self.assertEqual(tickets, {avec: "ODA-1", sans: "", "20250101-080000-000000": ""})

    def test_conversation_d_un_ticket(self):
        tache = {"cle": "ODA-1", "titre": "Tâche fictive", "texte": "Description fictive"}
        ancienne = historique.enregistrer("local", {"messages": MESSAGES[:1], "ticket": tache})
        recente = historique.enregistrer("local", {"messages": MESSAGES, "ticket": tache})
        autre = historique.enregistrer("local", {"messages": MESSAGES, "ticket": dict(tache, cle="ODA-2")})
        historique.enregistrer("local", {"messages": MESSAGES})
        self._dater("local", ancienne, "2026-01-01T10:00:00")
        self._dater("local", recente, "2026-01-02T10:00:00")
        self._dater("local", autre, "2026-01-03T10:00:00")
        self.assertEqual(historique.du_ticket("local", "oda-1 ")["id"], recente)
        self.assertEqual(historique.du_ticket("local", "ODA-2")["id"], autre)
        self.assertIsNone(historique.du_ticket("local", "ODA-3"))
        self.assertIsNone(historique.du_ticket("local", ""))
        self.assertIsNone(historique.du_ticket("autre", "ODA-1"))   # cloisonné par projet

    def test_recapitulatif(self):
        texte = historique.recapitulatif(MESSAGES, "Et pour un samedi ?")
        self.assertIn("délai du transporteur", texte)
        self.assertTrue(texte.endswith("Nouvelle question : Et pour un samedi ?"))
        self.assertEqual(historique.recapitulatif([], "Q"), "Q")
        long = [{"role": "user", "texte": f"ancien {i} " + "x" * 2000} for i in range(20)]
        texte = historique.recapitulatif(long, "Q")
        self.assertIn("omis", texte)
        self.assertIn("ancien 19", texte)
        self.assertNotIn("ancien 0 ", texte)


class TestDocumentationPoursuivie(AvecDossierTemporaire):
    def session(self, dossier=None) -> Session:
        with mock.patch.object(ecran, "disponible", return_value=False):
            return Session(self.profil(), [("standard", self.source)], [], config.Reglages(), "essai",
                           dossier_documentation=dossier)

    @staticmethod
    def outils(session):
        return {t.name: t.handler for t in session._outils_maison()}

    def test_lister_et_versions(self):
        dossier = documentation.nouveau_dossier("ODA-1")
        (dossier / "sujet.txt").write_text("ODA-1 — Libération", encoding="utf-8")
        documentation.enregistrer(dossier, copy.deepcopy(DOCUMENT))
        modifie = copy.deepcopy(DOCUMENT)
        modifie["titre"] = "Libération manuelle des vagues, version 2"
        documentation.enregistrer(dossier, modifie)
        self.assertEqual(len(list((dossier / "versions").glob("document-*.json"))), 1)
        self.assertEqual(documentation.existant(dossier)["titre"], modifie["titre"])
        self.assertNotIn("_genere_le", documentation.existant(dossier))
        liste = documentation.lister()
        self.assertEqual([(d["sujet"], d["video"]) for d in liste], [("ODA-1 — Libération", False)])
        documentation.produire(dossier, "ODA-1 — Libération", video_aussi=False, journal=lambda _: None)
        self.assertIn("markdown", documentation.fichiers(dossier))

    def test_demarrer_renvoie_le_document_existant(self):
        dossier = documentation.nouveau_dossier("ODA-1")
        (dossier / "sujet.txt").write_text("ODA-1 — Libération", encoding="utf-8")
        documentation.enregistrer(dossier, copy.deepcopy(DOCUMENT))
        session = self.session(dossier)
        outils = self.outils(session)
        marche = asyncio.run(outils["documentation_demarrer"]({"sujet": "autre libellé"}))["content"][0]["text"]
        self.assertIn("DOCUMENTATION EXISTANTE", marche)
        self.assertIn(DOCUMENT["scenes"][1]["narration"], marche)
        self.assertEqual(session.dossier_documentation, dossier)
        self.assertEqual(documentation.sujet(dossier), "ODA-1 — Libération")   # sujet gardé
        modifie = copy.deepcopy(DOCUMENT)
        modifie["cas_de_test"].append({"titre": "Vague vide", "etapes": [{"action": "Libérer", "attendu": "Refus"}]})
        reponse = asyncio.run(outils["documentation_enregistrer"](modifie))
        self.assertFalse(reponse["is_error"])
        self.assertEqual(len(json.loads((dossier / "document.json").read_text(encoding="utf-8"))["cas_de_test"]), 2)

    def test_nouvelle_documentation_dans_un_autre_dossier(self):
        dossier = documentation.nouveau_dossier("ODA-1")
        documentation.enregistrer(dossier, copy.deepcopy(DOCUMENT))
        session = self.session(dossier)
        marche = asyncio.run(self.outils(session)["documentation_demarrer"]({"sujet": "Inventaire",
                                                                             "nouvelle": True}))
        self.assertNotIn("DOCUMENTATION EXISTANTE", marche["content"][0]["text"])
        self.assertNotEqual(session.dossier_documentation, dossier)

    def test_sans_dossier_rien_d_existant(self):
        session = self.session()
        marche = asyncio.run(self.outils(session)["documentation_demarrer"]({"sujet": "Inventaire"}))
        self.assertNotIn("DOCUMENTATION EXISTANTE", marche["content"][0]["text"])
