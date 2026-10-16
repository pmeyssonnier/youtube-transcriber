from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PWSH = shutil.which("pwsh")  # PowerShell 7 (Windows, Linux, macOS)
WINDOWS_POWERSHELL = shutil.which("powershell") if os.name == "nt" else None  # 5.1 : celle de METTRE_A_JOUR.bat

OLD_FILES = {
    "VERSION": "1.0.0\n",
    "app/main.py": "VERSION_CODE = 'ancien'\n",
    "app/pipeline.py": "x = 1\n",
    "app/static/index.html": "<html>ancien</html>\n",
    "run.py": "print('ancien')\n",
    "requirements.txt": "pip\n",
    "constraints.txt": "# rien\n",
    "demarrer.bat": "@echo off\n",
    "installer_windows.ps1": "# installateur\n",
    "README.md": "ancien readme\n",
    ".gitignore": ".env\n",
}
NEW_FILES = {
    **OLD_FILES,
    "mise_a_jour.ps1": "# script de la nouvelle version\n",
    "VERSION": "2.0.0\n",
    "app/main.py": "VERSION_CODE = 'nouveau'\n",
    "docs/format-json.md": "nouveau document\n",
    "README.md": "nouveau readme\n",
}
USER_FILES = {
    ".env": 'OPENAI_API_KEY="sk-secret"\n',
    "data/jobs/abc/job.json": '{"id": "abc"}\n',
    "data/referentiel/elus_mandats.json": "[]\n",
}


