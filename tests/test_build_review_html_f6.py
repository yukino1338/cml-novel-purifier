from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_review_html as review  # noqa: E402


def workspace_item(name: str, state: str, **summary_overrides: int) -> dict[str, Any]:
    summary = {
        "ads_candidates": 0,
        "ads_decision_pending": 0,
        "title_candidates": 0,
        "blocked_candidates": 0,
        "operations": 0,
        "deleted_operations": 0,
        "changed_characters": 0,
        "formal_decisions": 0,
        "formal_uncertain": 0,
        "validation_issues": 0,
        "protection_conflicts": 0,
        "anomalies": 0,
        "chapter_count": 0,
        "structure_confidence": "unknown",
        "fallback_chunks": 0,
    }
    summary.update(summary_overrides)
    labels = {
        "needs-review": ("需要复核", "自动处理已暂停"),
        "awaiting-agent": ("等待自动处理", "等待 Agent 自动决策"),
        "pending-verify": ("等待验证", "自动决策已生成"),
        "verified": ("已验证", "清洗已完成，等待导出"),
        "completed": ("处理完成", "清洗与导出已完成"),
    }
    label, title = labels[state]
    modules = {
        module: {
            "candidate_count": 0,
            "draft_count": 0,
            "decision_count": 0,
            "groups": [],
            "details": [],
            "review_items": [],
        }
        for module in review.MODULES
    }
    return {
        "workspace": f"C:/books/{name}.txt.cleanwork",
        "name": f"{name}.txt",
        "summary": summary,
        "workflow": {"key": state, "label": label, "title": title, "message": "状态说明。"},
        "focus": [],
        "review_summary": {},
        "rollback": {
            "all": {
                "powershell": "python scripts/rollback.py 'example.cleanwork' --level all",
                "posix": "python scripts/rollback.py example.cleanwork --level all",
            },
            "ads_module": {
                "powershell": "python scripts/rollback.py 'example.cleanwork' --level module --module ads --overwrite",
                "posix": "python scripts/rollback.py example.cleanwork --level module --module ads --overwrite",
            },
        },
        "reports": {},
        "modules": modules,
    }


