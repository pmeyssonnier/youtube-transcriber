from __future__ import annotations

import collections
import json
import math
import os
import random
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app, inspections
from app.pipeline import TranscriptionPipeline
from app.speakers import speaker_stats
from app.stitching import LINK_MIN_SECONDS, choose_cut, stitch_segments
from app.store import JobStore


def seg(start: float, end: float, speaker: str, chunk: int = 0) -> dict:
    return {"start": start, "end": end, "speaker": speaker, "text": f"{speaker}@{start}", "chunk_index": chunk}


WINDOWS = [(0.0, 345.0), (300.0, 645.0), (600.0, 900.0)]


class LinkingTests(unittest.TestCase):
    def test_voices_speaking_together_in_the_shared_audio_are_merged(self) -> None:
        first = [seg(0, 120, "P01-A"), seg(130, 300, "P01-B"), seg(300, 330, "P01-A"), seg(332, 345, "P01-C")]
        second = [seg(300, 331, "P02-B", 1), seg(333, 345, "P02-D", 1), seg(346, 500, "P02-D", 1), seg(510, 640, "P02-B", 1)]
        third = [seg(600, 640, "P03-K", 2), seg(641, 650, "P03-L", 2), seg(650, 900, "P03-L", 2)]
        segments, info = stitch_segments([first, second, third], WINDOWS)

        by_raw = {item["raw_speaker"]: item["speaker"] for item in segments}
        self.assertEqual(by_raw["P01-A"], by_raw["P02-B"])  # même voix vue dans les deux parties
        self.assertEqual(by_raw["P02-B"], "Voix 01")  # première apparition
        self.assertNotEqual(by_raw["P01-B"], by_raw["P01-A"])  # jamais entendue dans un passage commun
        self.assertEqual(info["voices"], len({item["speaker"] for item in segments}))
        self.assertLess(info["voices"], info["raw_labels"])

    def test_no_sentence_is_lost_or_repeated_at_the_cut(self) -> None:
        first = [seg(0, 100, "P01-A"), seg(300, 330, "P01-A"), seg(340, 345, "P01-A")]
        second = [seg(300, 331, "P02-B", 1), seg(340, 346, "P02-B", 1), seg(400, 500, "P02-B", 1)]
        segments, _ = stitch_segments([first, second], WINDOWS[:2])
        starts = [item["start"] for item in segments]
        self.assertEqual(len(starts), len(set(starts)))
        # 300-331 et 340-346 : chacun apparaît une seule fois, pas de trou
        covered = [(item["start"], item["end"]) for item in segments if 290 <= item["start"] < 400]
        self.assertEqual(len(covered), 2)

    def test_cut_prefers_a_silence_in_both_transcripts(self) -> None:
        before = [seg(300, 318, "a"), seg(324, 345, "a")]
        after = [seg(300, 319, "b"), seg(322, 345, "b")]
        cut = choose_cut(before, after, 300.0, 345.0)
        self.assertTrue(319 <= cut <= 322, cut)  # entre la fin du dernier segment et le début du suivant

    def test_short_or_ambiguous_overlaps_do_not_merge(self) -> None:
        # 1 s seulement en commun : en dessous du seuil
        short = stitch_segments([[seg(0, 300, "P01-A"), seg(344, 345, "P01-B")], [seg(344, 346, "P02-X", 1), seg(400, 600, "P02-X", 1)]], WINDOWS[:2])[0]
        self.assertEqual(len({item["speaker"] for item in short}), 2)
        self.assertLess(1.0, LINK_MIN_SECONDS)

        # P01-A est scindée en deux voix par la 2e partie : pas de liaison « mutuelle » claire avec la plus courte
        split = stitch_segments(
            [[seg(300, 345, "P01-A")], [seg(300, 312, "P02-X", 1), seg(313, 345, "P02-Y", 1), seg(400, 500, "P02-X", 1)]],
            WINDOWS[:2],
        )[0]
        raw_to_voice = {item["raw_speaker"]: item["speaker"] for item in split}
        self.assertEqual(raw_to_voice["P02-X"] == raw_to_voice.get("P01-A", None), False)

    def test_two_labels_of_the_same_part_never_share_a_voice(self) -> None:
        first = [seg(300, 320, "P01-A"), seg(322, 345, "P01-B")]
        second = [seg(300, 321, "P02-X", 1), seg(323, 345, "P02-X", 1), seg(400, 600, "P02-X", 1)]
        segments, _ = stitch_segments([first, second], WINDOWS[:2])
        voices = collections.defaultdict(set)
        for item in segments:
            voices[item["speaker"]].add((item["raw_speaker"].split("-")[0], item["raw_speaker"]))
        for members in voices.values():
            parts = [part for part, _ in members]
            self.assertEqual(len(parts), len(set(parts)) if len(members) > 1 else 1)

    def test_contiguous_windows_keep_everything_in_order(self) -> None:
        first = [seg(0, 50, "P01-A"), seg(60, 100, "P01-B")]
        second = [seg(100, 150, "P02-A", 1), seg(160, 200, "P02-B", 1)]
        segments, info = stitch_segments([first, second], [(0, 100), (100, 200)])
        self.assertEqual([item["start"] for item in segments], [0, 60, 100, 160])
        self.assertEqual(info["raw_labels"], 4)

    def test_without_relabel_original_labels_are_kept(self) -> None:
        first = [seg(300, 345, "Intervenant")]
        second = [seg(300, 345, "Intervenant", 1), seg(400, 500, "Intervenant", 1)]
        segments, info = stitch_segments([first, second], WINDOWS[:2], relabel=False)
        self.assertEqual({item["speaker"] for item in segments}, {"Intervenant"})
        self.assertNotIn("raw_speaker", segments[0])
        self.assertEqual(info["voices"], 1)


