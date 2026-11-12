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


DOCUMENT_EN = {
    "langue": "en",
    "titre": "Batch history and the FIFO rule",
    "resume": "The first stock entry date of each product and batch pair is recorded and reused.",
    "scenes": [
        {"titre": "The need", "points": ["Old batches looked recent"],
         "narration": "On receipt, the batch was given today's date as its stock entry date."},
        {"titre": "What changes", "points": ["The first date is recorded"],
         "narration": "The first stock entry date of each product and batch pair is now recorded."},
        {"titre": "Business rules", "points": ["Products without batches are not affected"],
         "narration": "Products without batches are not affected."},
    ],
    "cas_de_test": [{"titre": "Receive a new batch", "prerequis": "A batch-managed product.",
                     "etapes": [{"ecran": "Dossiers d'arrivage", "action": "Process the shipment",
                                 "attendu": "the stock is created with today's date"},
                                {"ecran": "Historique des lots", "action": "Filter on Lot",
                                 "attendu": "one line with today's date"}]}],
    "points_attention": ["Screen labels are in French"],
}


class TestLangue(AvecDossierTemporaire):
    def test_langue_du_document(self):
        self.assertEqual(video.langue(DOCUMENT), "fr")
        self.assertEqual(video.langue(DOCUMENT_EN), "en")
        sans_champ = {k: v for k, v in DOCUMENT_EN.items() if k != "langue"}
        self.assertEqual(video.langue(sans_champ), "en")  # déduite du texte

    def test_video_anglaise_sans_francais(self):
        lu = " ".join(s["narration"] for s in video.sequences(DOCUMENT_EN))
        for francais in ("Cas de test", "Prérequis", "D’abord", "Ensuite", "on doit constater", "Points d'attention",
                         "Le document joint"):
            self.assertNotIn(francais, lu)
        self.assertIn("Test case 1. Receive a new batch. Prerequisites: A batch-managed product. First, process "
                      "the shipment. Expected result: The stock is created with today's date. Next, filter on Lot.",
                      lu)

    def test_video_francaise_inchangee(self):
        test = [s for s in video.sequences(DOCUMENT) if s["type"] == "test"][0]
        self.assertIn("D’abord, cliquer sur Libérer ; on doit constater que la vague passe au statut Libérée.",
                      test["narration"])
        self.assertEqual(test["sous_titre"], "Prérequis : Une vague planifiée")

    def test_document_ecrit_en_anglais(self):
        dossier = documentation.nouveau_dossier("ODA-2")
        documentation.enregistrer(dossier, copy.deepcopy(DOCUMENT_EN))
        texte = documentation.produire(dossier, "ODA-2", video_aussi=False, journal=lambda _: None)["markdown"] \
            .read_text(encoding="utf-8")
        self.assertIn("## Test cases", texte)
        self.assertIn("| # | Screen | Action | Expected result |", texte)
        self.assertNotIn("Prérequis", texte)

    def test_langue_inconnue_refusee(self):
        document = copy.deepcopy(DOCUMENT_EN)
        document["langue"] = "de"
        self.assertTrue(documentation.valider(document, []))

    def test_voix_de_la_langue(self):
        self.assertEqual(video.nom_pour("fr_FR-tom-medium, en_GB-alba-medium", "en"), "en_GB-alba-medium")
        self.assertEqual(video.nom_pour("fr_FR-tom-medium, en_GB-alba-medium", "fr"), "fr_FR-tom-medium")
        self.assertEqual(video.nom_pour("fr-FR-HenriNeural", "en"), "")          # voix anglaise par défaut
        self.assertEqual(video.nom_pour("fr-FR-RemyMultilingualNeural", "en"), "fr-FR-RemyMultilingualNeural")
        self.assertEqual(video.nom_pour("Hortense", "en"), "Hortense")           # Windows filtre par langue

    def test_moteur_par_langue(self):
        vus = []
        with mock.patch.dict(config._SECRETS, {"DOCUMENTATION_VOIX": "piper"}, clear=True), \
                mock.patch.object(video, "_disponible", side_effect=lambda m, n, j, l: vus.append(l) or (m, l)):
            video.oublier_moteur()
            self.assertEqual(video.moteur_voix(lang="en"), ("piper", "en"))
            self.assertEqual(video.moteur_voix(lang="fr"), ("piper", "fr"))
        video.oublier_moteur()
        self.assertEqual(vus, ["en", "fr"])


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