class ReviewPageF6Tests(unittest.TestCase):
    def test_review_payload_v2_binds_state_and_uses_python_delete_eligibility(self) -> None:
        item = workspace_item("状态绑定", "needs-review", formal_uncertain=1)
        safe = review.review_candidate(
            Path("anonymous.cleanwork"),
            "ads",
            {
                "candidate_id": "AD-0001",
                "candidate_fingerprint": "a" * 64,
                "signals": ["url", "domain"],
                "occurrence_count": 1,
                "anchors_truncated": False,
                "anchors": [
                    {
                        "anchor_id": "A-0001",
                        "offset": 10,
                        "end": 22,
                        "line": 7,
                        "original": "example.com",
                        "prefix": "请访问",
                        "suffix": "获取更新",
                        "chapter": {"index": 2, "title": "第二章 风起"},
                    }
                ],
                "contexts": [{"before": "前文", "original": "请访问 example.com 获取更新", "after": "后文"}],
            },
            {"verdict": "delete", "reason": "外部访问引导"},
            None,
            None,
        )
        mixed = review.review_candidate(
            Path("anonymous.cleanwork"),
            "ads",
            {
                "candidate_id": "AD-0002",
                "candidate_fingerprint": "b" * 64,
                "signals": ["domain"],
                "mutation_guard": "long_line_mixed_content",
                "occurrence_count": 1,
                "anchors_truncated": False,
                "anchors": [
                    {
                        "anchor_id": "A-0002",
                        "offset": 30,
                        "end": 42,
                        "line": 8,
                        "original": "剧情夹杂域名",
                        "prefix": "",
                        "suffix": "",
                    }
                ],
                "sample": "剧情夹杂域名",
            },
            {"verdict": "uncertain", "reason": "混合正文"},
            None,
            None,
        )
        item["modules"]["ads"]["review_items"] = [safe, mixed]
        item["modules"]["ads"]["candidate_count"] = 2

        payload = review.build_review_payload(item)

        self.assertEqual(payload["review_ui_schema"], 2)
        self.assertRegex(payload["review_state_id"], r"^[0-9a-f]{64}$")
        self.assertRegex(payload["workspace_identity"], r"^[0-9a-f]{64}$")
        first, second = payload["modules"]["ads"]["items"]
        self.assertEqual(first["display_index"], 1)
        self.assertTrue(first["delete_allowed"])
        self.assertTrue(first["batch_delete_allowed"])
        self.assertEqual(first["line_number"], 7)
        self.assertNotIn("AD-", first["display_title"])
        self.assertNotIn("ADF-", first["display_title"])
        self.assertFalse(second["delete_allowed"])
        self.assertIn("mixed_whole_block", second["delete_blockers"])
        self.assertIsNone(second["edit_plan_id"])
        self.assertIn("尚未支持", second["segment_support_message"])

        protected = review.review_candidate(
            Path("anonymous.cleanwork"),
            "ads",
            {
                "candidate_id": "AD-0003",
                "candidate_fingerprint": "d" * 64,
                "signals": ["url"],
                "occurrence_count": 1,
                "anchors_truncated": False,
                "anchors": [{"anchor_id": "A-0003", "original": "example.com"}],
                "sample": "example.com",
            },
            {"verdict": "uncertain", "protected_terms": ["作品专名"]},
            None,
            None,
        )
        self.assertFalse(protected["delete_allowed"])
        self.assertIn("protection_conflict", protected["delete_blockers"])

        changed = json.loads(json.dumps(item))
        changed["modules"]["ads"]["review_items"][0]["formal_decision"] = "keep"
        self.assertNotEqual(
            review.build_review_payload(changed)["review_state_id"],
            payload["review_state_id"],
        )

    def test_review_state_id_ignores_display_only_copy(self) -> None:
        item = workspace_item("稳定状态", "awaiting-agent", ads_decision_pending=1)
        candidate = {
            "candidate_id": "AD-0001",
            "candidate_fingerprint": "c" * 64,
            "signals": ["url"],
            "occurrence_count": 1,
            "anchors_truncated": False,
            "anchors": [{"anchor_id": "A-1", "original": "example.com", "line": 3}],
            "contexts": [{"original": "example.com"}],
        }
        rendered = review.review_candidate(
            Path("anonymous.cleanwork"), "ads", candidate, None, None, None
        )
        item["modules"]["ads"]["review_items"] = [rendered]
        first = review.build_review_payload(item)["review_state_id"]
        item["modules"]["ads"]["review_items"][0]["display_title"] = "仅改展示文案"
        second = review.build_review_payload(item)["review_state_id"]
        self.assertEqual(first, second)

    def test_review_assets_load_from_module_location_and_stay_inline(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                os.chdir(temp_dir)
                css, script = review.load_review_assets()
                item = workspace_item("资产测试", "completed")
                page = review.render_html(
                    {"mode": "single", "workspaces": [item], "summary": item["summary"]},
                    "测试结果",
                )
            finally:
                os.chdir(previous_cwd)

        marker = f"cml-review-template:{review.REVIEW_TEMPLATE_VERSION}"
        self.assertIn(marker, css)
        self.assertIn(marker, script)
        self.assertIn(
            f'<meta name="cml-review-template-version" content="{review.REVIEW_TEMPLATE_VERSION}">',
            page,
        )
        self.assertIn(f"<style>\n{css}\n</style>", page)
        self.assertIn(f"<script>\n{script}\n</script>", page)
        self.assertNotIn(review.REVIEW_TEMPLATE_SENTINEL, page)
        self.assertNotIn("<script src=", page)
        self.assertNotIn("<link rel=", page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)

    def test_review_asset_loading_fails_closed_for_missing_or_unversioned_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            asset_dir = Path(temp_dir)
            with mock.patch.object(review, "REVIEW_ASSET_DIR", asset_dir):
                with self.assertRaisesRegex(RuntimeError, "review.css"):
                    review.load_review_assets()

            (asset_dir / "review.css").write_text(
                f"/* cml-review-template:{review.REVIEW_TEMPLATE_SENTINEL} */\n:root {{ color: black; }}\n",
                encoding="utf-8",
            )
            (asset_dir / "review.js").write_text("(() => {})();\n", encoding="utf-8")
            with mock.patch.object(review, "REVIEW_ASSET_DIR", asset_dir):
                with self.assertRaisesRegex(RuntimeError, "version sentinel"):
                    review.load_review_assets()

    @unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
    def test_review_javascript_template_has_valid_syntax(self) -> None:
        completed = subprocess.run(
            [shutil.which("node") or "node", "--check", str(review.REVIEW_ASSET_DIR / "review.js")],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_review_context_keeps_full_text_for_bounded_browser_display(self) -> None:
        original = "候选正文" * 100 + "完整结尾"

        context = review.contexts({"contexts": [{"before": "前文", "original": original, "after": "后文"}]})

        self.assertEqual(context["original"], original)
        self.assertTrue(context["original"].endswith("完整结尾"))

    def test_batch_index_orders_books_by_risk_without_embedding_book_details(self) -> None:
        completed = workspace_item("已完成", "completed")
        waiting = workspace_item("待处理", "awaiting-agent", ads_decision_pending=4)
        blocked = workspace_item("有阻止项", "needs-review", anomalies=1)
        blocked["modules"]["ads"]["review_items"] = [
            {"candidate_id": "PRIVATE-CANDIDATE", "original": "PRIVATE-CONTEXT"}
        ]

        ordered = review.order_workspaces_for_display([completed, waiting, blocked])
        books = review.build_batch_books(
            [completed, waiting, blocked],
            ["a" * 64, "b" * 64, "c" * 64],
        )
        data = review.build_batch_index_data(books, review.aggregate([completed, waiting, blocked]), "run-1")
        page = review.render_batch_index_html(data, "测试结果")
        serialized = json.dumps(data, ensure_ascii=False)

        self.assertEqual([item["name"] for item in ordered], ["有阻止项.txt", "待处理.txt", "已完成.txt"])
        self.assertEqual([item["name"] for item in books], ["有阻止项.txt", "待处理.txt", "已完成.txt"])
        self.assertIn("小说列表（风险优先）", page)
        self.assertIn("books/", page)
        self.assertLess(page.index("有阻止项.txt"), page.index("待处理.txt"))
        self.assertLess(page.index("待处理.txt"), page.index("已完成.txt"))
        self.assertNotIn("C:/books/", page)
        self.assertNotIn("C:/books/", serialized)
        self.assertNotIn("PRIVATE-CANDIDATE", page)
        self.assertNotIn("PRIVATE-CONTEXT", serialized)
        self.assertNotIn("workspaces", data)
        self.assertNotIn("modules", serialized)
        self.assertNotIn("reports", serialized)

    def test_batch_index_size_does_not_follow_candidate_body_size(self) -> None:
        small = workspace_item("示例", "needs-review", anomalies=1)
        large = workspace_item("示例", "needs-review", anomalies=1)
        large["modules"]["ads"]["review_items"] = [
            {"candidate_id": "AD-0001", "original": "候选正文" * 100_000}
        ]

        small_books = review.build_batch_books([small], ["a" * 64])
        large_books = review.build_batch_books([large], ["a" * 64])
        small_page = review.render_batch_index_html(
            review.build_batch_index_data(small_books, review.aggregate([small]), "run-1"),
            "测试结果",
        )
        large_page = review.render_batch_index_html(
            review.build_batch_index_data(large_books, review.aggregate([large]), "run-1"),
            "测试结果",
        )

        self.assertEqual(small_page, large_page)

    def test_batch_book_ids_are_full_sha256_and_child_pages_do_not_cross_books(self) -> None:
        first = workspace_item("甲书", "completed")
        second = workspace_item("乙书", "completed")
        first["focus"] = [{"level": "info", "message": "FIRST-ONLY"}]
        second["focus"] = [{"level": "info", "message": "SECOND-ONLY"}]

        first_id = review.batch_book_id(Path(first["workspace"]))
        second_id = review.batch_book_id(Path(second["workspace"]))
        first_page = review.render_html(
            {"mode": "single", "workspaces": [first], "summary": first["summary"]},
            "甲书 · 小说清洗结果",
        )
        second_page = review.render_html(
            {"mode": "single", "workspaces": [second], "summary": second["summary"]},
            "乙书 · 小说清洗结果",
        )

        self.assertRegex(first_id, r"^[0-9a-f]{64}$")
        self.assertRegex(second_id, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first_id, second_id)
        self.assertIn("FIRST-ONLY", first_page)
        self.assertNotIn("SECOND-ONLY", first_page)
        self.assertIn("SECOND-ONLY", second_page)
        self.assertNotIn("FIRST-ONLY", second_page)

    def test_batch_child_validation_rejects_missing_empty_and_tampered_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            book_id = "a" * 64
            child = root / "books" / f"{book_id}.html"
            child.parent.mkdir()
            child.write_text("<!doctype html><title>book</title>", encoding="utf-8")
            books = [
                {
                    "id": book_id,
                    "name": "示例.txt",
                    "status": {"key": "completed", "label": "处理完成"},
                    "risk": {"priority": 4, "label": "已完成，无阻止项", "attention_count": 0, "pending_count": 0},
                    "summary": {},
                    "html": f"books/{book_id}.html",
                    "sha256": review.sha256_file(child),
                }
            ]
            index = root / "review_index.json"
            index.write_text(
                json.dumps(review.build_batch_index_data(books, {}, "run-1"), ensure_ascii=False),
                encoding="utf-8",
            )

            review.validate_batch_book_pages(index)

            data = json.loads(index.read_text(encoding="utf-8"))
            data["books"][0]["workspace"] = "C:/private/example.cleanwork"
            index.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "不允许的字段"):
                review.validate_batch_book_pages(index)
            del data["books"][0]["workspace"]
            index.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

            child.write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                review.validate_batch_book_pages(index)

            child.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "为空"):
                review.validate_batch_book_pages(index)

            child.unlink()
            with self.assertRaisesRegex(RuntimeError, "缺失"):
                review.validate_batch_book_pages(index)

    def test_review_controls_expose_keyboard_and_state_semantics(self) -> None:
        item = workspace_item("长文本", "needs-review", formal_uncertain=1)
        item["modules"]["ads"]["candidate_count"] = 1
        item["modules"]["ads"]["review_items"] = [
            {
                "candidate_id": "AD-0001",
                "module": "ads",
                "risk": "high",
                "draft_verdict": "uncertain",
                "formal_decision": "uncertain",
                "chapter": {"index": 1, "title": "第一章"},
                "anchors_count": 1,
                "anchors": [{"offset": 1, "prefix": "前", "original": "广告", "suffix": "后"}],
                "before": "前文",
                "original": "很长的候选原文" * 80,
                "after": "后文",
                "needs_review": True,
                "review_reasons": ["Agent 正式结论仍为未决"],
            }
        ]

        page = review.render_html({"mode": "single", "workspaces": [item], "summary": item["summary"]}, "测试结果")

        self.assertIn("aria-live='polite'", page)
        self.assertIn("type='radio'", page)
        self.assertIn("<fieldset class='verdict-fieldset'>", page)
        self.assertIn("data-review-text-toggle", page)
        self.assertIn("sessionStorage", page)
        self.assertIn(":focus-visible", page)
        self.assertIn("prefers-reduced-motion", page)
        self.assertIn(".status::before", page)
        self.assertIn("font-size: 16px", page)
        self.assertIn("cml.review-request.v1", page)
        self.assertNotIn("<script src=", page)
        self.assertNotIn("<link rel=", page)

    def test_rollback_commands_have_visible_keyboard_copy_actions(self) -> None:
        item = workspace_item("可回退", "pending-verify", operations=2)

        page = review.render_html({"mode": "single", "workspaces": [item], "summary": item["summary"]}, "测试结果")

        self.assertIn("data-copy-command", page)
        self.assertIn("复制全部回退命令", page)
        self.assertIn("role='status'", page)

    def test_copied_rollback_command_quotes_external_path_and_candidate_id(self) -> None:
        workspace = Path("C:/books/含'特殊$()字符.txt.cleanwork")

        for shell in ("powershell", "posix"):
            with self.subTest(shell=shell):
                command = review.rollback_command(
                    workspace,
                    "ads",
                    "point",
                    "AD-`特殊",
                    shell=shell,
                )
                self.assertIn(review.shell_quote(workspace, shell), command)
                self.assertIn(
                    "--candidate-id " + review.shell_quote("AD-`特殊", shell),
                    command,
                )
        with self.assertRaisesRegex(ValueError, "only for ads"):
            review.rollback_command(workspace, "titles", "module", shell="posix")
        with self.assertRaisesRegex(ValueError, "unsupported rollback level"):
            review.rollback_command(workspace, "ads", "unknown", shell="posix")

    def test_report_only_modules_show_the_real_boundary_and_no_rollback(self) -> None:
        group = {
            "group": "疑似标题",
            "count": 1,
            "anchors_count": 1,
            "samples": [
                {
                    "candidate_id": "TT-0001",
                    "risk": "low",
                    "original": "匿名候选",
                    "rollback": {},
                }
            ],
        }

        rendered = review.render_group(group, "titles")
        self.assertIn("扫描状态", rendered)
        self.assertIn("处理边界", rendered)
        self.assertIn("只报告", rendered)
        self.assertIn("原文不变", rendered)
        self.assertNotIn("Agent 正式结论", rendered)
        self.assertNotIn("data-copy-command", rendered)

        candidate = review.review_candidate(
            Path("anonymous.cleanwork"),
            "blocked",
            {
                "candidate_id": "BW-0001",
                "mask_type": "mask_chars",
                "context": "匿名屏*蔽文本",
                "anchors": [],
            },
            None,
            None,
            None,
        )
        self.assertEqual(candidate["rollback"], {})

    def test_rollback_guide_uses_chinese_headings(self) -> None:
        item = workspace_item("示例", "pending-verify", operations=1)
        item["modules"]["titles"]["details"] = [
            {"candidate_id": "TT-0001", "rollback": {"point": {"posix": "UNSUPPORTED"}}}
        ]

        guide = "\n".join(review.rollback_lines_for_workspace(item))

        self.assertIn("工作区", guide)
        self.assertIn("模块回退", guide)
        self.assertIn("候选回退示例", guide)
        self.assertNotIn("UNSUPPORTED", guide)
        self.assertNotIn("Workspace:", guide)


if __name__ == "__main__":
    unittest.main()
