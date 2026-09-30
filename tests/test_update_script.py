from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
NEVER_REPLACED = {"METTRE_A_JOUR.bat", "data", ".venv", ".env", ".app.lock", ".git", ".sauvegarde_code"}
IGNORED = {".pytest_cache", "__pycache__"}


def code_items() -> set[str]:
    script = (ROOT / "mise_a_jour.ps1").read_text(encoding="utf-8")
    block = re.search(r"\$CodeItems = @\((.*?)\)", script, re.S).group(1)
    return set(re.findall(r"'([^']+)'", block))


class UpdateScriptTests(unittest.TestCase):
    def test_every_shipped_file_is_replaced_by_the_update(self) -> None:
        shipped = {
            path.name for path in ROOT.iterdir() if path.name not in NEVER_REPLACED and path.name not in IGNORED
        }
        self.assertEqual(shipped - code_items(), set(), "fichier livré absent de $CodeItems : il ne serait pas mis à jour")

    def test_user_data_is_never_in_the_replaced_list(self) -> None:
        self.assertEqual(code_items() & NEVER_REPLACED, set())

    def test_bat_is_a_single_command_line(self) -> None:
        # cmd relit un .bat au fil de l'eau : rien ne doit suivre l'appel PowerShell.
        lines = [
            line for line in (ROOT / "METTRE_A_JOUR.bat").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lower().startswith(("@echo", "cd /d", "rem"))
        ]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].rstrip().endswith("exit"))
        self.assertIn("mise_a_jour.ps1", lines[0])

    def test_user_data_folders_are_git_ignored(self) -> None:
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for entry in (".env", ".venv/", ".sauvegarde_code/"):
            self.assertIn(entry, ignored)


if __name__ == "__main__":
    unittest.main()
