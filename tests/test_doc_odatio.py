"""Documentation Odatio lue sur Bitbucket : chemins bornés, pages publiées ramenées à leur source, outils déclarés
seulement si la doc est configurée, jamais en synthèse. Aucun appel réseau : bitbucket._get / _pages simulés."""
from unittest import mock

from odatio_agent import bitbucket, config, consignes, doc_odatio, outils, session

from .outillage import AvecDossierTemporaire

SECRETS = {"BITBUCKET_URL": "https://git.exemple.io", "BITBUCKET_HTTP_TOKEN": "jeton",
           "ODATIO_DOC_DEPOT": "DOC/odatio-docs", "ODATIO_DOC_BRANCHE": "master", "ODATIO_DOC_DOSSIER": "docs"}
FICHIERS = ["index.rst", "development/index.rst", "development/api.md", "user/stock/inventaire.rst"]
OUTILS = ["doc_sommaire", "doc_lire", "doc_chercher"]


class AvecDoc(AvecDossierTemporaire):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(config._SECRETS, SECRETS, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.appels = []

        def pages(profil, chemin, params=None, maximum=500, taille=100, get=None):
            self.appels.append((chemin, params))
            return list(FICHIERS) if chemin.endswith("/files/docs") else []

        def get(profil, chemin, params=None, **kwargs):
            self.appels.append((chemin, params))
            return {"lines": [{"text": "Développement"}], "isLastPage": True}

        for nom, faux in (("_pages", pages), ("_get", get)):
            p = mock.patch.object(bitbucket, nom, side_effect=faux)
            p.start()
            self.addCleanup(p.stop)


class TestDocumentation(AvecDoc):
    def test_page_publiee_ramenee_a_sa_source(self):
        profil = self.profil()
        for chemin in ("development/index.rst", "docs/development/index.rst", "development/index.html",
                       "development", "https://site.exemple/doc/odatio-docs/development/index.html#titre"):
            with self.subTest(chemin):
                self.assertEqual(doc_odatio.resoudre(profil, chemin), "docs/development/index.rst")

    def test_lire_sur_la_branche_de_la_doc(self):
        texte = outils.doc_lire(self.profil(), "development/index.html", 1, 50)
        self.assertIn("Documentation Odatio — DOC/odatio-docs @ master : docs/development/index.rst", texte)
        chemin, params = self.appels[-1]
        self.assertTrue(chemin.endswith("/projects/DOC/repos/odatio-docs/browse/docs/development/index.rst"))
        self.assertEqual(params["at"], "refs/heads/master")

    def test_chemins_refuses(self):
        for chemin in ("../config/.env", "/etc/passwd", "C:/Windows/win.ini", "absent.html"):
            with self.subTest(chemin), self.assertRaises(outils.OutilErreur):
                outils.doc_lire(self.profil(), chemin)

    def test_projet_non_autorise(self):
        with mock.patch.dict(config._SECRETS, {"BITBUCKET_PROJETS": "STD"}), \
                self.assertRaises(outils.OutilErreur) as e:
            outils.doc_sommaire(self.profil())
        self.assertIn("non autorisé", str(e.exception))

    def test_non_configuree(self):
        with mock.patch.dict(config._SECRETS, {"ODATIO_DOC_DEPOT": ""}):
            self.assertFalse(doc_odatio.configuree(self.profil()))
            with self.assertRaises(outils.OutilErreur):
                outils.doc_sommaire(self.profil())


class TestAntora(AvecDoc):
    """Site Antora : …/<composant>/[<version>/][<module>/]<page>.html vient de modules/<module>/pages/<page>.adoc."""
    def setUp(self):
        super().setUp()
        self.fichiers = ["antora.yml", "modules/ROOT/pages/index.adoc", "modules/ROOT/pages/guide/stock.adoc",
                         "modules/development/pages/index.adoc", "modules/development/pages/api/rest.adoc"]
        p = mock.patch.object(doc_odatio, "_fichiers", side_effect=lambda *a: list(self.fichiers))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.dict(config._SECRETS, {"ODATIO_DOC_DEPOT": "COMPONENT/odatio-docs", "ODATIO_DOC_DOSSIER": ""})
        p.start()
        self.addCleanup(p.stop)

    def test_pages_publiees(self):
        cas = {"https://site.exemple/doc/odatio-docs/development/index.html": "modules/development/pages/index.adoc",
               "development/api/rest.html": "modules/development/pages/api/rest.adoc",
               "https://site.exemple/doc/odatio-docs/index.html": "modules/ROOT/pages/index.adoc",
               "https://site.exemple/doc/odatio-docs/1.0/guide/stock.html": "modules/ROOT/pages/guide/stock.adoc",
               "modules/ROOT/pages/index.adoc": "modules/ROOT/pages/index.adoc"}
        for chemin, attendu in cas.items():
            with self.subTest(chemin):
                self.assertEqual(doc_odatio.resoudre(self.profil(), chemin), attendu)

    def test_version_dans_l_adresse(self):
        self.fichiers = ["modules/ROOT/pages/index.adoc"]                 # « development » : une version
        self.assertEqual(doc_odatio.resoudre(self.profil(), "development/index.html"), "modules/ROOT/pages/index.adoc")


class TestSession(AvecDoc):
    def noms(self, **options) -> tuple:
        s = session.Session(self.profil(), [("standard", str(self.source))], [], config.Reglages(), **options)
        return [o.name for o in s._outils_maison()], s.options.allowed_tools, s.options.system_prompt

    def test_outils_et_consigne(self):
        for options in ({}, {"lecture_seule": True}):           # IHM et question du manager
            with self.subTest(**options):
                noms, autorises, prompt = self.noms(**options)
                for nom in OUTILS:
                    self.assertIn(nom, noms)
                    self.assertIn(f"mcp__{session.SERVEUR}__{nom}", autorises)
                self.assertIn("Documentation Odatio (produit Standard, lecture seule)", prompt)

    def test_ni_sans_configuration_ni_en_synthese(self):
        with mock.patch.dict(config._SECRETS, {"ODATIO_DOC_DEPOT": ""}):
            noms, _, prompt = self.noms()
        self.assertFalse(set(noms) & set(OUTILS))
        self.assertNotIn("doc_lire", prompt)
        texte = consignes.construire(self.profil(), [], [], "", config.Reglages(), synthese=True)
        self.assertNotIn("doc_lire", texte)
