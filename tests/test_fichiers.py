"""Tests de l'outil fichier_ecrire (odatio_agent.fichiers)."""
import json
from pathlib import Path

from odatio_agent import config, fichiers
from odatio_agent.outils import OutilErreur

from .outillage import AvecDossierTemporaire


class TestEcrire(AvecDossierTemporaire):
    def test_ecriture_nominale(self):
        f = fichiers.ecrire(self.profil(), "CDE_0082380800_T1.xml", "<commande>T1</commande>\n")
        chemin = self.cache / "fichiers" / "essai" / "CDE_0082380800_T1.xml"
        self.assertEqual(f["chemin"], str(chemin))
        self.assertEqual(chemin.read_bytes(), b"<commande>T1</commande>\n")
        self.assertEqual(f["taille"], 24)
        self.assertFalse(f["renomme"])

    def test_dossier_configure(self):
        dossier = self.racine / "a_integrer"
        f = fichiers.ecrire(self.profil(dossier_fichiers=str(dossier)), "a.csv", "x;y\n")
        self.assertEqual(f["chemin"], str(dossier / "a.csv"))

    def test_journal_sans_contenu(self):
        fichiers.ecrire(self.profil(), "a.txt", "CONTENU-CONFIDENTIEL")
        lignes = (self.cache / "journal" / "outils.jsonl").read_text(encoding="utf-8").splitlines()
        entree = json.loads(lignes[-1])
        self.assertEqual((entree["outil"], entree["fichier"], entree["taille"]), ("fichier_ecrire", "a.txt", 20))
        self.assertNotIn("CONTENU-CONFIDENTIEL", lignes[-1])

    def test_pas_d_ecrasement(self):
        premier = fichiers.ecrire(self.profil(), "a.xml", "1")
        second = fichiers.ecrire(self.profil(), "a.xml", "2")
        troisieme = fichiers.ecrire(self.profil(), "a.xml", "3")
        self.assertEqual(len({premier["chemin"], second["chemin"], troisieme["chemin"]}), 3)
        self.assertTrue(second["renomme"])
        self.assertRegex(second["nom"], r"^a_\d{8}-\d{6}\.xml$")
        self.assertEqual(Path(premier["chemin"]).read_bytes(), b"1")

    def test_encodage_et_fins_de_ligne(self):
        f = fichiers.ecrire(self.profil(), "a.txt", "élève\nfin\r\n", encodage="iso-8859-1", fins_ligne="crlf")
        self.assertEqual(Path(f["chemin"]).read_bytes(), "élève\r\nfin\r\n".encode("iso-8859-1"))
        f = fichiers.ecrire(self.profil(), "b.txt", "a\r\nb\rc\n")
        self.assertEqual(Path(f["chemin"]).read_bytes(), b"a\nb\nc\n")
        f = fichiers.ecrire(self.profil(), "c.txt", "€")
        self.assertEqual(Path(f["chemin"]).read_bytes(), "€".encode("utf-8"))   # sans BOM
        f = fichiers.ecrire(self.profil(), "d.txt", "€", encodage="cp1252")
        self.assertEqual(Path(f["chemin"]).read_bytes(), b"\x80")

    def test_caractere_hors_encodage_refuse(self):
        with self.assertRaisesRegex(OutilErreur, "n'existe pas en iso-8859-1"):
            fichiers.ecrire(self.profil(), "a.txt", "prix 10 €", encodage="iso-8859-1")

    def test_encodage_inconnu_refuse(self):
        with self.assertRaises(OutilErreur):
            fichiers.ecrire(self.profil(), "a.txt", "x", encodage="utf-16")

    def test_secrets_masques(self):
        f = fichiers.ecrire(self.profil(), "a.json", '{"password": "Tr3sSecret!"}')
        self.assertEqual(f["secrets_masques"], 1)
        self.assertNotIn(b"Tr3sSecret", Path(f["chemin"]).read_bytes())

    def test_contenu_trop_gros(self):
        with self.assertRaisesRegex(OutilErreur, "trop gros"):
            fichiers.ecrire(self.profil(), "a.txt", "x" * (fichiers.TAILLE_MAX + 1))
        with self.assertRaisesRegex(OutilErreur, "trop gros"):   # 5 Mo dépassés une fois encodé
            fichiers.ecrire(self.profil(), "a.txt", "é" * (fichiers.TAILLE_MAX // 2 + 1))
        self.assertFalse((self.cache / "fichiers" / "essai" / "a.txt").exists())

    def test_noms_refuses(self):
        for nom in [str(self.racine / "a.xml"), "C:a.xml", "/tmp/a.xml", "..", "../a.xml", "a..xml",
                    "sous/a.xml", "sous\\a.xml", ".env", "a b.xml", "commande.xml.exe", "a.sh", "a", "",
                    "CON.txt", "nul.xml", "é.xml"]:
            with self.subTest(nom=nom), self.assertRaises(OutilErreur):
                fichiers.ecrire(self.profil(), nom, "x")
        self.assertFalse(any(self.racine.rglob("*.exe")))

    def test_extensions_autorisees(self):
        for extension in fichiers.EXTENSIONS + (".XML",):
            with self.subTest(extension=extension):
                fichiers.ecrire(self.profil(), f"essai{extension}", "x")

    def test_dossier_sous_une_source_refuse_a_l_ecriture(self):
        profil = self.profil(dossier_fichiers=str(self.source / "entree"))
        with self.assertRaisesRegex(OutilErreur, "source"):
            fichiers.ecrire(profil, "a.xml", "x")
        self.assertFalse((self.source / "entree").exists())


if __name__ == "__main__":
    import unittest
    unittest.main()
