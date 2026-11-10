"""Tests de la documentation pour le consultant (odatio_agent.documentation, outils de la session)."""
import asyncio
import copy
from unittest import mock

from odatio_agent import config, consignes, documentation, ecran, video
from odatio_agent.session import Session

from .outillage import AvecDossierTemporaire

DOCUMENT = {
    "titre": "Libération manuelle des vagues",
    "resume": "Le chef d'équipe peut libérer une vague depuis l'écran de suivi.",
    "scenes": [
        {"titre": "Le besoin", "points": ["Les urgences attendaient le traitement planifié"],
         "narration": "Les vagues n'étaient libérées que par le traitement planifié."},
        {"titre": "Ce qui change", "points": ["Bouton Libérer sur le suivi des vagues"],
         "narration": "Un bouton Libérer apparaît sur l'écran de suivi des vagues."},
        {"titre": "Règles de gestion", "points": ["Seules les vagues planifiées sont libérables"],
         "narration": "Seules les vagues planifiées peuvent être libérées."},
    ],
    "cas_de_test": [{"titre": "Vague complète", "prerequis": "Une vague planifiée",
                     "etapes": [{"ecran": "Suivi des vagues", "action": "Cliquer sur Libérer",
                                 "attendu": "La vague passe au statut Libérée"}]}],
    "points_attention": ["Le traitement planifié continue de fonctionner"],
}


def _outils(session: Session) -> dict:
    return {t.name: t.handler for t in session._outils_maison()}


class TestValidation(AvecDossierTemporaire):
    def test_document_valide(self):
        self.assertEqual(documentation.valider(DOCUMENT, []), [])

    def test_refus(self):
        cas = {
            "scène": lambda d: d.update(scenes=d["scenes"][:2]),
            "capture": lambda d: d["scenes"][0].update(capture="inventee.png"),
            "secret": lambda d: d.update(resume=d["resume"] + " password=Secret123"),
            "e-mail": lambda d: d["scenes"][0].update(narration="Contacter jean.dupont@client.fr."),
            "téléphone": lambda d: d["scenes"][0].update(narration="Appeler le 06 12 34 56 78."),
            "cas de test": lambda d: d.update(cas_de_test=[]),
            "narration": lambda d: d["scenes"][0].update(narration="mot " * 400),
        }
        for attendu, modifier in cas.items():
            document = copy.deepcopy(DOCUMENT)
            modifier(document)
            erreurs = " ".join(documentation.valider(document, [])).lower()
            self.assertIn(attendu.split()[0][:6], erreurs, attendu)

    def test_capture_connue(self):
        dossier = documentation.nouveau_dossier("ODA-1")
        nom = documentation.ajouter_capture(dossier, "Suivi Vagues.PNG", b"x")
        self.assertEqual(nom, "suivi-vagues.png")
        document = copy.deepcopy(DOCUMENT)
        document["scenes"][1]["capture"] = nom
        self.assertEqual(documentation.valider(document, documentation.captures(dossier)), [])

    def test_rendu_ecrit(self):
        dossier = documentation.nouveau_dossier("ODA-1")
        documentation.enregistrer(dossier, copy.deepcopy(DOCUMENT))
        resultat = documentation.produire(dossier, "ODA-1 — Libération", video_aussi=False, journal=lambda _: None)
        texte = resultat["markdown"].read_text(encoding="utf-8")
        self.assertIn("| 1 | Suivi des vagues | Cliquer sur Libérer |", texte)
        self.assertIsNone(resultat["video"])

    def test_sequences_de_la_video(self):
        types = [s["type"] for s in video.sequences(DOCUMENT)]
        self.assertEqual(types, ["titre", "scene", "scene", "scene", "test", "scene", "fin"])


class TestOutilsDeSession(AvecDossierTemporaire):
    """La documentation se demande aussi dans la conversation : les deux outils sont toujours présents."""

    def session(self, **options) -> Session:
        return Session(self.profil(), [("standard", self.source)], [], config.Reglages(), "essai", **options)

    def test_consignes_annoncent_la_video(self):
        texte = consignes.construire(self.profil(), [("standard", self.source)], [], "essai", config.Reglages())
        self.assertIn("documentation_demarrer", texte)
        self.assertIn("Ne refuse", texte)

    def test_outils_toujours_presents_et_autorises(self):
        session = self.session()
        for nom in ("documentation_demarrer", "documentation_enregistrer"):
            self.assertIn(nom, _outils(session))
            self.assertIn(f"mcp__odatio__{nom}", session.options.allowed_tools)

    def test_enregistrer_exige_demarrer(self):
        session = self.session()
        reponse = asyncio.run(_outils(session)["documentation_enregistrer"](copy.deepcopy(DOCUMENT)))
        self.assertTrue(reponse["is_error"])
        self.assertIsNone(session.document)

    def test_parcours_depuis_la_conversation(self):
        session = self.session(ticket={"cle": "ODA-1234", "titre": "Libération", "texte": ""})
        outils = _outils(session)
        with mock.patch.object(ecran, "disponible", return_value=False):
            marche = asyncio.run(outils["documentation_demarrer"]({"sujet": "libération des vagues"}))
        self.assertIn("documentation_enregistrer", marche["content"][0]["text"])
        self.assertIn("aucune capture", marche["content"][0]["text"])
        dossier = session.dossier_documentation
        self.assertTrue(dossier.name.startswith("ODA-1234-"))
        self.assertEqual(documentation.sujet(dossier), "ODA-1234 — Libération")
        reponse = asyncio.run(outils["documentation_enregistrer"](copy.deepcopy(DOCUMENT)))
        self.assertFalse(reponse["is_error"])
        self.assertTrue((dossier / "document.json").exists())
        self.assertEqual(session.document["titre"], DOCUMENT["titre"])

    def test_dossier_fourni_avec_captures(self):
        dossier = documentation.nouveau_dossier("ODA-1")
        documentation.ajouter_capture(dossier, "suivi-vagues.png", b"x")
        session = self.session(dossier_documentation=dossier)
        marche = asyncio.run(_outils(session)["documentation_demarrer"]({"sujet": "x"}))
        self.assertIn("suivi-vagues.png", marche["content"][0]["text"])
        self.assertEqual(session.dossier_documentation, dossier)
