from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GIT = shutil.which("git")
PWSH = shutil.which("pwsh")
WINDOWS_POWERSHELL = shutil.which("powershell") if os.name == "nt" else None

FILES = {
    "app/main.py": "VERSION_CODE = 'v1'\n",
    "requirements.txt": "pip\n",
    "constraints.txt": "# rien\n",
    "README.md": "v1\n",
}


def git(directory: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.org", "-c", "commit.gpgsign=false", *args],
        cwd=directory, capture_output=True, text=True, encoding="utf-8", check=True, timeout=60,
    )
    return result.stdout.strip()


def write(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")


class GitUpdateCases:
    """Mise à jour automatique d'un clone git au démarrage, avec retour en arrière (audit R6)."""

    POWERSHELL: str | None = None

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        self.remote = base / "remote.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.remote)], check=True, capture_output=True)
        self.author = base / "auteur"
        git(base, "clone", str(self.remote), str(self.author))
        git(self.author, "checkout", "-B", "main")
        write(self.author, FILES)
        git(self.author, "add", "-A")
        git(self.author, "commit", "-m", "v1")
        git(self.author, "push", "-u", "origin", "main")
        self.user = base / "utilisateur"
        git(base, "clone", "-b", "main", str(self.remote), str(self.user))
        shutil.copyfile(ROOT / "demarrer_maj.ps1", self.user / "demarrer_maj.ps1")
        (self.user / ".git" / "info" / "exclude").write_text("demarrer_maj.ps1\n", encoding="utf-8")
        self.old_commit = git(self.user, "rev-parse", "HEAD")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def publish(self, files: dict[str, str]) -> str:
        write(self.author, files)
        git(self.author, "add", "-A")
        git(self.author, "commit", "-m", "v2")
        git(self.author, "push", "origin", "main")
        return git(self.author, "rev-parse", "HEAD")

    def run_script(self, **env) -> subprocess.CompletedProcess:
        environment = {**os.environ, "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1", "GIT_TERMINAL_PROMPT": "0"}
        environment.update(env)
        return subprocess.run(
            [self.POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.user / "demarrer_maj.ps1"),
             "-PythonPath", sys.executable],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=environment, timeout=300, cwd=self.user,
        )

    def head(self) -> str:
        return git(self.user, "rev-parse", "HEAD")

    def test_a_working_update_is_applied(self) -> None:
        new_commit = self.publish({"app/main.py": "VERSION_CODE = 'v2'\n"})
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.head(), new_commit)
        self.assertEqual((self.user / "app" / "main.py").read_text(encoding="utf-8"), "VERSION_CODE = 'v2'\n")
        self.assertNotIn("restaur", result.stdout.lower())

    def test_nothing_new_changes_nothing(self) -> None:
        result = self.run_script()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.head(), self.old_commit)

    def test_failing_components_roll_the_code_back(self) -> None:
        self.publish({"requirements.txt": "paquet-qui-n-existe-pas==9.9.9\n", "app/main.py": "VERSION_CODE = 'v2'\n"})
        result = self.run_script()
        self.assertEqual(result.returncode, 0, "le démarrage ne doit jamais être bloqué")
        self.assertEqual(self.head(), self.old_commit)
        self.assertEqual((self.user / "app" / "main.py").read_text(encoding="utf-8"), "VERSION_CODE = 'v1'\n")
        self.assertEqual((self.user / "requirements.txt").read_text(encoding="utf-8"), "pip\n")
        self.assertIn("version precedente", result.stdout)

    def test_a_new_version_that_cannot_start_is_rolled_back(self) -> None:
        self.publish({"app/main.py": "import module_qui_n_existe_pas\n"})
        result = self.run_script()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.head(), self.old_commit)
        self.assertEqual((self.user / "app" / "main.py").read_text(encoding="utf-8"), "VERSION_CODE = 'v1'\n")
        self.assertIn("ne demarre pas", result.stdout)

    def test_local_changes_survive_a_rollback(self) -> None:
        (self.user / "notes.txt").write_text("a moi", encoding="utf-8")                       # fichier non suivi
        (self.user / "README.md").write_text("v1 modifie localement\n", encoding="utf-8")      # fichier suivi modifié
        self.publish({"app/main.py": "import module_qui_n_existe_pas\n"})
        self.run_script()
        self.assertEqual(self.head(), self.old_commit)
        self.assertEqual((self.user / "notes.txt").read_text(encoding="utf-8"), "a moi")
        self.assertEqual((self.user / "README.md").read_text(encoding="utf-8"), "v1 modifie localement\n")

    def test_a_pull_that_cannot_be_done_never_blocks_the_start(self) -> None:
        self.publish({"README.md": "v2\n"})
        (self.user / "README.md").write_text("modification locale en conflit\n", encoding="utf-8")
        result = self.run_script()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.head(), self.old_commit)
        self.assertEqual((self.user / "README.md").read_text(encoding="utf-8"), "modification locale en conflit\n")
        self.assertIn("impossible", result.stdout)

    def test_not_a_git_repository_is_ignored(self) -> None:
        shutil.rmtree(self.user / ".git")
        result = self.run_script()
        self.assertEqual(result.returncode, 0)


@unittest.skipUnless(GIT and PWSH, "git ou PowerShell 7 absent")
class GitUpdatePowerShell7Tests(GitUpdateCases, unittest.TestCase):
    POWERSHELL = PWSH


@unittest.skipUnless(GIT and WINDOWS_POWERSHELL, "git ou Windows PowerShell 5.1 absent")
class GitUpdateWindowsPowerShellTests(GitUpdateCases, unittest.TestCase):
    POWERSHELL = WINDOWS_POWERSHELL


if __name__ == "__main__":
    unittest.main()
