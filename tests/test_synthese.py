"""Tests de la synthèse client (odatio_agent.synthese, outils de la session, contrôle du rapport).
Données fictives uniquement : un faux Standard et un faux dépôt spécifique « client-x »."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
from unittest import mock

from odatio_agent import bitbucket, config, consignes, synthese as synth
from odatio_agent.session import Session

from .outillage import AvecDossierTemporaire

POM_STD = """<project><groupId>com.exemple.odatio</groupId><artifactId>odatio-parent</artifactId>
<version>26.22-SNAPSHOT</version></project>"""
POM_CLIENT = """<project xmlns="http://maven.apache.org/POM/4.0.0">
<parent><groupId>com.exemple.odatio</groupId><artifactId>odatio-parent</artifactId><version>${standard.version}</version></parent>
<groupId>com.clientx</groupId><artifactId>client-x</artifactId>
<properties><standard.version>26.21.3</standard.version></properties></project>"""


def ecrire(racine: Path, fichiers: dict) -> None:
    for chemin, contenu in fichiers.items():
        (racine / chemin).parent.mkdir(parents=True, exist_ok=True)
        (racine / chemin).write_text(contenu, encoding="utf-8")


def empreinte(racine: Path) -> str:
    h = hashlib.sha1()
    for f in sorted(p for p in racine.rglob("*") if p.is_file()):
        h.update(str(f.relative_to(racine)).encode() + f.read_bytes())
    return h.hexdigest()


class AvecProjet(AvecDossierTemporaire):
    def setUp(self):
        super().setUp()
        ecrire(self.source, {
            "pom.xml": POM_STD,
            "wms-preparation/src/main/java/com/exemple/odatio/wave/WaveService.java": "class WaveService {}\n",
            "wms-core/src/main/resources/config/application.properties": "vague.max=10\n",
        })
        self.client = self.racine / "sources" / "client-x"
        ecrire(self.client, {
            "pom.xml": POM_CLIENT,
            "README.md": "# client-x",
            # même nom de classe que le Standard : surcharge ; ticket cité ; UTF-8 n'est pas un ticket
            "client-x-core/src/main/java/com/clientx/wave/WaveService.java":
                "// ODA-12 : libération par zone (encodage UTF-8)\nclass WaveService { int zone; }\n",
            "client-x-core/src/main/java/com/clientx/expedition/ShippingRules.java": "class ShippingRules {}\n",
            "client-x-core/src/main/resources/editions/etiquette-colis.zpl": "^XA^FO50,50^FDColis^FS^XZ\n",
            "client-x-interface-plugin/src/main/java/com/clientx/OrderImport.java": "class OrderImport {}\n",
            "studio/process/liberation/customized.xml": "<process id='liberation' zone='true'/>\n",
            "studio/process/liberation/original.xml": "<process id='liberation'/>\n",
            "client-x-core/src/main/resources/params/client-param.xml": "<param code='ZONE' valeur='1'/>\n",
            "client-x-front/app/vague.html": "<div>Vague</div>\n",
            "sql/V1__zones.sql": "CREATE TABLE ZONE_CLIENT (CODE VARCHAR(10));\n",
            # identique au Standard au même chemin : pas spécifique
            "wms-core/src/main/resources/config/application.properties": "vague.max=10\n",
        })
        self.sources = [("standard", str(self.source)), ("client-x", str(self.client))]

    def inventaire(self) -> dict:
        return synth.inventorier("client-x-recette", self.sources)


class TestInventaire(AvecProjet):
    def test_elements_categories_et_natures(self):
        inv = self.inventaire()
        par_unite = {(e["categorie"], e["unite"]): e for e in inv["elements"]}
        self.assertEqual(inv["fichiers_identiques"], 1)
        self.assertEqual(par_unite[("traitement / règle métier", "wave")]["nature"], "surcharge")
        self.assertIn("wave/WaveService.java", par_unite[("traitement / règle métier", "wave")]["fichiers"][0]
                      ["standard"])
        self.assertEqual(par_unite[("traitement / règle métier", "expedition")]["nature"], "ajout")
        self.assertIn(("édition / étiquette", "editions"), par_unite)
        self.assertIn(("interface / flux", "client-x-interface-plugin"), par_unite)
        self.assertEqual(par_unite[("processus Studio", "studio/process/liberation")]["nature"], "surcharge")
        self.assertEqual(par_unite[("paramétrage", "params")]["nature"], "paramétrage")
        self.assertIn("écran", {e["categorie"] for e in inv["elements"]})
        self.assertIn("script SQL", {e["categorie"] for e in inv["elements"]})
        tous = [f["chemin"] for e in inv["elements"] for f in e["fichiers"]]
        self.assertNotIn("README.md", tous)
        self.assertNotIn("pom.xml", tous)
        self.assertFalse(any(c.endswith("original.xml") for c in tous))
        self.assertEqual(par_unite[("traitement / règle métier", "wave")]["tickets"], ["ODA-12"])

    def test_version_du_standard(self):
        v = self.inventaire()["version"]
        self.assertEqual((v["version"], v["complete"]), ("26.21", "26.21.3"))
        self.assertEqual(v["source"], "client-x/pom.xml (propriété standard.version)")
        self.assertIn("parent", v["detail"])

    def test_sans_standard_ou_sans_specifique(self):
        inv = synth.inventorier("p", [("client-x", str(self.client))])
        self.assertTrue(inv["limites"])
        self.assertTrue(all(e["nature"] != "surcharge" or e["categorie"] == "processus Studio"
                            for e in inv["elements"]))
        with self.assertRaises(synth.SyntheseErreur):
            synth.inventorier("p", [("standard", str(self.source))])

    def test_lots_et_liste(self):
        dossier = synth.creer("client-x-recette", self.inventaire(), 3)
        e = synth.etat(dossier)
        self.assertEqual(sum(len(l["elements"]) for l in e["lots"]), len(self.inventaire()["elements"]))
        self.assertTrue(all(len(l["elements"]) <= 3 for l in e["lots"]))
        self.assertEqual(synth.prochaine_etape(e), ("lot", 1))
        self.assertEqual(synth.lister("client-x-recette")[0]["dossier"], str(dossier))
        self.assertEqual(synth.lister("autre"), [])
        self.assertTrue(str(dossier).startswith(str(self.cache / "synthese")))


def lot_valide(ids: list) -> dict:
    return {"elements": [{"id": i, "objectif": "Libère les vagues par zone de préparation.",
                          "composants": ["client-x-core/src/main/java/com/clientx/wave/WaveService.java"],
                          "ticket": "ODA-12", "sources": ["WaveService.java:2"], "statut": "vérifié"} for i in ids],
            "constats": [{"constat": "Le paramètre ZONE est lu mais absent en base.", "severite": "Moyenne",
                          "preuves": ["params/client-param.xml:1", "table PARAMETRE colonne CODE"],
                          "statut": "Déduit"}],
            "points_a_verifier": [{"point": "Usage réel de la zone", "ou_chercher": "équipe projet"}]}


FINALE = {"contexte": [{"sujet": "Modules utilisés", "constat": "Préparation et expédition.",
                        "sources": ["client-x/pom.xml"], "statut": "vérifié"}],
          "configuration": [{"domaine": "Préparation", "parametre": "vague.max", "valeur": "10",
                             "sources": ["application.properties:1"], "statut": "vérifié"}],
          "audit": [], "points_a_verifier": []}


class TestValidation(AvecProjet):
    def test_lot(self):
        ids = ["e-1", "e-2"]
        self.assertEqual(synth.valider_lot(lot_valide(ids), ids), [])
        cas = {
            "sans réponse": lambda p: p["elements"].pop(),
            "hors de ce lot": lambda p: p["elements"].append({**p["elements"][0], "id": "e-9"}),
            "source manquante": lambda p: p["elements"][0].update(sources=[]),
            "statut": lambda p: p["elements"][0].update(statut="probable"),
            "objectif": lambda p: p["elements"][0].update(objectif=""),
            "e-mail": lambda p: p["elements"][0].update(objectif="Écrire à jean.dupont@client-x.fr"),
            "téléphone": lambda p: p["elements"][0].update(objectif="Appeler le 06 12 34 56 78"),
            "secret": lambda p: p["elements"][0].update(objectif="password=Secret123"),
            "chaîne de connexion": lambda p: p["elements"][0].update(sources=["jdbc:postgresql://hote:5432/base"]),
            "sévérité": lambda p: p["constats"][0].update(severite="critique"),
        }
        for attendu, modifier in cas.items():
            partie = copy.deepcopy(lot_valide(ids))
            modifier(partie)
            self.assertIn(attendu, " ".join(synth.valider_lot(partie, ids)), attendu)
        non = {"elements": [], "non_expliques": [{"id": "e-1", "raison": "fichier vide"}, {"id": "e-2", "raison": ""}]}
        self.assertIn("raison", " ".join(synth.valider_lot(non, ids)))

    def test_finale(self):
        self.assertEqual(synth.valider_finale(FINALE), [])
        partie = copy.deepcopy(FINALE)
        partie["configuration"][0]["statut"] = ""
        partie["points_a_verifier"] = [{"point": "x", "ou_chercher": ""}]
        erreurs = " ".join(synth.valider_finale(partie))
        self.assertIn("statut", erreurs)
        self.assertIn("où chercher", erreurs)
        self.assertTrue(synth.valider_finale({"contexte": [], "configuration": []}))

    def test_rapport(self):
        bon = "\n".join(f"## {i}. {s}" for i, s in enumerate(synth.SECTIONS, start=1))
        bon += "\n\n| Sujet | Source | Statut |\n|---|---|---|\n| a | f.java:1 | vérifié |\n"
        self.assertEqual(synth.valider_rapport(bon), [])
        self.assertIn("Section manquante", " ".join(synth.valider_rapport(bon.replace("## 3.", "## X."))))
        self.assertIn("source manquante", " ".join(synth.valider_rapport(bon.replace("f.java:1", ""))))
        self.assertIn("statut", " ".join(synth.valider_rapport(bon.replace("| vérifié |", "| sûr |"))))
        self.assertIn("téléphone", " ".join(synth.valider_rapport(bon + "\nTél. 06 12 34 56 78\n")))
        self.assertIn("sans colonne Source", " ".join(synth.valider_rapport(bon + "\n| A | B |\n|---|---|\n| 1 | 2 |\n")))


class FausseSession:
    """Simule l'agent : à l'ouverture d'une étape, enregistre la partie comme le ferait synthese_enregistrer."""
    appels: list = []

    def __init__(self, dossier, mode, numero, echec=(), arret=()):
        self.dossier, self.mode, self.numero, self.echec, self.arret = dossier, mode, numero, echec, arret

    async def ouvrir(self):
        pass

    async def fermer(self):
        pass

    async def demander(self, question, sur_texte, sur_outil):
        FausseSession.appels.append((self.mode, self.numero))
        sur_outil("mcp__odatio__synthese_demarrer", {})
        if (self.mode, self.numero) in self.arret:
            raise KeyboardInterrupt("arrêt demandé")      # comme StopException de Streamlit : BaseException
        if (self.mode, self.numero) not in self.echec:
            if self.mode == "lot":
                lot = next(l for l in synth.etat(self.dossier)["lots"] if l["numero"] == self.numero)
                synth.enregistrer_lot(self.dossier, self.numero, lot_valide(lot["elements"]))
            else:
                synth.enregistrer_finale(self.dossier, FINALE)
        return mock.Mock(cout_usd=0.25, message="plafond atteint" if (self.mode, self.numero) in self.echec else "")


