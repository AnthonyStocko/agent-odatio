"""Sources d'un profil : projet Bitbucket entier développé en ses dépôts (Git et Bitbucket simulés)."""
import unittest
from unittest import mock

from odatio_agent import bitbucket, config, sources
from tests.outillage import AvecDossierTemporaire

PROJET = "https://git.exemple.test/projects/CLIENTX"


class AdresseDeProjet(unittest.TestCase):
    def test_reconnue(self):
        self.assertEqual(bitbucket.projet_depuis_url(PROJET), "CLIENTX")
        self.assertEqual(bitbucket.projet_depuis_url(PROJET.lower() + "/"), "CLIENTX")
        self.assertEqual(bitbucket.projet_depuis_url(PROJET + "?tab=repos"), "CLIENTX")

    def test_adresse_de_depot_non_concernee(self):
        for url in (PROJET + "/repos/app/browse", "https://git.exemple.test/scm/clientx/app.git", "", "C:/dossier"):
            self.assertEqual(bitbucket.projet_depuis_url(url), "", url)

    def test_adresse_de_clonage_sans_identifiant(self):
        page = {"values": [{"slug": "b", "links": {"clone": [
                    {"name": "ssh", "href": "ssh://git@git.exemple.test:7999/clientx/b.git"},
                    {"name": "http", "href": "https://jdupont@git.exemple.test/scm/clientx/b.git"}]}},
                {"slug": "a", "links": {}}], "isLastPage": True}
        with mock.patch.object(bitbucket, "_get", return_value=page), \
                mock.patch.object(bitbucket, "adresse", return_value="https://git.exemple.test"):
            self.assertEqual(bitbucket.depots_du_projet(None, "clientx"), [
                ("a", "https://git.exemple.test/scm/clientx/a.git"),
                ("b", "https://git.exemple.test/scm/clientx/b.git")])

    def test_projet_non_autorise(self):
        with mock.patch.object(bitbucket, "projets_autorises", return_value={"AUTRE"}):
            with self.assertRaises(bitbucket.BitbucketErreur):
                bitbucket.depots_du_projet(None, "CLIENTX")


class SourceProjet(AvecDossierTemporaire):
    def setUp(self):
        super().setUp()
        self.depots = [("app", "https://git.exemple.test/scm/clientx/app.git"),
                       ("studio", "https://git.exemple.test/scm/clientx/studio.git"),
                       ("vide", "https://git.exemple.test/scm/clientx/vide.git")]
        self.appels = []
        patch = mock.patch.object(bitbucket, "depots_du_projet", side_effect=lambda p, projet: list(self.depots))
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(sources, "_git", side_effect=self._git)
        patch.start()
        self.addCleanup(patch.stop)

    def _git(self, args, cwd=None):
        self.appels.append(args)
        if args[0] == "ls-remote":
            return "" if args[-1].endswith("vide.git") else (
                "abc\trefs/heads/master\n" + ("def\trefs/heads/dev\n" if "app" in args[-1] else ""))
        if args[0] == "clone":
            (config.CACHE.parent / args[-1]).joinpath(".git").mkdir(parents=True)
        if args[0] == "rev-parse":
            return "master\n"
        return ""

    def profil_projet(self, branche=""):
        return self.profil(sources=[config.Source(nom="client", depot=PROJET, branche=branche)])

    def test_tous_les_depots_clones_dans_la_source(self):
        prets = sources.preparer(self.profil_projet(), journal=lambda _: None)
        dossier = self.cache / "sources" / "essai" / "client"
        self.assertEqual(prets, [("client", dossier.resolve())])
        self.assertTrue((dossier / "app" / ".git").is_dir())
        self.assertTrue((dossier / "studio" / ".git").is_dir())
        self.assertFalse((dossier / "vide").exists())     # dépôt vide ignoré

    def test_branche_appliquee_aux_depots_qui_l_ont(self):
        messages = []
        sources.preparer(self.profil_projet("dev"), journal=messages.append)
        clones = {a[-1].replace("\\", "/").rsplit("/", 1)[-1]: a for a in self.appels if a[0] == "clone"}
        self.assertIn("--branch", clones["app"])
        self.assertNotIn("--branch", clones["studio"])
        self.assertTrue(any("studio" in m and "branche par défaut" in m for m in messages))

    def test_sans_mise_a_jour_aucun_appel_reseau(self):
        sources.preparer(self.profil_projet(), journal=lambda _: None)
        self.appels.clear()
        bitbucket.depots_du_projet.reset_mock()
        sources.preparer(self.profil_projet(), mettre_a_jour=False, journal=lambda _: None)
        self.assertEqual(self.appels, [])
        bitbucket.depots_du_projet.assert_not_called()

    def test_mise_a_jour_et_nouveau_depot(self):
        sources.preparer(self.profil_projet(), journal=lambda _: None)
        self.depots.append(("nouveau", "https://git.exemple.test/scm/clientx/nouveau.git"))
        self.appels.clear()
        sources.preparer(self.profil_projet(), journal=lambda _: None)
        self.assertEqual(sum(a[0] == "fetch" for a in self.appels), 2)          # app, studio
        self.assertEqual([a for a in self.appels if a[0] == "clone"][0][-1].replace("\\", "/").rsplit("/", 1)[-1],
                         "nouveau")

    def test_un_depot_en_echec_n_arrete_pas_les_autres(self):
        original = self._git

        def git(args, cwd=None):
            if args[0] == "clone" and args[-1].replace("\\", "/").endswith("/studio"):
                raise sources.SourceError("git clone a échoué : refusé")
            return original(args, cwd)
        sources._git.side_effect = git
        messages = []
        sources.preparer(self.profil_projet(), journal=messages.append)
        self.assertTrue(any("studio" in m and "non récupéré" in m for m in messages))

    def test_aucun_depot_recupere(self):
        self.depots[:] = [("vide", "https://git.exemple.test/scm/clientx/vide.git")]
        with self.assertRaises(sources.SourceError):
            sources.preparer(self.profil_projet(), journal=lambda _: None)

    def test_erreur_bitbucket_rendue_en_erreur_de_source(self):
        bitbucket.depots_du_projet.side_effect = bitbucket.BitbucketErreur("HTTP 403")
        with self.assertRaisesRegex(sources.SourceError, "CLIENTX"):
            sources.preparer(self.profil_projet(), journal=lambda _: None)

    def test_depot_seul_inchange(self):
        profil = self.profil(sources=[config.Source(nom="app", depot="https://git.exemple.test/scm/clientx/app.git")])
        prets = sources.preparer(profil, journal=lambda _: None)
        self.assertEqual(prets, [("app", (self.cache / "sources" / "essai" / "app").resolve())])
        self.assertEqual([a[0] for a in self.appels], ["clone"])
        bitbucket.depots_du_projet.assert_not_called()


if __name__ == "__main__":
    unittest.main()
