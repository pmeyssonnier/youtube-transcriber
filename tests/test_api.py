from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app, store


class ApiSecurityTests(unittest.TestCase):
    def test_health_static_page_and_security_headers(self) -> None:
        client = TestClient(app, base_url="http://localhost")
        response = client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["version"], "1.6.0")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("Analyser la vidéo", client.get("/").text)

    def test_rejects_unknown_host_and_cross_site_mutation(self) -> None:
        bad_host = TestClient(app, base_url="http://testserver")
        self.assertEqual(bad_host.get("/api/health").status_code, 400)

        client = TestClient(app, base_url="http://localhost")
        response = client.post(
            "/api/settings/api-key",
            headers={"Origin": "https://malveillant.example"},
            json={"api_key": "sk-" + "x" * 30},
        )
        self.assertEqual(response.status_code, 403)

    def test_retry_requires_failed_job_and_reports_running_conflict(self) -> None:
        client = TestClient(app, base_url="http://localhost")
        with tempfile.TemporaryDirectory() as directory:
            jobs_dir = Path(directory)
            with patch("app.store.JOBS_DIR", jobs_dir):
                job_id = "d" * 32
                store.create({"id": job_id, "url": "https://youtu.be/euuLzgjj7z4", "status": "failed"})

                with patch("app.main.pipeline.submit", return_value=True) as submit:
                    response = client.post(f"/api/jobs/{job_id}/retry")
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.json()["status"], "queued")
                submit.assert_called_once_with(job_id)

                store.update(job_id, status="failed")
                with patch("app.main.pipeline.submit", return_value=False):
                    response = client.post(f"/api/jobs/{job_id}/retry")
                self.assertEqual(response.status_code, 409)
                self.assertIn("déjà en cours", response.json()["detail"])

                store.update(job_id, status="completed")
                response = client.post(f"/api/jobs/{job_id}/retry")
                self.assertEqual(response.status_code, 409)

    def test_settings_live_in_a_dialog_not_on_the_main_screen(self) -> None:
        html = TestClient(app, base_url="http://localhost").get("/").text
        dialog = html[html.index('<dialog class="settings-dialog"'):html.index("</dialog>")]
        main = html[html.index("<main>"):html.index("</main>")]
        for element_id in ('id="keyForm"', 'id="updateYtdlp"', 'id="ytdlpVersion"'):
            self.assertIn(element_id, dialog)
            self.assertNotIn(element_id, main)
        self.assertIn('id="openSettings"', html)


if __name__ == "__main__":
    unittest.main()
