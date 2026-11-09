"""Tests du garde-fou des outils de fichiers (odatio_agent.garde), dossier des fichiers générés compris."""
import asyncio

from odatio_agent.garde import creer_garde

from .outillage import AvecDossierTemporaire


class TestGarde(AvecDossierTemporaire):
    def setUp(self):
        super().setUp()
        self.sortie = self.cache / "fichiers" / "essai"
        (self.sortie / "sous").mkdir(parents=True)
        self.garde = creer_garde([self.source], [".env"], self.sortie)

    def decision(self, outil: str, **entree) -> str:
        reponse = asyncio.run(self.garde({"tool_name": outil, "tool_input": entree, "cwd": str(self.source)},
                                         None, None))
        return reponse.get("hookSpecificOutput", {}).get("permissionDecision", "allow")

    def test_read_autorise_dans_le_dossier_de_sortie(self):
        self.assertEqual(self.decision("Read", file_path=str(self.sortie / "CDE_1_T1.xml")), "allow")

    def test_read_refuse_sous_dossier_et_parent_du_dossier_de_sortie(self):
        self.assertEqual(self.decision("Read", file_path=str(self.sortie / "sous" / "a.xml")), "deny")
        self.assertEqual(self.decision("Read", file_path=str(self.sortie.parent / "autre.xml")), "deny")
        self.assertEqual(self.decision("Read", file_path=str(self.sortie / ".." / "autre.xml")), "deny")

    def test_grep_et_glob_refuses_dans_le_dossier_de_sortie(self):
        self.assertEqual(self.decision("Grep", pattern="x", path=str(self.sortie)), "deny")
        self.assertEqual(self.decision("Glob", pattern="*.xml", path=str(self.sortie)), "deny")

    def test_secrets_refuses_dans_le_dossier_de_sortie(self):
        self.assertEqual(self.decision("Read", file_path=str(self.sortie / ".env")), "deny")

    def test_toujours_refuse_ailleurs(self):
        self.assertEqual(self.decision("Read", file_path=str(self.racine / "ailleurs.xml")), "deny")
        self.assertEqual(self.decision("Read", file_path=str(self.cache / "journal" / "outils.jsonl")), "deny")

    def test_sources_inchangees(self):
        self.assertEqual(self.decision("Read", file_path=str(self.source / "a" / "b.java")), "allow")
        self.assertEqual(self.decision("Read", file_path=str(self.source / ".env")), "deny")
        self.assertEqual(self.decision("Grep", pattern="x"), "allow")

    def test_sans_dossier_de_sortie(self):
        self.garde = creer_garde([self.source], [])
        self.assertEqual(self.decision("Read", file_path=str(self.sortie / "a.xml")), "deny")


if __name__ == "__main__":
    import unittest
    unittest.main()
