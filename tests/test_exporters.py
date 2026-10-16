from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from unittest.mock import patch

from app.exporters import clean_inline_text, clock, safe_filename_stem, write_exports, write_files_together


class ExporterTests(unittest.TestCase):
    def test_clock_formats_long_timestamps(self) -> None:
        self.assertEqual(clock(22_265), "06:11:05")
        self.assertEqual(clock(65.432, milliseconds=True), "00:01:05,432")

    def test_cleans_labels_and_windows_filenames(self) -> None:
        self.assertEqual(clean_inline_text("Président\r\nINJECT"), "Président INJECT")
        self.assertEqual(safe_filename_stem('Réunion : budget / 2026?'), "reunion-budget-2026")

    def test_writes_all_formats_and_applies_speaker_names(self) -> None:
        segments = [
            {
                "start": 0.0,
                "end": 2.5,
                "speaker": "P01-A",
                "text": "Bonjour à toutes et à tous.",
                "chunk_index": 0,
            },
            {
                "start": 2.5,
                "end": 5.0,
                "speaker": "P01-B",
                "text": "Goedenavond.",
                "chunk_index": 0,
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            files = write_exports(
                output_dir,
                {"title": "Conseil\ncommunal", "source_url": "https://youtu.be/example"},
                segments,
                {"P01-A": "Présidence\nmunicipale", "P01-B": "Intervenant 2"},
            )
            self.assertEqual(len(files), 5)
            self.assertEqual(
                {path.name for path in output_dir.iterdir()},
                {
                    "transcription.json",
                    "transcription.md",
                    "transcription.txt",
                    "transcription.srt",
                    "transcription.vtt",
                },
            )
            data = json.loads((output_dir / "transcription.json").read_text(encoding="utf-8"))
            self.assertEqual(data["segments"][0]["speaker_name"], "Présidence municipale")
            self.assertIn("[Présidence municipale]", (output_dir / "transcription.srt").read_text(encoding="utf-8"))


class AtomicExportTests(unittest.TestCase):
    """Un arrêt au milieu d'une réécriture ne mélange pas anciens et nouveaux fichiers (audit P2-08)."""

    SEGMENTS = [{"start": 0.0, "end": 2.0, "speaker": "P01-A", "text": "bonjour"}]

    def export(self, directory: Path, text: str) -> None:
        segments = [{**self.SEGMENTS[0], "text": text}]
        write_exports(directory, {"title": "Séance"}, segments, {"P01-A": "Orateur"})

    def test_failure_on_the_third_file_leaves_the_previous_exports_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.export(directory, "ancien texte")
            before = {path.name: path.read_text(encoding="utf-8") for path in directory.iterdir()}
            self.assertEqual(len(before), 5)

            original = Path.write_text
            calls = {"count": 0}

            def failing(self_path, *args, **kwargs):
                calls["count"] += 1
                if calls["count"] == 3:
                    raise OSError("disque plein")
                return original(self_path, *args, **kwargs)

            with patch.object(Path, "write_text", failing):
                with self.assertRaises(OSError):
                    self.export(directory, "nouveau texte")
            after = {path.name: path.read_text(encoding="utf-8") for path in directory.iterdir()}
            self.assertEqual(after, before)  # aucun fichier modifié, aucun fichier temporaire oublié

    def test_successful_rewrite_replaces_all_files_and_leaves_no_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.export(directory, "ancien texte")
            self.export(directory, "nouveau texte")
            self.assertEqual(sorted(path.name for path in directory.iterdir()),
                             ["transcription.json", "transcription.md", "transcription.srt", "transcription.txt", "transcription.vtt"])
            for path in directory.iterdir():
                self.assertIn("nouveau texte", path.read_text(encoding="utf-8"))
                self.assertNotIn("ancien texte", path.read_text(encoding="utf-8"))
            json.loads((directory / "transcription.json").read_text(encoding="utf-8"))

    def test_write_files_together_creates_the_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a" / "b"
            write_files_together(target, {"x.txt": "1", "y.txt": "2"})
            self.assertEqual((target / "y.txt").read_text(encoding="utf-8"), "2")


if __name__ == "__main__":
    unittest.main()