class TestDeroule(AvecProjet):
    def setUp(self):
        super().setUp()
        FausseSession.appels = []
        self.avant = empreinte(self.racine / "sources")
        self.dossier = synth.creer("client-x-recette", self.inventaire(), 4)
        self.nb_lots = len(synth.etat(self.dossier)["lots"])

    def executer(self, **options):
        return asyncio.run(synth.executer(self.dossier, lambda mode, numero: FausseSession(
            self.dossier, mode, numero, **options)))

    def test_complet(self):
        e = self.executer()
        self.assertTrue(e["termine"])
        self.assertFalse(e["partiel"])
        self.assertEqual(e["cout_usd"], 0.25 * (self.nb_lots + 1))
        rapport = (self.dossier / "rapport.md").read_text(encoding="utf-8")
        self.assertEqual(synth.valider_rapport(rapport), [])
        nb = len(synth.inventaire(self.dossier)["elements"])
        self.assertIn(f"Éléments de l'inventaire expliqués : {nb} / {nb} (100 %)", rapport)
        self.assertIn("26.21", rapport)
        self.assertIn("| moyenne | Le paramètre ZONE", rapport)
        self.assertEqual(rapport.count("| moyenne | Le paramètre ZONE"), 1)          # constat des lots dédoublonné
        self.assertEqual(rapport.count("- Usage réel de la zone"), 1)               # point à vérifier dédoublonné
        self.assertIn("### Script SQL", rapport)
        self.assertTrue((self.dossier / "rapport.docx").is_file())
        # lecture seule : sources intactes, écritures seulement sous .cache/synthese (et le journal)
        self.assertEqual(empreinte(self.racine / "sources"), self.avant)
        self.assertEqual({p.name for p in self.cache.iterdir()}, {"synthese"})

    def test_arret_puis_reprise(self):
        with self.assertRaises(KeyboardInterrupt):
            self.executer(arret={("lot", 2)})
        e = synth.etat(self.dossier)
        self.assertEqual([l["statut"] for l in e["lots"]][:2], ["fait", "a_faire"])
        FausseSession.appels = []
        e = self.executer()
        self.assertTrue(e["termine"])
        self.assertNotIn(("lot", 1), FausseSession.appels)       # lot terminé gardé
        self.assertEqual(FausseSession.appels[0], ("lot", 2))

    def test_lot_en_echec_rapport_partiel_puis_reprise(self):
        e = self.executer(echec={("lot", 1)})
        self.assertEqual(FausseSession.appels.count(("lot", 1)), 1)   # pas relancé dans la même exécution
        self.assertEqual(e["lots"][0]["statut"], "erreur")
        self.assertTrue(e["partiel"])
        rapport = (self.dossier / "rapport.md").read_text(encoding="utf-8")
        self.assertIn("lot 1 non traité (plafond atteint)", rapport)
        e = self.executer()
        self.assertTrue(e["termine"])
        self.assertEqual(FausseSession.appels[-2:], [("lot", 1), ("finale", 0)])   # finale refaite

    def test_aucun_lot_abouti_pas_de_finale(self):
        lots = {("lot", l["numero"]) for l in synth.etat(self.dossier)["lots"]}
        e = self.executer(echec=lots)
        self.assertNotIn(("finale", 0), FausseSession.appels)
        self.assertFalse(e.get("fichiers"))
        self.assertFalse((self.dossier / "rapport.md").exists())

    def test_reconcilier_apres_arret_brutal(self):
        lot = synth.etat(self.dossier)["lots"][0]
        synth.enregistrer_lot(self.dossier, 1, lot_valide(lot["elements"]))   # enregistré, état pas mis à jour
        self.executer()
        self.assertNotIn(("lot", 1), FausseSession.appels)


