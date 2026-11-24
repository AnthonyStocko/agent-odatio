"""Tests de l'inventaire de la synthèse client lu sur Bitbucket (synthese.inventorier_bitbucket), avec un faux
serveur Bitbucket : réponses simulées, données fictives (projet client « CLIENT-X », Standard « COMPONENT-X »).
Aucun appel réseau, aucun clonage."""
import asyncio
import json
from unittest import mock
from urllib.parse import unquote, urlsplit

import requests

from odatio_agent import bitbucket, config, consignes, synthese as synth

from .outillage import AvecDossierTemporaire
from .test_synthese import FausseSession

HOTE = "https://git.exemple.test"
POM_STD = """<project><groupId>com.exemple.odatio</groupId><artifactId>odatio-parent</artifactId>
<version>26.22-SNAPSHOT</version></project>"""
POM_CLIENT = """<project xmlns="http://maven.apache.org/POM/4.0.0">
<parent><groupId>com.exemple.odatio</groupId><artifactId>odatio-parent</artifactId><version>${standard.version}</version></parent>
<groupId>com.clientx</groupId><artifactId>client-x</artifactId>
<properties><standard.version>26.21.3</standard.version></properties></project>"""

# Standard : un dépôt « wms-content » (branche de la version v26.21), un dépôt de classes sans branche v26.21,
# un dépôt en panne (HTTP 500), un dépôt vide (HTTP 204).
STANDARD = {
    "wms-content": {"defaut": "development", "branches": {
        "development": {"pom.xml": POM_STD},
        "v26.21": {
            "pom.xml": POM_STD,
            "core/src/main/java/com/exemple/content/Identique.java": "class Identique {}\n",
            "core/src/main/java/com/exemple/content/Modifie.java": "class Modifie { int a; }\n",
        }}},
    "wms-preparation": {"defaut": "development", "branches": {"development": {
        "pom.xml": POM_STD,
        "core/src/main/java/com/exemple/wave/WaveService.java": "class WaveService {}\n",
    }}},
    "en-panne": {"panne": True},
    "vide": {"vide": True},
}
# Spécifique : copie de « wms-content » (même dépôt) et un dépôt propre au client.
CLIENT = {
    "wms-content": {"defaut": "integration", "branches": {"integration": {
        "pom.xml": POM_CLIENT,
        "core/src/main/java/com/exemple/content/Identique.java": "class Identique {}\r\n",   # CRLF : identique
        "core/src/main/java/com/exemple/content/Modifie.java": "// ODA-7\nclass Modifie { int b; }\n",
        "README.md": "# client-x",
        "target/classes/Genere.java": "class Genere {}\n",
    }}},
    "odatio-client-x": {"defaut": "integration", "branches": {"integration": {
        "client-x-core/src/main/java/com/clientx/wave/WaveService.java": "// ODA-12 : par zone\nclass WaveService {}\n",
        "client-x-core/src/main/java/com/clientx/expedition/ShippingRules.java": "// ODA-12\nclass ShippingRules {}\n",
        "client-x-core/src/main/resources/params/client-param.xml": "<param code='ZONE' valeur='1'/>\n",
        "studio/process/liberation/customized.xml": "<process id='liberation' zone='true'/>\n",
        "studio/process/liberation/original.xml": "<process id='liberation'/>\n",
        "config/secrets.properties": "jeton=ne-doit-jamais-etre-lu\n",
        "sql/V1__zones.sql": "CREATE TABLE ZONE_CLIENT (CODE VARCHAR(10));\n",
    }}},
}


class Reponse:
    def __init__(self, statut: int, corps=None, octets: bytes = b""):
        self.status_code, self._corps, self.octets = statut, corps, octets
        self.headers = {}
        self.content = octets or (json.dumps(corps).encode() if corps is not None else b"")

    def json(self):
        if self._corps is None:
            raise ValueError("pas de JSON")
        return self._corps

    def iter_content(self, taille):
        for i in range(0, len(self.octets), taille):
            yield self.octets[i:i + taille]

    def close(self):
        pass


