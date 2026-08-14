from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import parse_structure  # noqa: E402
import preprocess  # noqa: E402
import make_ad_decisions  # noqa: E402
import scan_ads  # noqa: E402
import scan_blocked  # noqa: E402
import scan_identity  # noqa: E402
import scan_titles  # noqa: E402


class ScanIdentityF2Tests(unittest.TestCase):
    def bind_workspace(self, root: Path) -> Path:
        source = root / "anonymous.txt"
        source.write_text(
            "第一章 起点\n请访问 https://reader.example.com 获取更新。\n"
            "第二章 终点\n他看见了屏*词。\n",
            encoding="utf-8",
        )
        workspace = root / "anonymous.txt.cleanwork"
        preprocess.run(source, str(workspace))
        parse_structure.run(workspace)
        return workspace

    def sample_candidates(self) -> list[dict[str, object]]:
        return [
            {
                "candidate_id": f"AD-{index:04d}",
                "layer": "L1",
                "sample": f"promotion-{index}",
                "anchors": [
                    {
                        "offset": index * 10,
                        "end": index * 10 + len(f"promotion-{index}"),
                        "original": f"promotion-{index}",
                        "prefix": "before",
                        "suffix": "after",
                    }
                ],
                "suggested_decision": {
                    "candidate_id": f"AD-{index:04d}",
                    "verdict": "uncertain",
                },
            }
            for index in range(1, 4)
        ]

    def run_paged_scan(self, workspace: Path) -> dict[str, object]:
        candidates = self.sample_candidates()
        summary = {
            "candidate_count": 1,
            "first_page_count": 1,
            "total_candidate_count": 3,
            "page_size": 1,
            "page_count": 3,
            "performance": {"timings_seconds": {}},
        }
        with mock.patch.object(scan_ads, "scan_candidates", return_value=(candidates, summary)):
            return scan_ads.run(
                workspace,
                "versions/v1_preprocessed.txt",
                "candidates/ads.jsonl",
                12,
                1,
                10,
            )

    def test_candidate_fingerprint_is_content_based_and_set_hash_is_order_independent(self) -> None:
        first = self.sample_candidates()[0]
        renumbered = copy.deepcopy(first)
        renumbered["candidate_id"] = "AD-9999"
        renumbered["suggested_decision"]["candidate_id"] = "AD-9999"  # type: ignore[index]

        self.assertEqual(
            scan_identity.candidate_fingerprint(first),
            scan_identity.candidate_fingerprint(renumbered),
        )
        changed = copy.deepcopy(first)
        changed["anchors"][0]["original"] = "different"  # type: ignore[index]
        self.assertNotEqual(
            scan_identity.candidate_fingerprint(first),
            scan_identity.candidate_fingerprint(changed),
        )

        candidates = self.sample_candidates()
        scan_identity.attach_candidate_fingerprints(candidates)
        self.assertEqual(
            scan_identity.candidate_set_sha256(candidates),
            scan_identity.candidate_set_sha256(copy.deepcopy(candidates)),
        )
        self.assertEqual(
            scan_identity.candidate_set_sha256(candidates),
            scan_identity.candidate_set_sha256(list(reversed(candidates))),
        )

    def test_anchor_ids_are_stable_and_content_bound(self) -> None:
        first = self.sample_candidates()
        second = copy.deepcopy(first)
        for candidates in (first, second):
            scan_identity.attach_candidate_fingerprints(candidates)
            scan_identity.attach_anchor_ids(candidates)
            scan_identity.validate_anchor_ids(candidates)

        self.assertEqual(
            first[0]["anchors"][0]["anchor_id"],  # type: ignore[index]
            second[0]["anchors"][0]["anchor_id"],  # type: ignore[index]
        )
        first[0]["anchors"][0]["anchor_id"] = "AN-" + "0" * 64  # type: ignore[index]
        with self.assertRaises(scan_identity.ScanIdentityError):
            scan_identity.validate_anchor_ids(first)

    def test_bound_edit_plan_does_not_change_candidate_identity(self) -> None:
        text = "剧情正文继续推进。" * 50 + "请访问 https://reader.example.com/update 获取更新。"
        candidates, _ = scan_ads.scan_candidates(text, max_candidates=20)
        self.assertTrue(candidates)
        scan_identity.attach_candidate_fingerprints(candidates)
        scan_identity.attach_anchor_ids(candidates)
        before = [candidate["candidate_fingerprint"] for candidate in candidates]

        scan_ads.bind_edit_plans(candidates)

        self.assertEqual(
            before,
            [scan_identity.candidate_fingerprint(candidate) for candidate in candidates],
        )
        scan_identity.validate_anchor_ids(candidates)

    def test_scan_identity_binds_input_structure_config_and_candidate_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.txt"
            structure_path = root / "chapters.json"
            input_path.write_text("body", encoding="utf-8")
            structure_path.write_text('{"chapters":[]}', encoding="utf-8")
            candidates = self.sample_candidates()
            scan_identity.attach_candidate_fingerprints(candidates)
            scan_identity.attach_anchor_ids(candidates)

            baseline = scan_identity.build_scan_identity(
                "ads", input_path, structure_path, {"page_size": 10}, candidates
            )
            same = scan_identity.build_scan_identity(
                "ads", input_path, structure_path, {"page_size": 10}, copy.deepcopy(candidates)
            )
            self.assertEqual(baseline, same)

            input_path.write_text("changed body", encoding="utf-8")
            input_changed = scan_identity.build_scan_identity(
                "ads", input_path, structure_path, {"page_size": 10}, candidates
            )
            self.assertNotEqual(baseline["input_sha256"], input_changed["input_sha256"])
            self.assertNotEqual(baseline["scan_id"], input_changed["scan_id"])

            input_path.write_text("body", encoding="utf-8")
            structure_path.write_text('{"chapters":[{"index":1}]}', encoding="utf-8")
            structure_changed = scan_identity.build_scan_identity(
                "ads", input_path, structure_path, {"page_size": 10}, candidates
            )
            self.assertNotEqual(baseline["structure_sha256"], structure_changed["structure_sha256"])
            self.assertNotEqual(baseline["scan_id"], structure_changed["scan_id"])

            structure_path.write_text('{"chapters":[]}', encoding="utf-8")
            config_changed = scan_identity.build_scan_identity(
                "ads", input_path, structure_path, {"page_size": 11}, candidates
            )
            self.assertNotEqual(baseline["config_sha256"], config_changed["config_sha256"])
            self.assertNotEqual(baseline["scan_id"], config_changed["scan_id"])

    def test_scan_identity_binds_the_current_scanner_rule_pack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.txt"
            structure_path = root / "chapters.json"
            input_path.write_text("body", encoding="utf-8")
            structure_path.write_text('{"chapters":[]}', encoding="utf-8")
            candidates = self.sample_candidates()
            scan_identity.attach_candidate_fingerprints(candidates)
            scan_identity.attach_anchor_ids(candidates)
            first_pack = {
                "schema_version": 1,
                "scanner": "ads",
                "files": [{"path": "scripts/scan_ads.py", "sha256": "1" * 64}],
            }
            second_pack = copy.deepcopy(first_pack)
            second_pack["files"][0]["sha256"] = "2" * 64  # type: ignore[index]

            with mock.patch.object(
                scan_identity,
                "build_scan_rule_pack",
                return_value=first_pack,
            ):
                first = scan_identity.build_scan_identity(
                    "ads", input_path, structure_path, {}, candidates
                )
            with mock.patch.object(
                scan_identity,
                "build_scan_rule_pack",
                return_value=second_pack,
            ):
                second = scan_identity.build_scan_identity(
                    "ads", input_path, structure_path, {}, candidates
                )

            self.assertEqual(first["scan_identity_schema_version"], 3)
            self.assertEqual(first["scan_rule_pack"], first_pack)
            self.assertNotEqual(
                first["scan_rule_pack_sha256"], second["scan_rule_pack_sha256"]
            )
            self.assertNotEqual(first["scan_id"], second["scan_id"])

    def test_draft_rule_pack_and_profile_identity_are_deterministic(self) -> None:
        first = scan_identity.build_draft_rule_pack()
        second = scan_identity.build_draft_rule_pack()
        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(first["component"], "ads_draft")
        self.assertEqual(
            [entry["path"] for entry in first["files"]],
            sorted(
                [
                    "scripts/ad_decision_policy.py",
                    "scripts/ad_review_protocol.py",
                    "scripts/ad_rules.py",
                    "scripts/book_profile.py",
                    "scripts/make_ad_decisions.py",
                ]
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "book_profile.json"
            missing = scan_identity.build_profile_identity(profile)
            self.assertEqual(missing["profile_present"], False)
            self.assertRegex(missing["book_profile_sha256"], r"^[0-9a-f]{64}$")

            profile.write_text("{}\n", encoding="utf-8")
            present = scan_identity.build_profile_identity(profile)
            self.assertEqual(present["profile_present"], True)
            self.assertEqual(
                present["book_profile_sha256"], missing["book_profile_sha256"]
            )
            self.assertRegex(
                present["book_profile_file_sha256"], r"^[0-9a-f]{64}$"
            )
            profile.write_text('{"title":"changed"}\n', encoding="utf-8")
            changed = scan_identity.build_profile_identity(profile)
            self.assertNotEqual(
                changed["book_profile_sha256"], present["book_profile_sha256"]
            )
            profile.write_bytes(b'\xef\xbb\xbf{\r\n  "title": "changed"\r\n}\r\n')
            reformatted = scan_identity.build_profile_identity(profile)
            self.assertEqual(
                reformatted["book_profile_sha256"],
                changed["book_profile_sha256"],
            )
            self.assertNotEqual(
                reformatted["book_profile_file_sha256"],
                changed["book_profile_file_sha256"],
            )
            profile.write_text('{"rename_approved":true}\n', encoding="utf-8")
            with self.assertRaisesRegex(
                scan_identity.ScanIdentityError, "public schema"
            ):
                scan_identity.build_profile_identity(profile)

    def test_review_protocol_identity_binds_source_and_utf8_byte_budgets(self) -> None:
        first = scan_identity.build_review_protocol_identity(
            target_page_bytes=32 * 1024,
            hard_page_bytes=48 * 1024,
        )
        second = scan_identity.build_review_protocol_identity(
            target_page_bytes=32 * 1024,
            hard_page_bytes=48 * 1024,
        )
        changed = scan_identity.build_review_protocol_identity(
            target_page_bytes=24 * 1024,
            hard_page_bytes=48 * 1024,
        )
        self.assertEqual(first, second)
        self.assertEqual(first["target_page_bytes"], 32 * 1024)
        self.assertEqual(first["hard_page_bytes"], 48 * 1024)
        self.assertEqual(
            [entry["path"] for entry in first["source_pack"]["files"]],
            ["scripts/ad_review_protocol.py"],
        )
        self.assertNotEqual(
            scan_identity.canonical_json_sha256(first),
            scan_identity.canonical_json_sha256(changed),
        )

    def test_draft_report_stage_and_records_bind_runtime_and_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.bind_workspace(Path(directory))
            scan_ads.run(
                workspace,
                "versions/v1_preprocessed.txt",
                "candidates/ads.jsonl",
                12,
                20,
                20,
            )
            profile = workspace / "meta/book_profile.json"
            profile.write_text("{}\n", encoding="utf-8")
            report = make_ad_decisions.run(
                workspace,
                "candidates/ads_pages",
                "decisions/ads_decisions.draft.jsonl",
                "meta/book_profile.json",
                True,
            )
            manifest = json.loads(
                (workspace / "manifest.json").read_text(encoding="utf-8")
            )
            stage = manifest["stages"]["2_ads"]
            expected_pack = scan_identity.build_draft_rule_pack()
            expected_profile = scan_identity.build_profile_identity(profile)
            expected_review_identity = scan_identity.build_review_protocol_identity(
                target_page_bytes=32 * 1024,
                hard_page_bytes=48 * 1024,
            )

            self.assertEqual(report["draft_rule_pack"], expected_pack)
            self.assertEqual(
                report["draft_rule_pack_sha256"],
                scan_identity.canonical_json_sha256(expected_pack),
            )
            for key, value in {
                "draft_rule_pack": expected_pack,
                "draft_rule_pack_sha256": report["draft_rule_pack_sha256"],
                **expected_profile,
            }.items():
                self.assertEqual(stage[key], value)
            self.assertEqual(
                report["review_protocol_identity"], expected_review_identity
            )
            self.assertEqual(
                stage["review_protocol_identity_sha256"],
                scan_identity.canonical_json_sha256(expected_review_identity),
            )
            review_manifest = json.loads(
                (workspace / report["review_pages_manifest"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                review_manifest["review_protocol_identity"], expected_review_identity
            )
            self.assertEqual(
                review_manifest["candidate_set_sha256"], report["candidate_set_sha256"]
            )
            drafts = [
                json.loads(line)
                for line in (workspace / "decisions/ads_decisions.draft.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            for draft in drafts:
                self.assertEqual(
                    draft["draft_rule_pack_sha256"],
                    report["draft_rule_pack_sha256"],
                )
                self.assertEqual(
                    draft["book_profile_sha256"],
                    expected_profile["book_profile_sha256"],
                )
                self.assertEqual(
                    draft["profile_present"], expected_profile["profile_present"]
                )

    def test_all_scanners_publish_the_same_identity_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.bind_workspace(Path(directory))
            reports = (
                self.run_paged_scan(workspace),
                scan_titles.run(workspace, "auto", "candidates/titles.jsonl"),
                scan_blocked.run(workspace, "auto", "candidates/blocked.jsonl", 20),
            )
            manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
            for report, stage_name in zip(reports, ("2_ads", "3_titles", "4_blocked_words")):
                for key in (
                    "scan_id",
                    "input_sha256",
                    "structure_sha256",
                    "config_sha256",
                    "candidate_set_sha256",
                    "scan_rule_pack_sha256",
                ):
                    self.assertRegex(str(report[key]), r"^[0-9a-f]{64}$")
                    self.assertEqual(manifest["stages"][stage_name][key], report[key])
                self.assertEqual(report["scan_identity_schema_version"], 3)
                self.assertEqual(
                    manifest["stages"][stage_name]["scan_identity_schema_version"],
                    3,
                )
                self.assertEqual(
                    manifest["stages"][stage_name]["scan_rule_pack"],
                    report["scan_rule_pack"],
                )

            first = scan_titles.run(workspace, "auto", "candidates/titles.jsonl")
            second = scan_titles.run(workspace, "auto", "candidates/titles.jsonl")
            self.assertEqual(first["scan_id"], second["scan_id"])
            self.assertEqual(first["candidate_set_sha256"], second["candidate_set_sha256"])

    def test_report_bound_structure_path_is_validated_with_the_scan_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.bind_workspace(Path(directory))
            report = scan_titles.run(
                workspace,
                "auto",
                "candidates/titles.jsonl",
            )
            candidates = [
                json.loads(line)
                for line in (workspace / report["output"]).read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
            scan_identity.validate_scan_identity(workspace, report, candidates)

            missing = copy.deepcopy(report)
            missing["structure"] = ""
            with self.assertRaisesRegex(
                scan_identity.ScanIdentityError,
                "structure path is missing",
            ):
                scan_identity.validate_scan_identity(
                    workspace,
                    missing,
                    candidates,
                )

            mismatched = copy.deepcopy(report)
            mismatched["structure"] = "meta/other.json"
            with self.assertRaisesRegex(
                scan_identity.ScanIdentityError,
                "does not match the committed scan",
            ):
                scan_identity.validate_scan_identity(
                    workspace,
                    mismatched,
                    candidates,
                )

            manifest_path = workspace / "manifest.json"
            manifest_before = manifest_path.read_bytes()
            manifest = json.loads(manifest_before)
            manifest["stages"]["3_titles"]["structure"] = "../outside.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False),
                encoding="utf-8",
            )
            escaped = copy.deepcopy(report)
            escaped["structure"] = "../outside.json"
            try:
                with self.assertRaisesRegex(
                    scan_identity.ScanIdentityError,
                    "escapes the workspace",
                ):
                    scan_identity.validate_scan_identity(
                        workspace,
                        escaped,
                        candidates,
                    )
            finally:
                manifest_path.write_bytes(manifest_before)

    def test_page_manifest_is_explicit_and_loads_the_complete_ordered_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.bind_workspace(Path(directory))
            report = self.run_paged_scan(workspace)

            self.assertEqual(report["pages"]["page_count"], 3)  # type: ignore[index]
            self.assertEqual(
                report["pages"]["manifest"],  # type: ignore[index]
                [
                    {
                        "file": f"candidates/ads_pages/ads_page_{index:03d}.jsonl",
                        "page_number": index,
                        "record_count": 1,
                        "sha256": mock.ANY,
                    }
                    for index in range(1, 4)
                ],
            )
            records = scan_identity.load_validated_pages(workspace, report)
            self.assertEqual([record["candidate_id"] for record in records], ["AD-0001", "AD-0002", "AD-0003"])

    def test_missing_swapped_duplicate_and_tampered_pages_are_rejected(self) -> None:
        cases = ("missing", "swapped", "duplicate", "tampered")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                workspace = self.bind_workspace(Path(directory))
                report = self.run_paged_scan(workspace)
                first = workspace / "candidates/ads_pages/ads_page_001.jsonl"
                second = workspace / "candidates/ads_pages/ads_page_002.jsonl"
                if case == "missing":
                    first.unlink()
                elif case == "swapped":
                    first_bytes = first.read_bytes()
                    first.write_bytes(second.read_bytes())
                    second.write_bytes(first_bytes)
                elif case == "duplicate":
                    report["pages"]["manifest"][1]["page_number"] = 1  # type: ignore[index]
                else:
                    first.write_text('{"candidate_id":"forged"}\n', encoding="utf-8")

                with self.assertRaises(scan_identity.ScanIdentityError):
                    scan_identity.load_validated_pages(workspace, report)

    def test_forged_report_bindings_cannot_replace_the_committed_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.bind_workspace(Path(directory))
            report = self.run_paged_scan(workspace)
            report["scan_config"]["max_anchors"] = 999  # type: ignore[index]
            report["config_sha256"] = scan_identity.canonical_json_sha256(report["scan_config"])
            report["scan_id"] = scan_identity.canonical_json_sha256(
                {
                    "scanner": report["scanner"],
                    "input_sha256": report["input_sha256"],
                    "structure_sha256": report["structure_sha256"],
                    "config_sha256": report["config_sha256"],
                    "candidate_set_sha256": report["candidate_set_sha256"],
                }
            )

            with self.assertRaises(scan_identity.ScanIdentityError):
                scan_identity.load_validated_pages(workspace, report)

    def test_report_and_stage_cannot_forge_a_non_runtime_rule_pack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.bind_workspace(Path(directory))
            report = self.run_paged_scan(workspace)
            manifest_path = workspace / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            forged_pack = copy.deepcopy(report["scan_rule_pack"])
            forged_pack["files"][0]["sha256"] = "f" * 64  # type: ignore[index]
            forged_hash = scan_identity.canonical_json_sha256(forged_pack)
            report["scan_rule_pack"] = forged_pack
            report["scan_rule_pack_sha256"] = forged_hash
            report["scan_id"] = scan_identity.canonical_json_sha256(
                {
                    "scan_identity_schema_version": 3,
                    "scanner": report["scanner"],
                    "input_sha256": report["input_sha256"],
                    "structure_sha256": report["structure_sha256"],
                    "config_sha256": report["config_sha256"],
                    "candidate_set_sha256": report["candidate_set_sha256"],
                    "scan_rule_pack_sha256": forged_hash,
                }
            )
            stage = manifest["stages"]["2_ads"]
            stage["scan_rule_pack"] = forged_pack
            stage["scan_rule_pack_sha256"] = forged_hash
            stage["scan_id"] = report["scan_id"]
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )

            with self.assertRaisesRegex(
                scan_identity.ScanIdentityError, "current scanner rule pack"
            ):
                scan_identity.load_validated_pages(workspace, report)


if __name__ == "__main__":
    unittest.main()
