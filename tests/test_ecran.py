"""Tests des garde-fous de la navigation dans l'application (odatio_agent.ecran), sans navigateur."""
import asyncio
from unittest import mock

from odatio_agent import config, consignes, ecran, video
from odatio_agent.session import Session

from .outillage import AvecDossierTemporaire

APPLICATION = config.Application(url="http://localhost:8888/odatio/", utilisateur="testeur",
                                 mot_de_passe_env="APP_PASSWORD_ESSAI")


class TestLibellesRefuses(AvecDossierTemporaire):
    def test_ecritures_refusees(self):
        for libelle in ("Valider", "Enregistrer", "Supprimer la ligne", "Libérer", "OK", "Oui", "Lancer le calcul",
                        "Confirmer", "Générer les missions", "Annuler la commande", "Clôturer", "Save"):
            self.assertTrue(ecran._ECRITURE.search(libelle), libelle)

    def test_navigation_acceptee(self):
        for libelle in ("Suivi des vagues", "Paramétrage", "Rechercher", "Détail", "Annuler", "Fermer", "Suivant",
                        "Créer une vague", "Okapi", "Historique des mouvements"):
            self.assertFalse(ecran._ECRITURE.search(libelle), libelle)

    def clic(self, libelle: str, nature: str, actions: bool = False):
        application = config.Application(url=APPLICATION.url, actions=actions)
        navigateur = ecran.Navigateur(self.profil(application=application))
        element = mock.AsyncMock()
        element.evaluate.return_value = nature
        navigateur.page = mock.MagicMock(url=APPLICATION.url)
        with mock.patch.object(navigateur, "_demarrer", new=mock.AsyncMock()), \
                mock.patch.object(navigateur, "_trouver", new=mock.AsyncMock(return_value=element)), \
                mock.patch.object(navigateur, "_stabiliser", new=mock.AsyncMock()), \
                mock.patch.object(navigateur, "observer", new=mock.AsyncMock(return_value="écran")):
            asyncio.run(navigateur.cliquer(libelle))
        return element

    def test_bouton_d_ecriture_refuse(self):
        for libelle, nature in (("Valider", "BUTTON"), ("Supprimer", "A"), ("Enregistrer", "menuitem"),
                                ("Validation en masse", "BUTTON"), ("Libérer", "DIV")):
            with self.assertRaises(ecran.EcranErreur, msg=libelle) as refus:
                self.clic(libelle, nature)
            self.assertIn("mode navigation", str(refus.exception))

    def test_menu_au_nom_trompeur_accepte(self):
        for libelle, nature in (("Validation des réceptions", "A"), ("Historique des validations", "menuitem"),
                                ("Suivi des vagues", "BUTTON"), ("Import / export", "tab")):
            self.clic(libelle, nature).click.assert_awaited_once()

    def test_mode_actions(self):
        self.clic("Valider", "BUTTON", actions=True).click.assert_awaited_once()


class TestAdresses(AvecDossierTemporaire):
    def test_application_configuree(self):
        navigateur = ecran.Navigateur(self.profil(application=APPLICATION))
        self.assertEqual(navigateur._adresse(""), "http://localhost:8888/odatio/")
        self.assertEqual(navigateur._adresse("vagues"), "http://localhost:8888/odatio/vagues")
        self.assertEqual(navigateur._adresse("/autre"), "http://localhost:8888/autre")
        for refusee in ("https://example.com/", "http://localhost:9999/", "https://localhost:8888/"):
            with self.assertRaises(ecran.EcranErreur):
                navigateur._adresse(refusee)

    def test_sans_configuration_adresse_locale_seulement(self):
        navigateur = ecran.Navigateur(self.profil())
        self.assertEqual(navigateur._adresse("http://localhost:8888/"), "http://localhost:8888/")
        self.assertEqual(navigateur._adresse("127.0.0.1:8080/x"), "http://127.0.0.1:8080/x")
        for refusee in ("https://example.com/", "http://recette.client.fr/", ""):
            with self.assertRaises(ecran.EcranErreur):
                navigateur._adresse(refusee)


class TestConfigurationEtConsignes(AvecDossierTemporaire):
    def test_lecture_du_profil(self):
        profil = config._profil("essai", {
            "sources": [{"chemin": str(self.source)}],
            "application": {"url": "http://localhost:8888/", "utilisateur": "testeur", "actions": True}}, True)
        self.assertEqual((profil.application.url, profil.application.actions), ("http://localhost:8888/", True))
        self.assertIsNone(config._profil("essai", {"sources": [{"chemin": str(self.source)}]}, True).application)
        self.assertEqual(config.nom_mot_de_passe_application("recette-x"), "APP_PASSWORD_RECETTE_X")

    def test_marche_a_suivre(self):
        avec = consignes.documentation([], 20, "http://localhost:8888/ ; navigation seule", False)
        self.assertIn("ecran_capturer", avec)
        self.assertIn("Évite de capturer", avec)
        self.assertIn("Base de test déclarée", consignes.documentation([], 20, "x", True))
        sans = consignes.documentation([], 20)
        self.assertNotIn("ecran_", sans)
        self.assertIn("aucune capture", sans)

    def test_outils_exigent_une_documentation(self):
        with mock.patch.object(ecran, "disponible", return_value=True):
            session = Session(self.profil(application=APPLICATION), [("standard", self.source)], [],
                              config.Reglages(), "essai")
            outils = {t.name: t.handler for t in session._outils_maison()}
        for nom in ("ecran_ouvrir", "ecran_observer", "ecran_cliquer", "ecran_saisir", "ecran_capturer"):
            self.assertIn(f"mcp__odatio__{nom}", session.options.allowed_tools)
        reponse = asyncio.run(outils["ecran_ouvrir"]({}))
        self.assertTrue(reponse["is_error"])
        self.assertIsNone(session.navigateur)

    def test_sans_playwright_pas_d_outil(self):
        with mock.patch.object(ecran, "disponible", return_value=False):
            session = Session(self.profil(), [("standard", self.source)], [], config.Reglages(), "essai")
        self.assertFalse([o for o in session.options.allowed_tools if "ecran_" in o])


class TestVoix(AvecDossierTemporaire):
    def reglage(self, **valeurs):
        with mock.patch.dict(config._SECRETS, valeurs, clear=True):
            return video._reglage()

    def test_reglages(self):
        self.assertEqual(self.reglage(), ("auto", ""))
        self.assertEqual(self.reglage(DOCUMENTATION_VOIX="edge", DOCUMENTATION_VOIX_NOM="fr-FR-HenriNeural"),
                         ("edge", "fr-FR-HenriNeural"))
        self.assertEqual(self.reglage(DOCUMENTATION_VOIX="Hortense"), ("windows", "Hortense"))  # ancien réglage
        self.assertEqual(self.reglage(DOCUMENTATION_VOIX="non"), ("aucune", ""))

    def test_adresse_du_modele_piper(self):
        self.assertEqual(video._url_piper("fr_FR-siwis-medium"),
                         "https://huggingface.co/rhasspy/piper-voices/resolve/main/fr/fr_FR/siwis/medium/"
                         "fr_FR-siwis-medium.onnx")

    def test_edge_jamais_choisi_automatiquement(self):
        essayes = []
        with mock.patch.dict(config._SECRETS, {}, clear=True), \
                mock.patch.object(video, "_disponible", side_effect=lambda m, n, j, *_: essayes.append(m)):
            video.oublier_moteur()
            self.assertEqual(video.moteur_voix(), ("", ""))
        video.oublier_moteur()
        self.assertNotIn("edge", essayes)