class FauxBitbucket:
    """Faux Bitbucket Server (API REST 1.0) : pages de 3 valeurs au plus, quelle que soit la limite demandée."""
    PAGE = 3

    def __init__(self):
        self.projets = {"COMPONENT-X": STANDARD, "CLIENT-X": CLIENT}
        self.appels = []

    def page(self, valeurs: list, params: dict):
        debut = int(params.get("start", 0))
        taille = min(int(params.get("limit", 25)), self.PAGE)
        bout = valeurs[debut:debut + taille]
        fin = debut + len(bout) >= len(valeurs)
        return Reponse(200, {"values": bout, "isLastPage": fin, "nextPageStart": None if fin else debut + len(bout)})

    def get(self, url, params=None, timeout=None, headers=None, stream=False, **_):
        assert headers["Authorization"] == "Bearer jeton-factice" and timeout
        params = params or {}
        chemin = unquote(urlsplit(url).path)
        self.appels.append((chemin, dict(params)))
        parties = chemin.split("/")       # '', rest, api, 1.0, projects, P, repos, r, ...
        projet = self.projets.get(parties[5]) if len(parties) > 5 else None
        if projet is None:
            return Reponse(404, {"errors": [{"message": "Projet introuvable"}]})
        if len(parties) == 7:
            return self.page([{"slug": s} for s in sorted(projet)], params)
        depot = projet.get(parties[7])
        if depot is None:
            return Reponse(404, {"errors": [{"message": "Dépôt introuvable"}]})
        if depot.get("panne"):
            return Reponse(500, {"errors": [{"message": "Erreur interne"}]})
        if depot.get("vide"):
            return Reponse(204) if parties[8:] == ["branches", "default"] else Reponse(404, {"errors": []})
        reste = parties[8:]
        ref = params.get("at") or params.get("until") or f"refs/heads/{depot['defaut']}"
        branche = depot["branches"].get(ref.replace("refs/heads/", ""))
        if reste == ["branches", "default"]:
            return Reponse(200, {"displayId": depot["defaut"]})
        if branche is None:
            return Reponse(404, {"errors": [{"message": f"Commit '{ref}' does not exist in repository."}]})
        if reste == ["commits"]:
            return Reponse(200, {"values": [{"id": "a1b2c3d4e5f60718", "committerTimestamp": 1793031289000}],
                                 "isLastPage": False})
        if reste == ["files"]:
            return self.page(sorted(branche), params)
        if reste[0] == "raw":
            contenu = branche.get("/".join(reste[1:]))
            if contenu is None:
                return Reponse(404, {"errors": [{"message": "Fichier introuvable"}]})
            return Reponse(200, octets=contenu.encode("utf-8"))
        return Reponse(404, {"errors": [{"message": "Ressource inconnue"}]})


class FausseSessionHttp:
    def __init__(self, serveur):
        self.serveur = serveur

    def get(self, *args, **kwargs):
        return self.serveur.get(*args, **kwargs)

    def close(self):
        pass


class AvecFauxBitbucket(AvecDossierTemporaire):
    def setUp(self):
        super().setUp()
        self.serveur = FauxBitbucket()
        secrets = {"BITBUCKET_HTTP_TOKEN": "jeton-factice", "BITBUCKET_URL": HOTE}
        for patch in (mock.patch.object(config, "secret", side_effect=lambda nom: secrets.get(nom, "")),
                      mock.patch.object(requests, "Session", side_effect=lambda: FausseSessionHttp(self.serveur)),
                      mock.patch.object(requests, "get", side_effect=self.serveur.get),
                      mock.patch.object(requests, "post", side_effect=AssertionError("aucun POST attendu"))):
            patch.start()
            self.addCleanup(patch.stop)

    def profil_bb(self, branche_standard: str = "") -> config.Profil:
        return self.profil(sources=[
            config.Source(nom="client-x", depot=f"{HOTE}/projects/CLIENT-X"),
            config.Source(nom="Standard", depot=f"{HOTE}/projects/COMPONENT-X", branche=branche_standard)],
            fichiers_interdits=list(config.INTERDITS_PAR_DEFAUT))

    def inventorier(self, profil=None, **reglages) -> dict:
        self.etapes = []
        return synth.inventorier_bitbucket(profil or self.profil_bb(), "Standard", config.Reglages(**reglages),
                                           self.etapes.append)


