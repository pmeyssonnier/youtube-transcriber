from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.pipeline import PipelineError, TranscriptionPipeline, _diagnose
from app.process import ProcessStalled, run_supervised


def python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


class SupervisedProcessTests(unittest.TestCase):
    def test_normal_run_returns_output_and_exit_code(self) -> None:
        result = run_supervised(python("import sys; print('ok'); print('e', file=sys.stderr); sys.exit(3)"), poll=0.05)
        self.assertEqual((result.returncode, result.stdout.strip(), result.stderr.strip()), (3, "ok", "e"))

    def test_overall_timeout_stops_the_process(self) -> None:
        started = time.monotonic()
        with self.assertRaises(ProcessStalled) as caught:
            run_supervised(python("import time; time.sleep(60)"), timeout=0.6, poll=0.1)
        self.assertEqual(caught.exception.kind, "total")
        self.assertLess(time.monotonic() - started, 20)

    def test_process_that_stops_writing_is_stopped_but_one_that_keeps_writing_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            stuck = f"import time, pathlib; pathlib.Path(r'{folder}/a.part').write_bytes(b'x'); time.sleep(60)"
            with self.assertRaises(ProcessStalled) as caught:
                run_supervised(python(stuck), idle_timeout=1.0, watch=folder, poll=0.1)
            self.assertEqual(caught.exception.kind, "idle")

            busy = (
                "import time, pathlib\n"
                f"p = pathlib.Path(r'{folder}/b.part')\n"
                "for i in range(12):\n"
                "    p.write_bytes(b'x' * (i + 1)); time.sleep(0.25)\n"
                "print('fini')"
            )
            result = run_supervised(python(busy), idle_timeout=1.0, watch=folder, poll=0.1)
            self.assertEqual((result.returncode, result.stdout.strip()), (0, "fini"))

    def test_child_processes_are_stopped_too(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "survivant.txt"
            child = f"import time, pathlib; time.sleep(2); pathlib.Path(r'{marker}').write_text('vivant')"
            parent = (
                "import subprocess, sys, time\n"
                f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
                "time.sleep(60)"
            )
            with self.assertRaises(ProcessStalled):
                run_supervised(python(parent), timeout=0.8, poll=0.1)
            time.sleep(3.2)
            self.assertFalse(marker.exists(), "le processus enfant devait être arrêté avec son parent")


class RunCommandMessageTests(unittest.TestCase):
    def test_stalled_download_gives_a_recoverable_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            code = f"import time, pathlib; pathlib.Path(r'{folder}/x').write_bytes(b'1'); time.sleep(60)"
            with patch("app.process.POLL_SECONDS", 0.1):
                with self.assertRaises(PipelineError) as caught:
                    TranscriptionPipeline._run_command(
                        python(code), "Le téléchargement audio a échoué.", watch=folder, idle_timeout=1.0
                    )
            message = str(caught.exception)
            self.assertIn("Le téléchargement audio a échoué.", message)
            self.assertIn("Aucune progression", message)
            self.assertIn("Relancer", message)

    def test_failures_are_explained(self) -> None:
        cases = {
            "OSError: [Errno 28] No space left on device": "disque est plein",
            "ERROR: Sign in to confirm you're not a bot": "connexion",
            "ERROR: Private video": "privée",
            "HTTP Error 403: Forbidden": "refusé",
            "Temporary failure in name resolution": "réseau",
        }
        for stderr, expected in cases.items():
            with self.subTest(stderr):
                self.assertIn(expected, _diagnose(stderr))
        self.assertEqual(_diagnose("erreur inconnue"), "")

        with self.assertRaises(PipelineError) as caught:
            TranscriptionPipeline._run_command(
                python("import sys; print('No space left on device', file=sys.stderr); sys.exit(1)"), "Échec."
            )
        self.assertIn("disque est plein", str(caught.exception))
        with self.assertRaises(PipelineError) as caught:
            TranscriptionPipeline._run_command(["programme-introuvable-xyz"], "Échec.")
        self.assertIn("introuvable", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
