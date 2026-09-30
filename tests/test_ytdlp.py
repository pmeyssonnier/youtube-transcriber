from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import ytdlp
from app.main import app


def completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class YtDlpUpdateTests(unittest.TestCase):
    def test_reports_previous_and_new_version(self) -> None:
        with (
            patch("app.ytdlp.ytdlp_version", side_effect=["2026.8.19", "2026.9.1"]),
            patch("app.ytdlp.subprocess.run", return_value=completed()) as run,
        ):
            result = ytdlp.update_ytdlp()
        self.assertEqual(result, {"previous_version": "2026.8.19", "version": "2026.9.1", "updated": True, "development_build": False})
        command = run.call_args.args[0]
        self.assertEqual(command[1:4], ["-m", "pip", "install"])
        self.assertIn("yt-dlp[default]", command)

    def test_stable_by_default_and_pre_releases_only_on_request(self) -> None:
        with (
            patch("app.ytdlp.ytdlp_version", return_value="2026.8.19"),
            patch("app.ytdlp.subprocess.run", return_value=completed()) as run,
        ):
            ytdlp.update_ytdlp()
            self.assertNotIn("--pre", run.call_args.args[0])
            ytdlp.update_ytdlp(include_dev=True)
            self.assertIn("--pre", run.call_args.args[0])

    def test_flags_development_builds(self) -> None:
        for version, expected in (("2026.9.27.232945.dev0", True), ("2026.8.19", False)):
            with (
                self.subTest(version=version),
                patch("app.ytdlp.ytdlp_version", side_effect=["2026.8.1", version]),
                patch("app.ytdlp.subprocess.run", return_value=completed()),
            ):
                self.assertEqual(ytdlp.update_ytdlp()["development_build"], expected)

    def test_requirements_ask_for_a_stable_ytdlp(self) -> None:
        requirements = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text(encoding="utf-8")
        line = next(item for item in requirements.splitlines() if item.startswith("yt-dlp"))
        self.assertRegex(line, r"^yt-dlp\[default\]>=\d{4}\.\d{1,2}\.\d{1,2}$")

    def test_already_up_to_date(self) -> None:
        with (
            patch("app.ytdlp.ytdlp_version", return_value="2026.9.1"),
            patch("app.ytdlp.subprocess.run", return_value=completed()),
        ):
            self.assertFalse(ytdlp.update_ytdlp()["updated"])

    def test_pip_failure_and_timeout_raise_readable_errors(self) -> None:
        with (
            patch("app.ytdlp.ytdlp_version", return_value="1"),
            patch("app.ytdlp.subprocess.run", return_value=completed(1, stderr="réseau coupé")),
        ):
            with self.assertRaisesRegex(ytdlp.YtDlpUpdateError, "réseau coupé"):
                ytdlp.update_ytdlp()
        with (
            patch("app.ytdlp.ytdlp_version", return_value="1"),
            patch("app.ytdlp.subprocess.run", side_effect=subprocess.TimeoutExpired("pip", 1)),
        ):
            with self.assertRaisesRegex(ytdlp.YtDlpUpdateError, "délai"):
                ytdlp.update_ytdlp()

    def test_concurrent_update_is_refused(self) -> None:
        self.assertTrue(ytdlp._update_lock.acquire(blocking=False))
        try:
            with self.assertRaisesRegex(ytdlp.YtDlpUpdateError, "déjà en cours"):
                ytdlp.update_ytdlp()
        finally:
            ytdlp._update_lock.release()


class YtDlpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, base_url="http://localhost")

    def test_status_endpoint(self) -> None:
        with patch("app.main.ytdlp_version", return_value="2026.9.1"):
            response = self.client.get("/api/yt-dlp")
        self.assertEqual(response.json(), {"version": "2026.9.1"})

    def test_update_forwards_the_development_option(self) -> None:
        payload = {"previous_version": "1", "version": "2", "updated": True, "development_build": True}
        with (
            patch("app.main.store.unfinished", return_value=[]),
            patch("app.main.update_ytdlp", return_value=payload) as update,
        ):
            self.client.post("/api/yt-dlp/update", json={"include_dev": True})
            update.assert_called_with(include_dev=True)
            self.client.post("/api/yt-dlp/update")
            update.assert_called_with(include_dev=False)

    def test_update_is_refused_while_a_job_is_running(self) -> None:
        with (
            patch("app.main.store.unfinished", return_value=[{"id": "x"}]),
            patch("app.main.update_ytdlp") as update,
        ):
            response = self.client.post("/api/yt-dlp/update")
        self.assertEqual(response.status_code, 409)
        update.assert_not_called()

    def test_update_success_and_failure(self) -> None:
        payload = {"previous_version": "1", "version": "2", "updated": True}
        with patch("app.main.store.unfinished", return_value=[]), patch("app.main.update_ytdlp", return_value=payload):
            response = self.client.post("/api/yt-dlp/update")
        self.assertEqual(response.json(), payload)

        with (
            patch("app.main.store.unfinished", return_value=[]),
            patch("app.main.update_ytdlp", side_effect=ytdlp.YtDlpUpdateError("échec")),
        ):
            response = self.client.post("/api/yt-dlp/update")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "échec")

    def test_update_rejects_cross_site_request(self) -> None:
        response = self.client.post("/api/yt-dlp/update", headers={"Origin": "https://malveillant.example"})
        self.assertEqual(response.status_code, 403)

    def test_page_contains_update_button(self) -> None:
        self.assertIn('id="updateYtdlp"', self.client.get("/").text)


if __name__ == "__main__":
    unittest.main()
