from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.config as config
from app.main import app

KEY = "sk-test-key-for-unit-tests-only-0123456789"
OTHER_KEY = "sk-another-key-for-unit-tests-only-9876"


class KeyStorageCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.project = self.root / "projet"
        self.project.mkdir()
        self.legacy = self.project / ".env"
        self.user = self.root / "config" / "youtube-transcriber" / ".env"
        self.patches = [
            patch.object(config, "ENV_FILE", self.user),
            patch.object(config, "LEGACY_ENV_FILE", self.legacy),
            patch.object(config, "PROJECT_ROOT", self.project),
            patch.dict(config.migration, {"moved": False, "error": None}),
            patch.dict(os.environ, {}, clear=False),
        ]
        for item in self.patches:
            item.start()
        os.environ.pop("OPENAI_API_KEY", None)

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()


class LocationTests(unittest.TestCase):
    """La clé n'est plus dans le dossier de l'application (vérification R4)."""

    def test_windows_uses_local_app_data(self) -> None:
        with patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\Pierre\AppData\Local"}):
            os.environ.pop("TRANSCRIBER_ENV_FILE", None)
            path = config.default_env_file("nt")
        self.assertTrue(str(path).replace("\\", "/").endswith("AppData/Local/youtube-transcriber/.env"), path)

    def test_other_systems_use_xdg_config(self) -> None:
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/home/p/.cfg"}):
            os.environ.pop("TRANSCRIBER_ENV_FILE", None)
            self.assertEqual(config.default_env_file("posix"), Path("/home/p/.cfg/youtube-transcriber/.env"))

    def test_an_explicit_path_wins(self) -> None:
        with patch.dict(os.environ, {"TRANSCRIBER_ENV_FILE": "/somewhere/secret.env"}):
            self.assertEqual(config.default_env_file(), Path("/somewhere/secret.env"))

    def test_the_default_is_never_inside_the_project(self) -> None:
        with patch.dict(os.environ, {}):
            os.environ.pop("TRANSCRIBER_ENV_FILE", None)
            self.assertNotIn(config.PROJECT_ROOT, config.default_env_file().parents)


class MigrationTests(KeyStorageCase):
    def test_the_key_moves_and_the_old_file_keeps_its_other_lines(self) -> None:
        self.legacy.write_text(f'# réglages\nOPENAI_API_KEY="{KEY}"\nTRANSCRIBER_DATA_DIR=D:\\Transcriptions\n', encoding="utf-8")
        self.assertTrue(config.migrate_legacy_key())
        self.assertEqual(config._read_key(self.user), KEY)
        remaining = self.legacy.read_text(encoding="utf-8")
        self.assertNotIn(KEY, remaining)
        self.assertNotIn("OPENAI_API_KEY", remaining)
        self.assertIn("TRANSCRIBER_DATA_DIR=D:\\Transcriptions", remaining)
        self.assertIn("# réglages", remaining)
        self.assertTrue(config.migration["moved"])
        self.assertEqual(config.get_openai_api_key(), KEY)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.user.stat().st_mode), 0o600)

    def test_it_is_idempotent(self) -> None:
        self.legacy.write_text(f'OPENAI_API_KEY="{KEY}"\n', encoding="utf-8")
        self.assertTrue(config.migrate_legacy_key())
        self.assertFalse(config.migrate_legacy_key())
        self.assertEqual(config._read_key(self.user), KEY)

    def test_nothing_happens_without_a_key_in_the_old_file(self) -> None:
        self.legacy.write_text("TRANSCRIBER_DATA_DIR=D:\\x\n", encoding="utf-8")
        self.assertFalse(config.migrate_legacy_key())
        self.assertFalse(self.user.exists())
        self.assertFalse(config.migration["moved"])

    def test_an_existing_new_key_is_not_overwritten_and_the_old_copy_is_removed(self) -> None:
        config._write_key(self.user, OTHER_KEY)
        self.legacy.write_text(f'OPENAI_API_KEY="{KEY}"\n', encoding="utf-8")
        self.assertTrue(config.migrate_legacy_key())
        self.assertEqual(config._read_key(self.user), OTHER_KEY)
        self.assertNotIn(KEY, self.legacy.read_text(encoding="utf-8"))

    def test_if_the_new_location_is_unwritable_the_key_stays_usable_where_it_is(self) -> None:
        (self.root / "config").write_text("je suis un fichier, pas un dossier", encoding="utf-8")
        self.legacy.write_text(f'OPENAI_API_KEY="{KEY}"\n', encoding="utf-8")
        self.assertFalse(config.migrate_legacy_key())
        self.assertIn(KEY, self.legacy.read_text(encoding="utf-8"))  # jamais perdue
        self.assertEqual(config.get_openai_api_key(), KEY)
        self.assertTrue(config.migration["error"])
        self.assertFalse(config.migration["moved"])

    def test_the_key_is_not_removed_if_the_read_back_differs(self) -> None:
        self.legacy.write_text(f'OPENAI_API_KEY="{KEY}"\n', encoding="utf-8")
        real = config._read_key
        calls = {"n": 0}

        def read(path):
            if path == self.user:
                calls["n"] += 1
                return "" if calls["n"] == 1 else "valeur-corrompue"  # absente, puis relue différente
            return real(path)

        with patch.object(config, "_read_key", read):
            self.assertFalse(config.migrate_legacy_key())
        self.assertIn(KEY, self.legacy.read_text(encoding="utf-8"))

    def test_initialize_directories_runs_the_migration(self) -> None:
        self.legacy.write_text(f'OPENAI_API_KEY="{KEY}"\n', encoding="utf-8")
        with patch.object(config, "JOBS_DIR", self.root / "jobs"):
            config.initialize_directories()
        self.assertEqual(config._read_key(self.user), KEY)