def simulate(seed: int, total: int = 2 * 3600, chunk: int = 1200, overlap: int = 45, speakers: int = 6):
    """Fake meeting: per-part diarization with shuffled labels and jittery segment boundaries."""
    rnd = random.Random(seed)
    weights = [1 / (index + 1) for index in range(speakers)]
    truth, moment = [], 0.0
    while moment < total:
        who = rnd.choices(range(speakers), weights)[0]
        length = rnd.uniform(6, 90)
        truth.append((moment, min(total, moment + length), who))
        moment += length + rnd.uniform(0.3, 3)
    starts = [index * chunk for index in range(math.ceil(total / chunk))]
    windows = [(start, min(total, start + chunk + (overlap if index < len(starts) - 1 else 0))) for index, start in enumerate(starts)]
    results = []
    for index, (low, high) in enumerate(windows):
        letters = list("ABCDEFGHIJ")
        rnd.shuffle(letters)
        labels: dict[int, str] = {}
        segments = []
        for start, end, who in truth:
            if end <= low or start >= high:
                continue
            cursor, stop = max(start, low), min(end, high)
            while cursor < stop - 0.2:
                piece_end = min(stop, cursor + rnd.uniform(3, 15))
                label = labels.setdefault(who, f"P{index + 1:02d}-{letters[len(labels) % 10]}")
                segments.append({
                    "start": max(low, cursor + rnd.uniform(-1, 1)), "end": min(high, piece_end + rnd.uniform(-1, 1)),
                    "speaker": label, "text": "", "chunk_index": index, "truth": who,
                })
                cursor = piece_end
        results.append(segments)
    return truth, windows, results


class SimulatedMeetingTests(unittest.TestCase):
    def test_merging_reduces_labels_without_mixing_people_or_losing_speech(self) -> None:
        for seed in range(3):
            with self.subTest(seed=seed):
                truth, windows, results = simulate(seed)
                segments, info = stitch_segments(results, windows)
                self.assertLess(info["voices"], info["raw_labels"])  # le regroupement apporte un gain

                by_voice = collections.defaultdict(collections.Counter)
                for item in segments:
                    by_voice[item["speaker"]][item["truth"]] += item["end"] - item["start"]
                total = sum(sum(counter.values()) for counter in by_voice.values())
                wrong = sum(sum(counter.values()) - max(counter.values()) for counter in by_voice.values())
                self.assertLess(wrong / total, 0.03)  # moins de 3 % de parole attribuée à une voix mélangée

                spoken = sum(end - start for start, end, _ in truth)
                self.assertGreater(total / spoken, 0.97)  # rien de perdu
                self.assertLess(total / spoken, 1.03)  # rien de répété