class TestSession(AvecProjet):
    def session(self, mode="lot", numero=1):
        self.dossier = synth.creer("client-x-recette", self.inventaire(), 4)
        reglages = config.Reglages(requetes_base_synthese=7, synthese_max_tours=55)
        with mock.patch.object(bitbucket, "configure", return_value=False):
            return Session(self.profil(), self.sources, [], reglages,
                           synthese={"mode": mode, "numero": numero, "dossier": str(self.dossier)})

    def test_outils_et_plafonds(self):
        s = self.session()
        noms = {o.name for o in s._outils_maison()}
        self.assertIn("synthese_demarrer", noms)
        self.assertFalse(noms & {"fichier_ecrire", "generation_demarrer", "documentation_demarrer",
                                 "documentation_enregistrer", "ecran_ouvrir"})
        self.assertEqual(s.plafond_base(), 7)
        self.assertEqual(s.options.max_turns, 55)
        self.assertIn("SYNTHÈSE CLIENT", s.options.system_prompt)
        self.assertNotIn("SYNTHÈSE CLIENT", consignes.construire(self.profil(), self.sources, [], "",
                                                                 config.Reglages()))

    def test_demarrer_et_enregistrer(self):
        s = self.session()
        outils = {o.name: o.handler for o in s._outils_maison()}
        texte = asyncio.run(outils["synthese_demarrer"]({}))["content"][0]["text"]
        lot = synth.etat(self.dossier)["lots"][0]
        for ident in lot["elements"]:
            self.assertIn(ident, texte)
        self.assertIn("7 accès base au plus", texte)
        refus = asyncio.run(outils["synthese_enregistrer"]({"elements": []}))
        self.assertTrue(refus["is_error"])
        self.assertFalse(s.synthese_enregistree)
        ok = asyncio.run(outils["synthese_enregistrer"](lot_valide(lot["elements"])))
        self.assertFalse(ok["is_error"])
        self.assertTrue(s.synthese_enregistree)
        partie = json.loads((self.dossier / "lots" / "001.json").read_text(encoding="utf-8"))
        self.assertEqual(partie["constats"][0]["statut"], "déduit")          # normalisé

    def test_finale(self):
        s = self.session("finale", 0)
        outils = {o.name: o.handler for o in s._outils_maison()}
        self.assertIn("Configuration en place", asyncio.run(outils["synthese_demarrer"]({}))["content"][0]["text"])
        self.assertFalse(asyncio.run(outils["synthese_enregistrer"](FINALE))["is_error"])


class TestReglages(AvecDossierTemporaire):
    def test_lecture_et_valeurs_par_defaut(self):
        fichier = self.racine / "profils.toml"
        fichier.write_text("[agent]\nsynthese_lot = 5\n", encoding="utf-8")
        with mock.patch.object(config, "FICHIER_PROFILS", fichier):
            r = config.reglages()
        self.assertEqual((r.synthese_lot, r.synthese_max_tours, r.requetes_base_synthese), (5, 80, 30))