class ReadWriteTests(KeyStorageCase):
    def test_lookup_order_is_user_file_then_old_file_then_environment(self) -> None:
        os.environ["OPENAI_API_KEY"] = "sk-from-the-environment-0000000000"
        self.assertEqual(config.get_openai_api_key(), "sk-from-the-environment-0000000000")
        self.legacy.write_text(f'OPENAI_API_KEY="{OTHER_KEY}"\n', encoding="utf-8")
        self.assertEqual(config.get_openai_api_key(), OTHER_KEY)
        config._write_key(self.user, KEY)
        self.assertEqual(config.get_openai_api_key(), KEY)

    def test_no_key_anywhere(self) -> None:
        self.assertIsNone(config.get_openai_api_key())

    def test_saving_writes_the_new_file_and_cleans_the_old_one(self) -> None:
        self.legacy.write_text(f'OPENAI_API_KEY="{OTHER_KEY}"\nAUTRE=1\n', encoding="utf-8")
        config.save_openai_api_key("  " + KEY + "  ")
        self.assertEqual(config._read_key(self.user), KEY)
        self.assertEqual(self.legacy.read_text(encoding="utf-8"), "AUTRE=1\n")
        self.assertEqual(config.get_openai_api_key(), KEY)

    def test_invalid_keys_are_refused_and_nothing_is_written(self) -> None:
        for value in ("", "pas-une-cle", "sk-court"):
            with self.subTest(value), self.assertRaises(ValueError):
                config.save_openai_api_key(value)
        self.assertFalse(self.user.exists())

    def test_an_unwritable_location_gives_a_readable_error(self) -> None:
        (self.root / "config").write_text("fichier", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            config.save_openai_api_key(KEY)
        self.assertIn("Impossible d'enregistrer la clé", str(caught.exception))


class SyncWarningTests(KeyStorageCase):
    def test_key_in_a_synced_folder_is_reported(self) -> None:
        with patch.object(config, "ENV_FILE", self.root / "OneDrive" / "cfg" / ".env"):
            self.assertEqual(config.key_sync_folder(), "OneDrive")

    def test_an_old_env_left_in_a_synced_application_folder_is_reported_until_migrated(self) -> None:
        synced = self.root / "Dropbox" / "appli"
        synced.mkdir(parents=True)
        legacy = synced / ".env"
        legacy.write_text(f'OPENAI_API_KEY="{KEY}"\n', encoding="utf-8")
        with patch.object(config, "PROJECT_ROOT", synced), patch.object(config, "LEGACY_ENV_FILE", legacy):
            self.assertEqual(config.key_sync_folder(), "Dropbox")
            self.assertTrue(config.migrate_legacy_key())
            self.assertIsNone(config.key_sync_folder())

    def test_nothing_to_report_in_a_normal_setup(self) -> None:
        self.assertIsNone(config.key_sync_folder())


class ApiTests(KeyStorageCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(app, base_url="http://localhost")

    def test_health_reports_the_location_but_never_the_key(self) -> None:
        self.legacy.write_text(f'OPENAI_API_KEY="{KEY}"\n', encoding="utf-8")
        config.migrate_legacy_key()
        data = self.client.get("/api/health").json()
        self.assertTrue(data["api_key_configured"])
        self.assertEqual(data["key_location"], str(self.user))
        self.assertTrue(data["key_migrated"])
        self.assertIsNone(data["key_sync_folder"])
        self.assertNotIn(KEY, str(data))

    def test_saving_through_the_interface_uses_the_new_location(self) -> None:
        response = self.client.post("/api/settings/api-key", json={"api_key": KEY})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(config._read_key(self.user), KEY)
        self.assertFalse(self.legacy.exists())
        self.assertTrue(self.client.get("/api/health").json()["api_key_configured"])

    def test_an_unwritable_location_is_reported_to_the_user(self) -> None:
        (self.root / "config").write_text("fichier", encoding="utf-8")
        response = self.client.post("/api/settings/api-key", json={"api_key": KEY})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Impossible d'enregistrer", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