def par_fichier(inv: dict) -> dict:
    return {(e.get("depot"), f["chemin"]): (e, f) for e in inv["elements"] for f in e["fichiers"]}


class TestInventaireBitbucket(AvecFauxBitbucket):
    def test_classement(self):
        inv = self.inventorier()
        fichiers = par_fichier(inv)
        # même dépôt, même chemin, contenu identique aux fins de ligne près : ignoré
        self.assertNotIn(("CLIENT-X/wms-content", "core/src/main/java/com/exemple/content/Identique.java"), fichiers)
        self.assertEqual(inv["fichiers_identiques"], 1)
        # même dépôt, même chemin, contenu différent : surcharge, Standard lu sur la branche de la version
        e, f = fichiers[("CLIENT-X/wms-content", "core/src/main/java/com/exemple/content/Modifie.java")]
        self.assertEqual((f["type"], e["nature"]), ("surcharge", "surcharge"))
        self.assertEqual(f["standard"], "COMPONENT-X/wms-content:core/src/main/java/com/exemple/content/Modifie.java"
                                        " @ v26.21")
        self.assertEqual(e["tickets"], ["ODA-7"])
        # même nom de classe sous src/main dans un dépôt du Standard : surcharge
        _, f = fichiers[("CLIENT-X/odatio-client-x", "client-x-core/src/main/java/com/clientx/wave/WaveService.java")]
        self.assertEqual(f["type"], "surcharge")
        self.assertTrue(f["standard"].startswith("COMPONENT-X/wms-preparation:core/src/main/java/com/exemple/wave/"))
        # Studio, ajout, paramétrage, script SQL
        e, f = fichiers[("CLIENT-X/odatio-client-x", "studio/process/liberation/customized.xml")]
        self.assertEqual((f["type"], e["categorie"]), ("surcharge", "processus Studio"))
        e, f = fichiers[("CLIENT-X/odatio-client-x",
                         "client-x-core/src/main/java/com/clientx/expedition/ShippingRules.java")]
        self.assertEqual((f["type"], e["tickets"]), ("ajout", ["ODA-12"]))
        e, _ = fichiers[("CLIENT-X/odatio-client-x", "client-x-core/src/main/resources/params/client-param.xml")]
        self.assertEqual(e["nature"], "paramétrage")
        self.assertIn("script SQL", {e["categorie"] for e in inv["elements"]})
        # mêmes exclusions que l'inventaire local ; fichiers interdits jamais lus
        chemins = {c for _, c in fichiers}
        for absent in ("README.md", "pom.xml", "target/classes/Genere.java", "config/secrets.properties",
                       "studio/process/liberation/original.xml"):
            self.assertNotIn(absent, chemins)
        self.assertFalse(any("secrets.properties" in c for c, _ in self.serveur.appels))
        # chaque élément indique son dépôt et sa branche
        self.assertTrue(all(e["depot"].startswith("CLIENT-X/") and e["branche"] == "integration"
                            for e in inv["elements"]))
        self.assertEqual(len({e["id"] for e in inv["elements"]}), len(inv["elements"]))

    def test_version_branche_commits_et_limites(self):
        inv = self.inventorier()
        v = inv["version"]
        self.assertEqual((v["version"], v["complete"], v["branche_deduite"]), ("26.21", "26.21.3", "v26.21"))
        self.assertEqual(v["source"], "CLIENT-X/wms-content:pom.xml (propriété standard.version)")
        depots_std = {d["depot"]: d for d in inv["standard"]["depots"]}
        self.assertEqual(depots_std["COMPONENT-X/wms-content"]["branche"], "v26.21")
        self.assertEqual(depots_std["COMPONENT-X/wms-preparation"]["branche"], "development")   # repli
        self.assertEqual(depots_std["COMPONENT-X/wms-content"]["commit"], "a1b2c3d4e5f6")
        self.assertIn("erreur", depots_std["COMPONENT-X/en-panne"])
        self.assertEqual(depots_std["COMPONENT-X/vide"]["erreur"], "dépôt vide")
        limites = "\n".join(inv["limites"])
        self.assertIn("sans branche « v26.21 »", limites)                          # repli signalé
        self.assertIn("COMPONENT-X/en-panne : non inventorié", limites)             # dépôt en erreur toléré
        self.assertNotIn("COMPONENT-X/vide", limites)                               # dépôt vide : rien à lire
        self.assertIn("interdit", limites)
        self.assertEqual(inv["specifiques"][0]["bitbucket"], "projet CLIENT-X")
        self.assertEqual(inv["bitbucket"]["appels"], len(self.serveur.appels))
        self.assertTrue(any("Inventaire Bitbucket terminé" in e for e in self.etapes))  # progression

    def test_listing_pagine(self):
        inv = self.inventorier()
        pages = [p for c, p in self.serveur.appels if c.endswith("/odatio-client-x/files")]
        self.assertGreater(len(pages), 2)
        self.assertEqual([p["start"] for p in pages], list(range(0, 3 * len(pages), 3)))
        self.assertEqual(len([1 for d, _ in par_fichier(inv) if d == "CLIENT-X/odatio-client-x"]), 5)

    def test_branche_du_standard_donnee_par_la_source(self):
        inv = self.inventorier(self.profil_bb(branche_standard="development"))
        self.assertNotIn("branche_deduite", inv["version"])
        # sur development, Modifie.java n'existe pas dans le Standard : ajout
        _, f = par_fichier(inv)[("CLIENT-X/wms-content", "core/src/main/java/com/exemple/content/Modifie.java")]
        self.assertEqual(f["type"], "ajout")

    def test_plafond_de_comparaisons(self):
        inv = self.inventorier(synthese_comparaisons=1)
        self.assertIn("Plafond de 1 fichiers comparés atteint : 1 fichier(s)", "\n".join(inv["limites"]))
        self.assertEqual(inv["bitbucket"]["comparaisons"], 1)

    def test_plafond_de_lectures(self):
        inv = self.inventorier(synthese_lectures=0)
        self.assertIn("Plafond de 0 fichiers lus atteint", "\n".join(inv["limites"]))
        tickets = {t for e in inv["elements"] for t in e["tickets"]}
        self.assertEqual(tickets, {"ODA-7"})                   # seul le fichier comparé a été lu

    def test_plafond_de_depots(self):
        inv = self.inventorier(synthese_depots=3)
        limites = "\n".join(inv["limites"])
        self.assertIn("Plafond de 3 dépôts atteint : 3 dépôt(s)", limites)
        self.assertEqual(len(inv["specifiques"][0]["depots"]), 2)          # spécifique parcouru en premier

    def test_erreurs_de_configuration(self):
        with mock.patch.object(bitbucket, "configure", return_value=False):
            with self.assertRaisesRegex(synth.SyntheseErreur, "pas configuré"):
                self.inventorier()
        seul_standard = self.profil(sources=[config.Source(nom="Standard", depot=f"{HOTE}/projects/COMPONENT-X")])
        with self.assertRaisesRegex(synth.SyntheseErreur, "aucune source spécifique"):
            self.inventorier(seul_standard)

    def test_standard_local_et_specifique_sur_bitbucket(self):
        (self.source / "pom.xml").write_text(POM_STD, encoding="utf-8")
        modifie = self.source / "wms-content" / "core/src/main/java/com/exemple/content/Modifie.java"
        modifie.parent.mkdir(parents=True)
        modifie.write_text("// ODA-7\nclass Modifie { int b; }\n", encoding="utf-8")      # identique au client
        profil = self.profil(sources=[config.Source(nom="client-x", depot=f"{HOTE}/projects/CLIENT-X"),
                                      config.Source(nom="Standard", chemin=str(self.source))])
        inv = self.inventorier(profil)
        self.assertNotIn(("CLIENT-X/wms-content", "core/src/main/java/com/exemple/content/Modifie.java"),
                         par_fichier(inv))
        self.assertEqual(inv["standard"], {"source": "Standard"})
        self.assertEqual(inv["version"]["version"], "26.21")


