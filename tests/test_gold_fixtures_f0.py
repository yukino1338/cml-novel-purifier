from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"
FIXTURE_ROOT = TESTS_DIR / "fixtures"
_AUXILIARY_FIXTURE_DIRECTORIES = frozenset({"forward_trials", "forward_evidence_v1"})
sys.path.insert(0, str(TESTS_DIR))

from fixture_factory import (  # noqa: E402
    PROVENANCE,
    build_fixture_bundle,
    make_candidate_explosion,
    make_candidate_explosion_fixture,
    make_large_novel,
    sha256_bytes,
    sha256_text,
)


def parse_jsonl_independently(
    path: Path,
) -> tuple[list[dict[str, object]], tuple[str, int] | None]:
    records: list[dict[str, object]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        1,
    ):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return records, ("invalid_json", line_number)
        if not isinstance(record, dict):
            return records, ("non_object_record", line_number)
        records.append(record)
    return records, None


class GoldFixtureF0Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest_path = FIXTURE_ROOT / "gold_manifest.json"
        cls.manifest = json.loads(cls.manifest_path.read_text(encoding="utf-8"))

    def test_manifest_has_required_anonymous_gold_counts(self) -> None:
        summary = self.manifest["summary"]
        self.assertEqual(self.manifest["schema_version"], 1)
        self.assertEqual(self.manifest["provenance"], PROVENANCE)
        self.assertGreaterEqual(summary["explicit_ad_count"], 40)
        self.assertGreaterEqual(summary["hard_negative_count"], 40)
        self.assertGreaterEqual(summary["document_count"], 6)
        self.assertGreaterEqual(summary["encoding_case_count"], 3)
        self.assertGreaterEqual(summary["malformed_jsonl_case_count"], 3)

        cases = self.manifest["candidate_catalog"]
        self.assertEqual(len(cases), 96)
        self.assertEqual(len({case["case_id"] for case in cases}), len(cases))
        self.assertEqual(
            sum(case["classification"] == "explicit_ad" for case in cases),
            48,
        )
        self.assertEqual(
            sum(case["classification"] == "narrative" for case in cases),
            48,
        )
        self.assertEqual(summary["must_surface_count"], 48)
        self.assertEqual(summary["must_suppress_count"], 24)
        self.assertEqual(summary["may_surface_count"], 24)
        self.assertEqual(
            self.manifest["offset_contract"]["unit"],
            "python_unicode_code_point",
        )
        self.assertEqual(
            self.manifest["offset_contract"]["interval"],
            "half_open",
        )
        positive_families = {
            case["evidence_family"]
            for case in cases
            if case["classification"] == "explicit_ad"
        }
        negative_families = {
            case["evidence_family"]
            for case in cases
            if case["classification"] == "narrative"
        }
        self.assertGreaterEqual(len(positive_families), 8)
        self.assertGreaterEqual(len(negative_families), 8)

        must_suppress = [
            case
            for case in cases
            if case["surface_expectation"] == "must_suppress"
        ]
        normalized = {
            re.sub(r"\d+", "#", case["text"])
            for case in must_suppress
        }
        self.assertEqual(len(normalized), len(must_suppress))

    def test_candidate_catalog_freezes_span_action_chapter_and_hashes(self) -> None:
        valid_chapters = {f"chapter-{index}" for index in range(1, 5)}
        for case in self.manifest["candidate_catalog"]:
            with self.subTest(case_id=case["case_id"]):
                text = case["text"]
                span = case["span"]
                self.assertEqual(span, {"start": 0, "end": len(text)})
                self.assertEqual(text[span["start"] : span["end"]], text)
                self.assertIn(case["expected_chapter"], valid_chapters)
                self.assertEqual(case["provenance"], PROVENANCE)
                self.assertEqual(case["input_sha256"], sha256_text(text))

                if case["classification"] == "explicit_ad":
                    self.assertEqual(case["allowed_actions"], ["delete"])
                    self.assertEqual(case["expected_action"], "delete")
                    expected_output = ""
                    self.assertEqual(case["surface_expectation"], "must_surface")
                else:
                    self.assertEqual(case["allowed_actions"], ["keep"])
                    self.assertEqual(case["expected_action"], "keep")
                    expected_output = text
                    self.assertIn(
                        case["surface_expectation"],
                        {"must_suppress", "may_surface"},
                    )
                self.assertTrue(case["evidence_tags"])
                self.assertEqual(
                    case["expected_output_sha256"],
                    sha256_text(expected_output),
                )

    def test_document_hashes_chapters_and_candidate_offsets_are_exact(self) -> None:
        for document in self.manifest["documents"]:
            with self.subTest(document=document["document_id"]):
                path = FIXTURE_ROOT / document["path"]
                text = path.read_text(encoding="utf-8")
                self.assertEqual(document["input_sha256"], sha256_text(text))
                self.assertEqual(
                    document["expected_chapter_count"],
                    len(document["expected_chapters"]),
                )

                previous_end = 0
                chapter_by_id: dict[str, dict[str, object]] = {}
                for chapter in document["expected_chapters"]:
                    self.assertGreaterEqual(chapter["start"], previous_end)
                    self.assertGreater(chapter["end"], chapter["start"])
                    self.assertTrue(
                        text[chapter["start"] : chapter["end"]].startswith(
                            chapter["title"] + "\n"
                        )
                    )
                    chapter_by_id[chapter["chapter_id"]] = chapter
                    previous_end = chapter["end"]

                delete_ranges: list[tuple[int, int]] = []
                anchor_ids: list[str] = []
                for candidate in document["candidates"]:
                    originals: list[str] = []
                    candidate_delete_ranges: list[tuple[int, int]] = []
                    for span in candidate["spans"]:
                        anchor_ids.append(span["anchor_id"])
                        original = span["original"]
                        originals.append(original)
                        self.assertEqual(text[span["start"] : span["end"]], original)
                        self.assertEqual(
                            span["line"],
                            text.count("\n", 0, span["start"]) + 1,
                        )
                        self.assertEqual(
                            span["prefix"],
                            text[max(0, span["start"] - 24) : span["start"]],
                        )
                        self.assertEqual(
                            span["suffix"],
                            text[
                                span["end"] : min(len(text), span["end"] + 24)
                            ],
                        )
                        chapter = chapter_by_id[span["chapter_id"]]
                        self.assertLessEqual(chapter["start"], span["start"])
                        self.assertLess(span["start"], chapter["end"])
                        self.assertEqual(
                            span["expected_locator"],
                            {
                                "kind": "chapter",
                                "chapter_id": span["chapter_id"],
                                "title": chapter["title"],
                            },
                        )
                        if candidate["expected_action"] == "delete":
                            mutation = span["mutation"]
                            self.assertEqual(
                                text[mutation["start"] : mutation["end"]],
                                original + "\n",
                            )
                            mutation_range = (mutation["start"], mutation["end"])
                            delete_ranges.append(mutation_range)
                            candidate_delete_ranges.append(mutation_range)
                        else:
                            self.assertIsNone(span["mutation"])

                    expected_input = "\n".join(originals)
                    self.assertEqual(
                        candidate["candidate_text_sha256"],
                        sha256_text(expected_input),
                    )
                    self.assertEqual(candidate["input_sha256"], sha256_text(text))
                    expected_candidate_output = text
                    for start, end in sorted(candidate_delete_ranges, reverse=True):
                        expected_candidate_output = (
                            expected_candidate_output[:start]
                            + expected_candidate_output[end:]
                        )
                    self.assertEqual(
                        candidate["expected_output_sha256"],
                        sha256_text(expected_candidate_output),
                    )
                self.assertEqual(len(anchor_ids), len(set(anchor_ids)))
                ordered_delete_ranges = sorted(delete_ranges)
                for previous, current in zip(
                    ordered_delete_ranges,
                    ordered_delete_ranges[1:],
                ):
                    self.assertLessEqual(previous[1], current[0])

                segments = document["expected_segments"]
                self.assertTrue(segments)
                segment_cursor = 0
                for segment in segments:
                    self.assertEqual(segment["start"], segment_cursor)
                    self.assertGreaterEqual(segment["end"], segment["start"])
                    self.assertEqual(
                        segment["text_sha256"],
                        sha256_text(text[segment["start"] : segment["end"]]),
                    )
                    segment_cursor = segment["end"]
                self.assertEqual(segment_cursor, len(text))

                expected_output = text
                for start, end in sorted(delete_ranges, reverse=True):
                    expected_output = expected_output[:start] + expected_output[end:]
                self.assertEqual(
                    document["expected_output_sha256"],
                    sha256_text(expected_output),
                )
                expected_output_path = document.get("expected_output_path")
                if expected_output_path:
                    self.assertEqual(
                        (FIXTURE_ROOT / expected_output_path).read_text(
                            encoding="utf-8"
                        ),
                        expected_output,
                    )
                else:
                    self.assertEqual(expected_output, text)

    def test_front_matter_and_no_chapter_contracts_are_explicit(self) -> None:
        documents = {
            document["document_id"]: document
            for document in self.manifest["documents"]
        }
        front = documents["front-matter"]
        text = (FIXTURE_ROOT / front["path"]).read_text(encoding="utf-8")
        span = front["front_matter_span"]
        self.assertGreater(span["end"], span["start"])
        self.assertTrue(text[span["start"] : span["end"]].startswith("匿名前置内容"))
        self.assertEqual(front["expected_chapter_count"], 2)

        no_chapters = documents["no-chapters"]
        self.assertEqual(no_chapters["expected_chapter_count"], 0)
        self.assertEqual(no_chapters["expected_chapters"], [])
        self.assertEqual(
            no_chapters["input_sha256"],
            no_chapters["expected_output_sha256"],
        )

    def test_encoding_fixtures_have_frozen_bytes_and_strict_decoding(self) -> None:
        for case in self.manifest["encoding_cases"]:
            with self.subTest(case_id=case["case_id"]):
                payload = (FIXTURE_ROOT / case["path"]).read_bytes()
                self.assertEqual(case["encoded_sha256"], sha256_bytes(payload))
                decoded = payload.decode(case["encoding"], errors="strict")
                self.assertEqual(decoded, case["decoded_text"])
                self.assertEqual(
                    case["decoded_text_sha256"],
                    sha256_text(decoded),
                )
                normalized = decoded.replace("\r\n", "\n").replace("\r", "\n")
                self.assertEqual(normalized, case["expected_normalized_text"])
                self.assertEqual(
                    case["expected_output_sha256"],
                    sha256_text(normalized),
                )

        for case in self.manifest["blocked_encoding_cases"]:
            with self.subTest(case_id=case["case_id"]):
                payload = bytes.fromhex(case["raw_hex"])
                self.assertEqual(case["input_sha256"], sha256_bytes(payload))
                successful_decodes: list[str] = []
                for encoding in ("utf-8", "gb18030", "big5"):
                    try:
                        successful_decodes.append(
                            payload.decode(encoding, errors="strict")
                        )
                    except UnicodeDecodeError:
                        continue
                if case["expected_reason"] == "ambiguous_strict_decoding":
                    self.assertGreaterEqual(len(successful_decodes), 2)
                    self.assertGreaterEqual(len(set(successful_decodes)), 2)
                else:
                    self.assertEqual(successful_decodes, [])

    def test_malformed_jsonl_fixtures_are_invalid_for_the_declared_reason(self) -> None:
        for case in self.manifest["malformed_jsonl_cases"]:
            with self.subTest(case_id=case["case_id"]):
                path = FIXTURE_ROOT / case["path"]
                self.assertEqual(case["input_sha256"], sha256_bytes(path.read_bytes()))
                records, load_error = parse_jsonl_independently(path)
                if case["expected_result"] == "load_error":
                    self.assertEqual(
                        load_error,
                        (
                            case["expected_error_kind"],
                            case["expected_error_line"],
                        ),
                    )
                    continue

                self.assertIsNone(load_error)
                if case["expected_result"] == "ok":
                    self.assertEqual(len(records), 2)
                elif case["expected_error_kind"] == "duplicate_candidate_id":
                    ids = [record["candidate_id"] for record in records]
                    self.assertNotEqual(len(ids), len(set(ids)))
                elif case["expected_error_kind"] == "anchors_not_array":
                    self.assertNotIsInstance(records[0]["anchors"], list)
                elif case["expected_error_kind"] == "negative_offset":
                    self.assertLess(records[0]["anchors"][0]["offset"], 0)
                else:
                    self.fail(f"unhandled semantic fixture: {case}")

    def test_all_declared_artifact_hashes_match_bytes(self) -> None:
        self.assertTrue(self.manifest["artifacts"])
        for relative_path, artifact in self.manifest["artifacts"].items():
            with self.subTest(path=relative_path):
                relative = Path(relative_path)
                self.assertFalse(relative.is_absolute())
                self.assertNotIn("..", relative.parts)
                payload = (FIXTURE_ROOT / relative_path).read_bytes()
                self.assertEqual(artifact["size_bytes"], len(payload))
                self.assertEqual(artifact["sha256"], sha256_bytes(payload))

    def test_layout_tokens_and_rollback_outcomes_are_frozen(self) -> None:
        documents = {
            document["document_id"]: document
            for document in self.manifest["documents"]
        }
        layout = documents["layout-tokens"]
        layout_text = (FIXTURE_ROOT / layout["path"]).read_text(encoding="utf-8")
        for token in layout["must_preserve"]:
            self.assertEqual(layout_text.count(token["token"]), token["input_count"])
            self.assertEqual(
                token["input_count"],
                token["expected_output_count"],
            )
        self.assertTrue(layout["layout_invariants"]["protected_tokens_unchanged"])
        self.assertTrue(layout["layout_invariants"]["author_note_kept_once"])

        rollback = documents["rollback"]
        all_anchor_ids = {
            span["anchor_id"]
            for candidate in rollback["candidates"]
            for span in candidate["spans"]
        }
        outcomes = {
            outcome["outcome_id"]: outcome
            for outcome in rollback["rollback_outcomes"]
        }
        self.assertEqual(
            set(outcomes),
            {
                "all",
                "module-ads",
                "chapter-1",
                "chapter-2",
                "point-shared",
                "point-single",
            },
        )
        original = (FIXTURE_ROOT / rollback["path"]).read_text(encoding="utf-8")
        for outcome in outcomes.values():
            output = (FIXTURE_ROOT / outcome["path"]).read_text(encoding="utf-8")
            self.assertEqual(outcome["sha256"], sha256_text(output))
            restored = set(outcome["restored_anchor_ids"])
            remaining = set(outcome["remaining_deleted_anchor_ids"])
            self.assertFalse(restored & remaining)
            self.assertEqual(restored | remaining, all_anchor_ids)
            self.assertEqual(
                outcome["invalidated_stages"],
                ["5_layout", "6_verify", "7_export", "review"],
            )
        self.assertEqual(
            (FIXTURE_ROOT / outcomes["all"]["path"]).read_text(encoding="utf-8"),
            original,
        )
        self.assertEqual(
            (FIXTURE_ROOT / outcomes["module-ads"]["path"]).read_text(
                encoding="utf-8"
            ),
            original,
        )

    def test_fixture_bundle_rebuild_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rebuilt_root = Path(tmp) / "fixtures"
            rebuilt = build_fixture_bundle(rebuilt_root)
            self.assertEqual(rebuilt, self.manifest)
            expected_files = set(self.manifest["bundle_files"])
            actual_files = {
                str(path.relative_to(FIXTURE_ROOT)).replace("\\", "/")
                for path in FIXTURE_ROOT.rglob("*")
                if path.is_file()
                and not (
                    _AUXILIARY_FIXTURE_DIRECTORIES
                    & set(path.relative_to(FIXTURE_ROOT).parts)
                )
            }
            rebuilt_files = {
                str(path.relative_to(rebuilt_root)).replace("\\", "/")
                for path in rebuilt_root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(actual_files, expected_files)
            self.assertEqual(rebuilt_files, expected_files)
            for relative_path in expected_files:
                self.assertEqual(
                    (FIXTURE_ROOT / relative_path).read_bytes(),
                    (rebuilt_root / relative_path).read_bytes(),
                    relative_path,
                )

    def test_large_and_candidate_dense_inputs_are_runtime_only_and_deterministic(
        self,
    ) -> None:
        generators = self.manifest["runtime_generators"]
        large_spec = generators["large_novel"]
        first_large = make_large_novel(
            large_spec["minimum_chars"],
            large_spec["seed"],
        )
        second_large = make_large_novel(
            large_spec["minimum_chars"],
            large_spec["seed"],
        )
        self.assertEqual(first_large, second_large)
        self.assertEqual(len(first_large), large_spec["char_count"])
        self.assertEqual(sha256_text(first_large), large_spec["sha256"])

        explosion_spec = generators["candidate_explosion"]
        explosion = make_candidate_explosion(explosion_spec["candidate_count"])
        explosion_fixture = make_candidate_explosion_fixture(
            explosion_spec["candidate_count"]
        )
        second_explosion_fixture = make_candidate_explosion_fixture(
            explosion_spec["candidate_count"]
        )
        self.assertEqual(explosion_fixture, second_explosion_fixture)
        self.assertEqual(explosion_fixture["text"], explosion)
        self.assertEqual(len(explosion), explosion_spec["char_count"])
        self.assertEqual(sha256_text(explosion), explosion_spec["sha256"])
        self.assertEqual(
            explosion.count("https://reader.example.com/bulk/"),
            explosion_spec["candidate_count"],
        )
        catalog_payload = json.dumps(
            explosion_fixture["candidates"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertEqual(
            sha256_text(catalog_payload),
            explosion_spec["candidate_catalog_sha256"],
        )
        self.assertEqual(
            len(explosion_fixture["candidates"]),
            explosion_spec["candidate_count"],
        )
        for candidate in explosion_fixture["candidates"]:
            span = candidate["span"]
            self.assertEqual(
                explosion[span["start"] : span["end"]],
                span["original"],
            )
            mutation = span["mutation"]
            expected_output = (
                explosion[: mutation["start"]]
                + explosion[mutation["end"] :]
            )
            self.assertEqual(
                candidate["expected_output_sha256"],
                sha256_text(expected_output),
            )

        committed_files = [
            path
            for path in FIXTURE_ROOT.rglob("*")
            if path.is_file()
        ]
        self.assertTrue(committed_files)
        self.assertLess(
            max(path.stat().st_size for path in committed_files),
            256 * 1024,
        )

    def test_public_fixture_text_is_synthetic_and_path_neutral(self) -> None:
        for path in FIXTURE_ROOT.rglob("*"):
            if not path.is_file():
                continue
            payload = path.read_bytes()
            if path.suffix in {".txt", ".json", ".jsonl"}:
                decoded = None
                for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
                    try:
                        decoded = payload.decode(encoding, errors="strict")
                        break
                    except UnicodeDecodeError:
                        continue
                self.assertIsNotNone(decoded, path)
                assert decoded is not None
                self.assertNotRegex(decoded, r"《[^》]{1,80}》")
                self.assertNotIn(str(ROOT), decoded)
                self.assertNotRegex(decoded, r"(?i)[a-z]:\\users\\")
                self.assertNotRegex(decoded, r"(?i)[a-z]:\\cml\\")
                if path.suffix == ".txt":
                    for host in re.findall(r"https?://([^/\s\"”]+)", decoded):
                        self.assertTrue(
                            host.lower().endswith(
                                ("example.com", "example.net", "example.org")
                            ),
                            host,
                        )

        manifest_text = self.manifest_path.read_text(encoding="utf-8")
        self.assertEqual(
            manifest_text.count('"provenance":'),
            manifest_text.count(f'"provenance": "{PROVENANCE}"'),
        )


if __name__ == "__main__":
    unittest.main()
