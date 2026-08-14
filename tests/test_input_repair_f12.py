from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402
import input_repair  # noqa: E402
import preprocess  # noqa: E402


STORY_BEFORE = "第一章 起点\n正文甲。\n"
STORY_AFTER = "正文乙。\n"
# U+0800 makes this otherwise valid UTF-8 line fail strict GB18030 decoding.
# That gives the repair workflow a deterministic, detectable mixed-encoding case.
AD_LINE = "防止失联，请访问 https://reader.example.com/update \u0800\n"


class InputRepairF12Tests(unittest.TestCase):
    def make_mixed_workspace(self, root: Path) -> tuple[Path, Path, bytes]:
        source = root / "mixed.txt"
        raw = (
            STORY_BEFORE.encode("gb18030")
            + AD_LINE.encode("utf-8")
            + STORY_AFTER.encode("gb18030")
        )
        source.write_bytes(raw)
        workspace = preprocess.run(source, encoding="gb18030")
        report = json.loads(
            (workspace / "report/preprocess_report.json").read_text(encoding="utf-8")
        )
        self.assertTrue(report["encoding_detection"]["blocked"])
        return source, workspace, raw

    def write_plan(self, workspace: Path) -> tuple[dict, dict]:
        candidates_path = workspace / "input_repair/repair_candidates.json"
        candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
        candidate = next(
            item
            for item in candidates["candidates"]
            if item["drop_full_physical_line_allowed"]
        )
        action = {
            "candidate_id": candidate["candidate_id"],
            "action": "drop_full_physical_line",
            "byte_start": candidate["byte_start"],
            "byte_end": candidate["byte_end"],
            "newline_hex": candidate["newline_hex"],
            "line_sha256": candidate["line_sha256"],
            "decoded_text_sha256": candidate["decoded_text_sha256"],
            "decoded_encoding": candidate["selected_encoding"],
            "user_confirmed": True,
            "confirmation": input_repair.CONFIRMATION,
        }
        plan = {
            "schema": input_repair.PLAN_SCHEMA,
            "source_sha256": candidates["source"]["sha256"],
            "candidates_report_sha256": common.sha256_file(candidates_path),
            "primary_encoding": candidates["primary_encoding"],
            "actions": [action],
        }
        common.write_json(workspace / "input_repair/repair_plan.json", plan)
        return plan, candidate

    def test_confirmed_full_ad_line_creates_derived_bytes_and_preprocesses_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, workspace, raw = self.make_mixed_workspace(Path(directory))
            inspect = input_repair.inspect_workspace(workspace, "gb18030")
            self.assertEqual(inspect["drop_allowed_count"], 1)
            _plan, candidate = self.write_plan(workspace)
            self.assertEqual(candidate["anomaly_type"], "primary_decode_failed")

            report = input_repair.apply_plan(workspace)
            prepared = (workspace / "versions/v0_prepared_input.txt").read_bytes()
            expected = raw[: candidate["byte_start"]] + raw[candidate["byte_end"] :]

            self.assertEqual(report["status"], "prepared")
            self.assertEqual(prepared, expected)
            self.assertEqual(source.read_bytes(), raw)
            self.assertEqual(
                (workspace / "versions/v0_original.txt").read_bytes(), raw
            )

            preprocess.run(
                source,
                encoding="gb18030",
                use_prepared_input=True,
            )
            final_report = json.loads(
                (workspace / "report/preprocess_report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                (workspace / "versions/v1_preprocessed.txt").read_text(
                    encoding="utf-8"
                ),
                STORY_BEFORE + STORY_AFTER,
            )
            self.assertTrue(final_report["preprocess_input"]["prepared"])
            self.assertEqual(
                final_report["source_identity"]["sha256"],
                hashlib.sha256(raw).hexdigest(),
            )

    def test_no_plan_keeps_default_preprocess_blocked_and_writes_no_prepared_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _source, workspace, _raw = self.make_mixed_workspace(Path(directory))
            input_repair.inspect_workspace(workspace, "gb18030")

            self.assertFalse(
                (workspace / "versions/v0_prepared_input.txt").exists()
            )
            manifest = json.loads(
                (workspace / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["current_head"], "versions/v0_original.txt")

    def test_story_mixed_line_is_never_eligible_for_full_line_drop(self) -> None:
        narrative = "“请访问 https://reader.example.com/update \u0800”，他读完纸条后收起。\n"
        raw = STORY_BEFORE.encode("gb18030") + narrative.encode("utf-8")
        result = input_repair.build_candidates(
            raw,
            "gb18030",
            input_repair.DEFAULT_CANDIDATE_ENCODINGS,
        )

        self.assertTrue(result["candidates"])
        candidate = result["candidates"][0]
        self.assertFalse(candidate["drop_full_physical_line_allowed"])
        self.assertIn("narrative_context", candidate["drop_blockers"])

    def test_any_plan_binding_change_fails_without_publishing_prepared_input(self) -> None:
        mutators = (
            lambda plan: plan.update(source_sha256="0" * 64),
            lambda plan: plan.update(candidates_report_sha256="0" * 64),
            lambda plan: plan.update(primary_encoding="utf-8"),
            lambda plan: plan["actions"][0].update(byte_start=1),
            lambda plan: plan["actions"][0].update(newline_hex=""),
            lambda plan: plan["actions"][0].update(line_sha256="0" * 64),
            lambda plan: plan["actions"][0].update(decoded_text_sha256="0" * 64),
            lambda plan: plan["actions"][0].update(user_confirmed=False),
            lambda plan: plan["actions"][0].update(confirmation="yes"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, mutate in enumerate(mutators):
                with self.subTest(index=index):
                    case = root / str(index)
                    case.mkdir()
                    _source, workspace, _raw = self.make_mixed_workspace(case)
                    input_repair.inspect_workspace(workspace, "gb18030")
                    plan, _candidate = self.write_plan(workspace)
                    changed = copy.deepcopy(plan)
                    mutate(changed)
                    common.write_json(
                        workspace / "input_repair/repair_plan.json", changed
                    )
                    manifest_before = (workspace / "manifest.json").read_bytes()
                    report_before = (
                        workspace / "report/input_repair_report.json"
                    ).read_bytes()

                    with self.assertRaises(ValueError):
                        input_repair.apply_plan(workspace)

                    self.assertEqual(
                        (workspace / "manifest.json").read_bytes(), manifest_before
                    )
                    self.assertEqual(
                        (workspace / "report/input_repair_report.json").read_bytes(),
                        report_before,
                    )
                    self.assertFalse(
                        (workspace / "versions/v0_prepared_input.txt").exists()
                    )

    def test_nonblocked_workspace_cannot_enter_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "clean.txt"
            source.write_text("第一章\n正文。\n", encoding="utf-8")
            workspace = preprocess.run(source, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "requires a blocked"):
                input_repair.inspect_workspace(workspace, "utf-8")

            self.assertFalse(
                (workspace / "input_repair/repair_candidates.json").exists()
            )

    def test_tampered_candidates_are_recomputed_before_plan_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _source, workspace, _raw = self.make_mixed_workspace(Path(directory))
            input_repair.inspect_workspace(workspace, "gb18030")
            plan, _candidate = self.write_plan(workspace)
            candidates_path = workspace / "input_repair/repair_candidates.json"
            candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
            candidates["candidates"][0]["drop_full_physical_line_allowed"] = False
            common.write_json(candidates_path, candidates)
            plan["candidates_report_sha256"] = common.sha256_file(candidates_path)
            common.write_json(workspace / "input_repair/repair_plan.json", plan)

            with self.assertRaisesRegex(ValueError, "fresh inspection"):
                input_repair.apply_plan(workspace)

            self.assertFalse(
                (workspace / "versions/v0_prepared_input.txt").exists()
            )

    def test_inspection_discloses_legal_byte_detection_limit(self) -> None:
        raw = (STORY_BEFORE + STORY_AFTER).encode("gb18030")
        report = input_repair.build_candidates(
            raw,
            "gb18030",
            input_repair.DEFAULT_CANDIDATE_ENCODINGS,
        )
        self.assertEqual(report["candidate_count"], 0)
        self.assertIn("may not be detected", report["limitations"][0])


if __name__ == "__main__":
    unittest.main()
