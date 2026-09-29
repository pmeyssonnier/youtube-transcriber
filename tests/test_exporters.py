from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.exporters import clean_inline_text, clock, safe_filename_stem, write_exports


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


if __name__ == "__main__":
    unittest.main()