class TestSyntheseBitbucket(AvecFauxBitbucket):
    def test_aucune_ecriture_hors_synthese_et_rapport(self):
        inv = self.inventorier()
        self.assertFalse(self.cache.exists())                 # l'inventaire n'écrit rien, ne clone rien
        dossier = synth.creer("client-x-recette", inv, 4)
        texte = synth.donnees_lot(dossier, 1)
        self.assertIn("dépôt Bitbucket CLIENT-X/", texte)
        self.assertIn("branche integration", texte)
        self.assertIn("@ v26.21", synth.donnees_lot(dossier, len(synth.etat(dossier)["lots"]))
                      + "".join(synth.donnees_lot(dossier, n) for n in range(1, len(synth.etat(dossier)["lots"]))))
        e = asyncio.run(synth.executer(dossier, lambda mode, numero: FausseSession(dossier, mode, numero)))
        self.assertTrue(e["termine"])
        rapport = (dossier / "rapport.md").read_text(encoding="utf-8")
        self.assertEqual(synth.valider_rapport(rapport), [])
        self.assertIn("Bitbucket, projet CLIENT-X, branche integration", rapport)
        self.assertIn("Inventaire lu sur Bitbucket, sans clonage", rapport)
        self.assertIn("Limite de l'inventaire : Standard : 1 dépôt(s) sans branche", rapport)
        self.assertIn("Standard lu sur la branche v26.21, déduite de cette version", rapport)
        self.assertEqual({p.name for p in self.cache.iterdir()}, {"synthese"})
        self.assertIn("Sources lues sur Bitbucket", synth.donnees_finale(dossier))
        self.assertEqual(json.loads((dossier / "inventaire.json").read_text(encoding="utf-8"))["bitbucket"]["appels"],
                         inv["bitbucket"]["appels"])

    def test_session_d_un_lot_sans_source_locale(self):
        from odatio_agent.session import Session
        dossier = synth.creer("client-x-recette", self.inventorier(), 4)
        s = Session(self.profil_bb(), [], [], config.Reglages(),
                    synthese={"mode": "lot", "numero": 1, "dossier": str(dossier)})
        self.assertEqual(s.options.cwd, str(config.dossier_fichiers(self.profil_bb())))
        noms = {o.name for o in s._outils_maison()}
        self.assertTrue({"bitbucket_lire", "bitbucket_fichiers", "synthese_demarrer"} <= noms)
        self.assertNotIn("fichier_ecrire", noms)

    def test_consigne_du_lot(self):
        texte = consignes.synthese("lot", "(données)", 5)
        self.assertIn("bitbucket_lire", texte)
        self.assertIn("« dépôt:chemin:ligne »", texte)


class TestContenuBrut(AvecFauxBitbucket):
    def test_interdit_refuse_et_lecture_bornee(self):
        profil = self.profil_bb()
        with self.assertRaisesRegex(bitbucket.BitbucketErreur, "refusée"):
            bitbucket.contenu_brut(profil, "CLIENT-X", "odatio-client-x", "config/secrets.properties", "integration")
        self.assertEqual(self.serveur.appels, [])
        octets, tronque = bitbucket.contenu_brut(profil, "CLIENT-X", "odatio-client-x", "sql/V1__zones.sql",
                                                 "integration", maximum=6)
        self.assertEqual((octets, tronque), (b"CREATE", True))