def write_tree(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")


def make_zip(path: Path, files: dict[str, str], prefix: str = "youtube-transcriber-main/") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(prefix + name, content)


def snapshot(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_file() and not relative.startswith((".sauvegarde_code", ".mise_a_jour_en_cours")) and "__pycache__" not in relative:
            result[relative] = path.read_text(encoding="utf-8")
    return result


class UpdateRollbackCases:
    """Une mise à jour ratée remet l'ancien code en place (audit P1-03).

    Les mêmes scénarios sont joués avec PowerShell 7 puis, sous Windows, avec Windows PowerShell 5.1
    (celui que lance METTRE_A_JOUR.bat).
    """

    POWERSHELL: str | None = None

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        self.project = base / "projet"
        write_tree(self.project, {**OLD_FILES, **USER_FILES})
        shutil.copyfile(ROOT / "mise_a_jour.ps1", self.project / "mise_a_jour.ps1")
        self.zip = base / "nouvelle.zip"
        self.before = snapshot(self.project)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def run_update(self, zip_path: Path | None = None, **env) -> subprocess.CompletedProcess:
        environment = {**os.environ, "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
        environment.pop("YTT_SIMULATE_SWAP_FAILURE", None)
        environment.update(env)
        command = [
            self.POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.project / "mise_a_jour.ps1"),
            "-ZipPath", str(zip_path or self.zip), "-PythonPath", sys.executable,
        ]
        return subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=environment, timeout=300, cwd=self.project,
        )

    def assertUnchanged(self) -> None:
        self.assertEqual(snapshot(self.project), self.before)
        self.assertFalse((self.project / ".mise_a_jour_en_cours").exists())

    def test_successful_update_replaces_code_and_keeps_user_data(self) -> None:
        make_zip(self.zip, NEW_FILES)
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        after = snapshot(self.project)
        self.assertEqual(after["VERSION"], "2.0.0\n")
        self.assertEqual(after["app/main.py"], "VERSION_CODE = 'nouveau'\n")
        self.assertEqual(after["docs/format-json.md"], "nouveau document\n")
        for name, content in USER_FILES.items():
            self.assertEqual(after[name], content)
        self.assertFalse((self.project / ".mise_a_jour_en_cours").exists())
        backups = list((self.project / ".sauvegarde_code").glob("1.0.0-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "app" / "main.py").read_text(encoding="utf-8"), OLD_FILES["app/main.py"])

    def test_incomplete_zip_is_refused_before_any_change(self) -> None:
        incomplete = {name: content for name, content in NEW_FILES.items() if name != "run.py"}
        make_zip(self.zip, incomplete)
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("incomplet", result.stdout)
        self.assertIn("run.py", result.stdout)
        self.assertUnchanged()

    def test_corrupt_zip_is_refused_before_any_change(self) -> None:
        self.zip.write_bytes(b"ceci n'est pas un zip")
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("illisible", result.stdout)
        self.assertUnchanged()

    def test_new_code_with_a_syntax_error_is_refused_before_any_change(self) -> None:
        make_zip(self.zip, {**NEW_FILES, "app/main.py": "def (:\n"})
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("erreur", result.stdout.lower())
        self.assertUnchanged()

    def test_failure_in_the_middle_of_the_replacement_restores_everything(self) -> None:
        make_zip(self.zip, NEW_FILES)
        # « app » est remplacé avant « README.md » : l'échec arrive quand une partie est déjà en place.
        result = self.run_update(YTT_SIMULATE_SWAP_FAILURE="README.md")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("restauree", result.stdout)
        self.assertUnchanged()
        self.assertEqual((self.project / "VERSION").read_text(encoding="utf-8"), "1.0.0\n")
        self.assertFalse((self.project / "docs").exists(), "un dossier ajouté par la mise à jour doit disparaître")
        self.assertIn("ECHEC", (self.project / ".sauvegarde_code" / "mise_a_jour.log").read_text(encoding="utf-8"))

    def test_failure_on_the_very_first_item_restores_everything(self) -> None:
        make_zip(self.zip, NEW_FILES)
        result = self.run_update(YTT_SIMULATE_SWAP_FAILURE="app")
        self.assertNotEqual(result.returncode, 0)
        self.assertUnchanged()

    def test_python_components_failure_restores_the_old_code(self) -> None:
        make_zip(self.zip, {**NEW_FILES, "requirements.txt": "paquet-qui-n-existe-pas==9.9.9\n"})
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("composants Python", result.stdout)
        self.assertIn("restauree", result.stdout)
        self.assertUnchanged()

    def test_new_version_that_cannot_start_is_rolled_back(self) -> None:
        make_zip(self.zip, {**NEW_FILES, "app/main.py": "import module_qui_n_existe_pas\n"})
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ne demarre pas", result.stdout)
        self.assertUnchanged()

    def test_an_interrupted_update_is_repaired_at_the_next_run(self) -> None:
        # Simule une coupure en plein remplacement : « app » déplacé dans « ancien », rien de remis.
        previous = self.project / ".mise_a_jour_en_cours" / "ancien"
        previous.mkdir(parents=True)
        shutil.move(str(self.project / "app"), str(previous / "app"))
        (self.project / "docs").mkdir()
        (self.project / "docs" / "x.md").write_text("ajouté", encoding="utf-8")
        (self.project / ".mise_a_jour_en_cours" / "ajoutes.txt").write_text("docs\n", encoding="utf-8")

        make_zip(self.zip, {**NEW_FILES, "app/main.py": "def (:\n"})  # la nouvelle mise à jour échoue aussi
        result = self.run_update()
        self.assertIn("interrompue", result.stdout)
        self.assertUnchanged()
        self.assertEqual(snapshot(self.project)["app/main.py"], OLD_FILES["app/main.py"])


@unittest.skipUnless(PWSH, "PowerShell 7 absent")
class UpdateRollbackPowerShell7Tests(UpdateRollbackCases, unittest.TestCase):
    POWERSHELL = PWSH


@unittest.skipUnless(WINDOWS_POWERSHELL, "Windows PowerShell 5.1 absent")
class UpdateRollbackWindowsPowerShellTests(UpdateRollbackCases, unittest.TestCase):
    POWERSHELL = WINDOWS_POWERSHELL


if __name__ == "__main__":
    unittest.main()