class PipelineOverlapTests(unittest.TestCase):
    def test_chunk_plan(self) -> None:
        plan = TranscriptionPipeline._chunk_plan(0.0, 3000.0, 1200, 45.0)
        self.assertEqual(plan, [(0.0, 1245.0), (1200.0, 1245.0), (2400.0, 600.0)])
        # une dernière partie trop courte est absorbée par la précédente
        plan = TranscriptionPipeline._chunk_plan(0.0, 2410.0, 1200, 45.0)
        self.assertEqual(plan, [(0.0, 1245.0), (1200.0, 1210.0)])
        # plage partielle : les positions restent celles de la vidéo
        plan = TranscriptionPipeline._chunk_plan(600.0, 2100.0, 1200, 0.0)
        self.assertEqual(plan, [(600.0, 1200.0), (1800.0, 300.0)])

    def test_split_with_overlap_cuts_each_part_and_records_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "source.m4a"
            audio.write_bytes(b"audio")
            chunks_dir, results_dir = root / "chunks", root / "results"
            chunks_dir.mkdir()
            results_dir.mkdir()
            pipeline = TranscriptionPipeline(JobStore())
            commands: list[list[str]] = []

            def fake_run(command, message, timeout=None):
                commands.append(command)
                Path(command[-1]).write_bytes(b"x")
                return ""

            with (
                patch.object(pipeline, "_duration", return_value=3000.0),
                patch.object(pipeline, "_run_command", side_effect=fake_run),
            ):
                chunks = pipeline._split_audio(audio, chunks_dir, results_dir, 1200, None, None, 45.0)
                self.assertEqual(len(chunks), 3)
                self.assertEqual(
                    [(c[c.index("-ss") + 1], c[c.index("-t") + 1]) for c in commands],
                    [("0.000", "1245.000"), ("1200.000", "1245.000"), ("2400.000", "600.000")],
                )
                self.assertEqual(pipeline._planned_offsets(chunks_dir, 3), [0.0, 1200.0, 2400.0])
                self.assertIsNone(pipeline._planned_offsets(chunks_dir, 2))

                # mêmes paramètres : réutilisation sans nouveau découpage
                before = len(commands)
                pipeline._split_audio(audio, chunks_dir, results_dir, 1200, None, None, 45.0)
                self.assertEqual(len(commands), before)

                # recouvrement différent : tout est redécoupé et les anciens résultats sont effacés
                (results_dir / "chunk_000.json").write_text("{}")
                pipeline._split_audio(audio, chunks_dir, results_dir, 1200, None, None, 180.0)
                self.assertGreater(len(commands), before)
                self.assertFalse((results_dir / "chunk_000.json").exists())
                self.assertEqual(pipeline._planned_offsets(chunks_dir, 3)[1], 1200.0)
            pipeline.shutdown()

    def test_run_merges_voices_across_overlapping_parts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            audio = root / "source.m4a"
            audio.write_bytes(b"audio")
            chunks = []
            for index in range(2):
                chunk = root / f"chunk_{index:03d}.mp3"
                chunk.write_bytes(b"c")
                chunks.append(chunk)
            with (
                patch("app.store.JOBS_DIR", jobs_dir),
                patch("app.pipeline.JOBS_DIR", jobs_dir),
                patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key-for-unit-tests-only"}),
            ):
                store = JobStore()
                job_id = "8" * 32
                store.create({
                    "id": job_id, "url": "https://youtu.be/euuLzgjj7z4", "video_identity": "euuLzgjj7z4",
                    "title": "t", "duration": 1500.0, "status": "queued", "progress": 0, "message": "",
                    "diarize": True, "speaker_linking": "light", "chunk_minutes": 20, "api_concurrency": 1,
                    "segments": [], "speaker_names": {}, "files": [],
                })
                chunks_dir = jobs_dir / job_id / "chunks"
                chunks_dir.mkdir()
                (chunks_dir / "manifest.json").write_text(json.dumps({"offsets": [0.0, 1200.0]}))
                pipeline = TranscriptionPipeline(store)

                def transcribe(_client, _chunk, index, _diarize):
                    local = [  # partie 1 : 0-1245 s ; partie 2 : démarre à 1200 s de la vidéo (0 s dans son audio)
                        [(0, 600, "P01-A"), (1200, 1230, "P01-A"), (1232, 1245, "P01-B")],
                        [(0, 31, "P02-X"), (33, 45, "P02-Y"), (46, 300, "P02-Y"), (500, 560, "P02-X")],
                    ][index]
                    return [
                        {"start": float(a), "end": float(b), "speaker": label, "text": label, "chunk_index": index}
                        for a, b, label in local
                    ]

                with (
                    patch.object(pipeline, "_existing_audio", return_value=audio),
                    patch.object(pipeline, "_split_audio", return_value=chunks),
                    patch.object(pipeline, "_duration", side_effect=[1245.0, 1245.0, 1245.0, 300.0, 1245.0, 300.0, 1245.0, 300.0]),
                    patch.object(pipeline, "_transcribe_chunk", side_effect=transcribe),
                    patch("app.pipeline.OpenAI", return_value=object()),
                ):
                    pipeline._run(job_id)
                result = store.get(job_id)
                self.assertEqual(result["status"], "completed")
                voices = {item["raw_speaker"]: item["speaker"] for item in result["segments"]}
                self.assertEqual(voices.get("P01-A"), "Voix 01")
                self.assertIn("→", result["message"])
                self.assertLess(result["speaker_labels_merged"], result["speaker_labels_raw"])
                pipeline.shutdown()


class LinkingApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, base_url="http://localhost")

    def _create(self, **extra):
        inspection_id = "b2" * 16
        inspections[inspection_id] = {
            "id": inspection_id, "video_identity": "euuLzgjj7z4", "title": "T", "duration": 600.0,
            "url": "https://youtu.be/euuLzgjj7z4", "cookie_browser": None, "chunk_minutes": 10,
            "created_monotonic": time.monotonic(), "requires_confirmation": False,
        }
        body = {"url": "https://youtu.be/euuLzgjj7z4", "inspection_id": inspection_id, "chunk_minutes": 10, **extra}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("app.store.JOBS_DIR", Path(directory)),
            patch("app.main.get_openai_api_key", return_value="sk-test-key-for-unit-tests-only"),
            patch("app.main.pipeline.submit", return_value=True),
        ):
            return self.client.post("/api/jobs", json=body)

    def test_mode_is_stored_and_disabled_without_diarization(self) -> None:
        self.assertEqual(self._create().json()["speaker_linking"], "light")
        self.assertEqual(self._create(speaker_linking="strong").json()["speaker_linking"], "strong")
        self.assertEqual(self._create(diarize=False, speaker_linking="strong").json()["speaker_linking"], "off")
        self.assertEqual(self._create(speaker_linking="extreme").status_code, 422)

    def test_page_offers_the_linking_choice(self) -> None:
        html = self.client.get("/").text
        self.assertIn('id="speakerLinking"', html)
        self.assertIn('value="strong"', html)


class SpeakerStatsPartsTests(unittest.TestCase):
    def test_stats_count_the_parts_a_voice_appears_in(self) -> None:
        segments = [
            {**seg(0, 10, "Voix 01", 0), "text": "un deux trois quatre cinq six sept"},
            {**seg(1300, 1310, "Voix 01", 1), "text": "un deux trois quatre cinq six sept"},
            {**seg(2500, 2510, "Voix 02", 2), "text": "un deux trois quatre cinq six sept"},
        ]
        stats = {item["label"]: item for item in speaker_stats(segments, {})}
        self.assertEqual(stats["Voix 01"]["parts"], 2)
        self.assertEqual(stats["Voix 02"]["parts"], 1)


if __name__ == "__main__":
    unittest.main()
