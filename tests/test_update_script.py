from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
# .github : configuration de l'intégration continue, inutile dans une installation
NEVER_REPLACED = {"METTRE_A_JOUR.bat", "data", ".venv", ".env", ".app.lock", ".git", ".github", ".sauvegarde_code"}
IGNORED = {".pytest_cache", "__pycache__", ".coverage", ".DS_Store", "Thumbs.db"}


def shipped_names() -> set[str]:
    """Top-level names shipped with the application.

    Uses git when available, so personal or tool folders (.ruff_cache, .vscode, .coverage...)
    never count; falls back to ignoring hidden folders and known junk otherwise.
    """
    try:
        output = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True, timeout=20
        ).stdout
        names = {line.split("/")[0] for line in output.splitlines() if line.strip()}
        if names:
            return names - NEVER_REPLACED
    except (OSError, subprocess.SubprocessError):
        pass
    return {
        path.name
        for path in ROOT.iterdir()
        if path.name not in NEVER_REPLACED
        and path.name not in IGNORED
        and not (path.is_dir() and path.name.startswith("."))
    }


def code_items() -> set[str]:
    script = (ROOT / "mise_a_jour.ps1").read_text(encoding="utf-8")
    block = re.search(r"\$CodeItems = @\((.*?)\)", script, re.S).group(1)
    return set(re.findall(r"'([^']+)'", block))


class UpdateScriptTests(unittest.TestCase):
    def test_every_shipped_file_is_replaced_by_the_update(self) -> None:
        self.assertEqual(
            shipped_names() - code_items(), set(),
            "fichier livré absent de $CodeItems : il ne serait pas mis à jour",
        )

    def test_tool_caches_do_not_break_the_check(self) -> None:
        for name in (".ruff_cache", ".mypy_cache", ".vscode"):
            folder = ROOT / name
            if folder.exists():
                continue
            folder.mkdir()
            try:
                self.assertNotIn(name, shipped_names())
            finally:
                folder.rmdir()

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

    def test_demarrer_updates_only_git_clones_and_never_blocks_startup(self) -> None:
        text = (ROOT / "demarrer.bat").read_text(encoding="utf-8")
        self.assertIn('if exist ".git"', text)  # copie issue d'un zip : pas de mise à jour git
        self.assertIn("where git", text)
        self.assertIn("git pull --ff-only", text)  # jamais de fusion automatique
        self.assertNotIn("git pull origin", text)  # ne tire pas une autre branche dans la branche courante
        self.assertIn("-r requirements.txt -c constraints.txt", text)
        update = text.index("git pull")
        self.assertLess(update, text.index('"run.py"') if '"run.py"' in text else text.index("run.py"))
        self.assertIn("Demarrage", text)  # un echec de mise a jour ne bloque pas le demarrage


if __name__ == "__main__":
    unittest.main()
