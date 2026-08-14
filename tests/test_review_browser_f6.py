from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_review_html as review  # noqa: E402
import parse_structure  # noqa: E402
import preprocess  # noqa: E402
import scan_ads  # noqa: E402
from tests.test_build_review_html_f6 import workspace_item  # noqa: E402

try:
    from playwright.sync_api import Browser, Page, Playwright, sync_playwright
except ImportError:  # pragma: no cover - exercised on minimal runtime installs
    Browser = Page = Playwright = Any  # type: ignore[assignment,misc]
    sync_playwright = None


VIEWPORTS = ((1440, 1000), (1024, 900), (768, 900), (720, 500), (390, 844), (375, 812))


def browser_executable(playwright: Playwright) -> str | None:
    configured = os.environ.get("CML_PLAYWRIGHT_EXECUTABLE")
    if configured and Path(configured).is_file():
        return configured
    bundled = Path(playwright.chromium.executable_path)
    if bundled.is_file():
        return str(bundled)
    candidates = (
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    )
    return next((str(path) for path in candidates if path.is_file()), None)


def anonymous_review_item() -> dict[str, Any]:
    item = workspace_item(
        "匿名构造样本",
        "needs-review",
        ads_candidates=32,
        formal_uncertain=8,
        anomalies=1,
        operations=2,
    )
    item["workspace"] = "anonymous.cleanwork"
    item["review_summary"] = {
        "review_candidate_count": 32,
        "formal_uncertain": 8,
        "validation_issues": 0,
        "protection_conflicts": 0,
    }
    candidates = []
    formal_values = ("uncertain", "delete", "keep", None)
    draft_values = ("uncertain", "delete", "keep")
    for index in range(32):
        original = f"匿名候选 TOKEN-{index:02d}"
        if index == 0:
            original += "。这是用于检查折叠、展开和窄屏换行的构造长文本。" * 24
        candidates.append(
            {
                "candidate_id": f"AD-{index + 1:04d}",
                "candidate_fingerprint": f"{index + 1:064x}",
                "family_key": f"family-{index // 4}",
                "family_label": f"备用域名引导组 {index // 4 + 1}",
                "cluster_id": f"ADF-{index // 4 + 1:04d}-technical",
                "risk": ("high", "medium", "low")[index % 3],
                "draft_verdict": draft_values[index % len(draft_values)],
                "formal_decision": formal_values[index % len(formal_values)],
                "chapter": {"index": index // 4 + 1, "title": f"第 {index // 4 + 1} 章"},
                "anchors_count": 1,
                "anchors": [
                    {
                        "anchor_id": f"A-{index + 1:04d}",
                        "offset": 100 + index,
                        "line": 20 + index,
                        "prefix": "匿名前缀",
                        "original": f"TOKEN-{index:02d}",
                        "suffix": "匿名后缀",
                    }
                ],
                "before": "匿名前文",
                "original": original,
                "after": "匿名后文",
                "needs_review": True,
                "review_reasons": ["构造的正式复核项"],
                **(
                    {"mutation_guard": "long_line_mixed_content"}
                    if index == 1
                    else {}
                ),
            }
        )
        if index == 2:
            candidates[-1].update(
                {
                    "mutation_guard": "segment_review_required",
                    "edit_plan_id": "EP-0003",
                    "edit_plan_validated": True,
                    "edit_plan_sha256": "3" * 64,
                    "delete_allowed": False,
                    "delete_blockers": ["mixed_content_requires_segment_delete"],
                    "batch_delete_allowed": False,
                    "batch_delete_blockers": ["mixed_content_requires_segment_delete"],
                    "segment_delete_allowed": True,
                    "segment_delete_blockers": [],
                    "segment_support_message": "只可删除 Python 已锁定的外部引导片段。",
                    "anchors_count": 2,
                    "anchors": [
                        {
                            "anchor_id": "A-0003-1",
                            "offset": 102,
                            "line": 22,
                            "prefix": "前缀一",
                            "original": "TOKEN-02-ONE",
                            "suffix": "后缀一",
                        },
                        {
                            "anchor_id": "A-0003-2",
                            "offset": 202,
                            "line": 23,
                            "prefix": "前缀二",
                            "original": "TOKEN-02-TWO",
                            "suffix": "后缀二",
                        },
                    ],
                    "segment_previews": [
                        {
                            "anchor_id": "A-0003-1",
                            "boundary_kind": "external_suffix",
                            "keep_text": "剧情正文一",
                            "delete_text": "访问 example.one",
                            "after_text": "剧情正文一",
                            "preview_truncated": False,
                        },
                        {
                            "anchor_id": "A-0003-2",
                            "boundary_kind": "external_suffix",
                            "keep_text": "剧情正文二",
                            "delete_text": "访问 example.two",
                            "after_text": "剧情正文二",
                            "preview_truncated": False,
                        },
                    ],
                    "segment_previews_truncated": False,
                }
            )
    item["modules"]["ads"]["candidate_count"] = len(candidates)
    item["modules"]["ads"]["review_items"] = candidates
    return item


def build_real_workspace(root: Path, name: str, domain: str) -> Path:
    source = root / f"{name}.txt"
    source.write_text(
        "\n".join(
            [
                "第一章 起点",
                "人物甲记录匿名场景，并继续观察装置运行。",
                f"站外更新提示：请访问 https://{domain}/update 获取后续内容。",
                "人物乙确认匿名装置仍然稳定。",
                "第二章 继续",
                "人物甲在新的匿名场景中复核装置。",
                f"下载提示：请访问 https://{domain}/file 获取匿名文件。",
                "人物乙完成记录。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    workspace = preprocess.run(source, encoding="utf-8")
    parse_structure.run(workspace)
    scan_ads.run(
        workspace,
        "versions/v1_preprocessed.txt",
        "candidates/ads.jsonl",
        12,
        25,
        120,
    )
    return workspace


def tab_to(page: Page, selector: str, *, reverse: bool = False, max_steps: int = 160) -> None:
    key = "Shift+Tab" if reverse else "Tab"
    for _ in range(max_steps + 1):
        if page.evaluate(
            "selector => document.activeElement?.matches(selector) === true",
            selector,
        ):
            return
        page.keyboard.press(key)
    raise AssertionError(f"keyboard focus did not reach {selector!r}")


@unittest.skipUnless(sync_playwright is not None, "Playwright is not installed")
class ReviewBrowserF6Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temp = tempfile.TemporaryDirectory()
        root = Path(cls._temp.name)
        first_workspace = build_real_workspace(root, "匿名甲", "alpha.example.com")
        second_workspace = build_real_workspace(root, "匿名乙", "beta.example.com")
        single_result = review.run(
            [first_workspace],
            str(root / "single-review"),
            False,
            80,
            3,
        )
        batch_result = review.run(
            [first_workspace, second_workspace],
            str(root / "batch-review"),
            False,
            80,
            3,
        )
        cls.page_path = Path(single_result["html"])
        cls.batch_page_path = Path(batch_result["html"])
        cls.batch_books = batch_result["books"]
        cls.interactive_page_path = root / "interactive-review.html"
        item = anonymous_review_item()
        cls.interactive_page_path.write_text(
            review.render_html(
                {"mode": "single", "workspaces": [item], "summary": item["summary"]},
                "匿名小说清洗结果",
            ),
            encoding="utf-8",
        )
        cls._playwright = sync_playwright().start()
        executable = browser_executable(cls._playwright)
        if executable is None:
            cls._playwright.stop()
            cls._temp.cleanup()
            message = "No Chromium-compatible browser is installed for Playwright"
            if os.environ.get("CML_REQUIRE_BROWSER_TESTS") == "1":
                raise RuntimeError(message)
            raise unittest.SkipTest(message)
        cls.browser: Browser = cls._playwright.chromium.launch(
            executable_path=executable,
            headless=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "browser"):
            cls.browser.close()
        if hasattr(cls, "_playwright"):
            cls._playwright.stop()
        if hasattr(cls, "_temp"):
            cls._temp.cleanup()

    def open_page(
        self,
        width: int,
        height: int,
        *,
        path: Path | None = None,
        clipboard_mode: str = "success",
    ) -> tuple[Any, Page, list[str]]:
        context = self.browser.new_context(viewport={"width": width, "height": height})
        if clipboard_mode == "success":
            context.add_init_script(
                """
                Object.defineProperty(navigator, "clipboard", {
                  configurable: true,
                  value: {writeText: async (text) => { window.__copiedText = String(text); }},
                });
                """
            )
        elif clipboard_mode in {"missing", "reject"}:
            clipboard_value = (
                "undefined"
                if clipboard_mode == "missing"
                else "{writeText: async () => { throw new Error('clipboard rejected'); }}"
            )
            context.add_init_script(
                f"""
                Object.defineProperty(navigator, "clipboard", {{
                  configurable: true,
                  value: {clipboard_value},
                }});
                document.execCommand = (command) => {{
                  if (command !== "copy") return false;
                  window.__fallbackCopiedText = document.activeElement?.value || "";
                  return true;
                }};
                """
            )
        else:
            context.close()
            raise ValueError(f"unsupported clipboard mode: {clipboard_mode}")
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto((path or self.page_path).as_uri(), wait_until="load")
        return context, page, errors

    def test_five_viewports_have_no_document_overflow_or_hidden_controls(self) -> None:
        for width, height in VIEWPORTS:
            with self.subTest(width=width, height=height):
                context, page, errors = self.open_page(width, height)
                try:
                    layout = page.evaluate(
                        """
                        () => {
                          const visible = (node) => {
                            const style = getComputedStyle(node);
                            const rect = node.getBoundingClientRect();
                            return style.display !== "none" && style.visibility !== "hidden"
                              && rect.width > 0 && rect.height > 0;
                          };
                          const controls = [...document.querySelectorAll("button,input,select,summary,a[href]")]
                            .filter(visible);
                          return {
                            documentWidth: document.documentElement.scrollWidth,
                            viewportWidth: document.documentElement.clientWidth,
                            shell: (() => {
                              const rect = document.querySelector(".page-shell").getBoundingClientRect();
                              return {left: rect.left, right: rect.right};
                            })(),
                            zeroSizedControls: controls
                              .filter((node) => {
                                const rect = node.getBoundingClientRect();
                                return rect.width < 1 || rect.height < 1;
                              }).length,
                            horizontalControlOverflow: controls
                              .filter((node) => {
                                const rect = node.getBoundingClientRect();
                                return rect.left < -1 || rect.right > innerWidth + 1;
                              })
                              .map((node) => node.outerHTML.slice(0, 120)),
                          };
                        }
                        """
                    )
                    self.assertLessEqual(layout["documentWidth"], layout["viewportWidth"] + 1)
                    self.assertGreaterEqual(layout["shell"]["left"], -1)
                    self.assertLessEqual(layout["shell"]["right"], width + 1)
                    self.assertEqual(layout["zeroSizedControls"], 0)
                    self.assertEqual(layout["horizontalControlOverflow"], [])
                    self.assertEqual(errors, [])
                finally:
                    context.close()

    def test_awaiting_agent_initial_view_exposes_first_candidate_without_click(self) -> None:
        context, page, errors = self.open_page(768, 900)
        try:
            self.assertIn("需要复核", page.locator(".workspace-hero").inner_text())
            self.assertTrue(page.locator(".review-current").is_visible())
            self.assertTrue(page.locator(".review-original").is_visible())
            self.assertTrue(page.locator(".verdict-fieldset").is_visible())
            self.assertGreater(page.locator(".queue-group").count(), 0)
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_mobile_content_and_primary_choice_are_visible_in_document_order(self) -> None:
        for width, height in ((390, 844), (375, 812)):
            with self.subTest(width=width, height=height):
                context, page, errors = self.open_page(width, height)
                try:
                    geometry = page.evaluate(
                        """
                        () => {
                          const rect = (selector) => {
                            const box = document.querySelector(selector).getBoundingClientRect();
                            return {top: box.top, bottom: box.bottom};
                          };
                          const focusable = [...document.querySelectorAll("button,input,select,summary,a[href],[tabindex]")];
                          return {
                            original: rect(".review-original"),
                            choices: rect(".verdict-fieldset"),
                            height: innerHeight,
                            keepIndex: focusable.indexOf(document.querySelector("[data-review-choice='keep']")),
                            toolbarIndex: focusable.indexOf(document.querySelector("[data-review-module]")),
                          };
                        }
                        """
                    )
                    self.assertLessEqual(geometry["original"]["bottom"], geometry["height"])
                    self.assertLessEqual(geometry["choices"]["bottom"], geometry["height"])
                    self.assertLess(geometry["keepIndex"], geometry["toolbarIndex"])
                    self.assertEqual(errors, [])
                finally:
                    context.close()

    def test_mobile_long_excerpt_keeps_primary_choice_in_view(self) -> None:
        for width, height in ((390, 844), (375, 812)):
            with self.subTest(width=width, height=height):
                context, page, errors = self.open_page(
                    width,
                    height,
                    path=self.interactive_page_path,
                )
                try:
                    geometry = page.evaluate(
                        """
                        () => {
                          const original = document.querySelector(".review-original").getBoundingClientRect();
                          const choices = document.querySelector(".verdict-fieldset").getBoundingClientRect();
                          return {original, choices, height: innerHeight};
                        }
                        """
                    )
                    self.assertLessEqual(geometry["original"]["bottom"], geometry["height"])
                    self.assertLessEqual(geometry["choices"]["bottom"], geometry["height"])
                    self.assertEqual(errors, [])
                finally:
                    context.close()

    def test_mobile_queue_navigation_reveals_the_new_current_candidate(self) -> None:
        context, page, errors = self.open_page(390, 844, path=self.interactive_page_path)
        try:
            page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            page.locator("[data-review-open='AD-0003']").click()
            page.wait_for_function(
                "document.querySelector('[data-review-active-heading]').getBoundingClientRect().top >= -1"
            )
            geometry = page.evaluate(
                """
                () => {
                  const rect = (selector) => document.querySelector(selector).getBoundingClientRect();
                  return {
                    heading: rect("[data-review-active-heading]"),
                    original: rect(".review-original"),
                    choices: rect(".verdict-fieldset"),
                    height: innerHeight,
                    focused: document.activeElement?.matches("[data-review-active-heading]") === true,
                  };
                }
                """
            )
            self.assertGreaterEqual(geometry["heading"]["top"], -1)
            self.assertGreaterEqual(geometry["original"]["top"], -1)
            self.assertLessEqual(geometry["choices"]["bottom"], geometry["height"])
            self.assertTrue(geometry["focused"])
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_mobile_segment_candidate_keeps_primary_choice_in_view(self) -> None:
        for width, height in ((390, 844), (375, 812)):
            with self.subTest(width=width, height=height):
                context, page, errors = self.open_page(
                    width,
                    height,
                    path=self.interactive_page_path,
                )
                try:
                    page.locator("[data-review-open='AD-0003']").click()
                    page.wait_for_function(
                        "document.querySelector('[data-review-active-heading]').getBoundingClientRect().top >= -1"
                    )
                    geometry = page.evaluate(
                        """
                        () => {
                          const choices = document.querySelector(".verdict-fieldset").getBoundingClientRect();
                          const brief = document.querySelector(".mixed-brief").getBoundingClientRect();
                          const details = document.querySelector(".mixed-panel").getBoundingClientRect();
                          return {choices, brief, details, height: innerHeight};
                        }
                        """
                    )
                    self.assertLessEqual(geometry["brief"]["bottom"], geometry["height"])
                    self.assertLessEqual(geometry["choices"]["bottom"], geometry["height"])
                    self.assertGreaterEqual(geometry["details"]["top"], geometry["choices"]["bottom"])
                    self.assertEqual(errors, [])
                finally:
                    context.close()

    def test_keyboard_covers_filters_paging_details_long_text_selection_and_copy(self) -> None:
        context, page, errors = self.open_page(
            390,
            844,
            path=self.interactive_page_path,
        )
        try:
            module = page.locator("[data-review-module]")
            module.press("ArrowDown")
            self.assertEqual(module.input_value(), "ads")

            status = page.locator("[data-review-status]")
            status.press("ArrowDown")
            self.assertEqual(status.input_value(), "formal:uncertain")

            search = page.locator("[data-review-search]")
            search.press_sequentially("TOKEN-00")
            page.locator("[data-review-count]").wait_for(state="visible")
            self.assertIn("符合条件 1 / 全部 32 条", page.locator("[data-review-count]").inner_text())

            toggle = page.locator("[data-review-text-toggle]")
            toggle.focus()
            page.keyboard.press("Enter")
            self.assertEqual(toggle.get_attribute("aria-expanded"), "true")
            self.assertTrue(page.locator(".review-original").first.is_visible())

            technical = page.locator(".technical-details")
            technical.locator("summary").focus()
            page.keyboard.press("Enter")
            self.assertTrue(technical.get_attribute("open") is not None)

            choice = page.locator("[data-review-choice='delete']")
            choice.focus()
            before_scroll = page.evaluate("window.scrollY")
            page.keyboard.press("Space")
            self.assertTrue(choice.is_checked())
            self.assertEqual(page.locator("[data-review-selected]").inner_text(), "已形成 1 条复核请求")
            self.assertEqual(page.evaluate("document.activeElement?.dataset.reviewChoice"), "delete")
            self.assertEqual(toggle.get_attribute("aria-expanded"), "true")
            self.assertTrue(technical.get_attribute("open") is not None)
            self.assertLessEqual(abs(page.evaluate("window.scrollY") - before_scroll), 4)

            for verdict in ("keep", "uncertain", "delete"):
                radio = page.locator(f"[data-review-choice='{verdict}']")
                radio.focus()
                page.keyboard.press("Space")
                self.assertTrue(radio.is_checked())
                self.assertTrue(technical.get_attribute("open") is not None)
                self.assertEqual(toggle.get_attribute("aria-expanded"), "true")
            page.locator("[data-review-note]").fill("已检查上下文，确认请求。")

            page.locator("[data-review-copy]").focus()
            page.keyboard.press("Enter")
            page.wait_for_function(
                "document.querySelector('[data-review-copy-status]').textContent.includes('复核请求 JSON 已复制')"
            )
            copied = json.loads(page.evaluate("window.__copiedText"))
            self.assertEqual(copied["schema"], "cml.review-request.v1")
            self.assertEqual(copied["requests"][0]["candidate_id"], "AD-0001")
            self.assertNotIn("original", copied["requests"][0])
            self.assertNotIn("offset", copied["requests"][0])
            self.assertNotIn("ADF-", json.dumps(copied))

            page.once("dialog", lambda dialog: dialog.dismiss())
            page.locator("[data-review-clear]").click()
            self.assertEqual(page.locator("[data-review-selected]").inner_text(), "已形成 1 条复核请求")
            page.once("dialog", lambda dialog: dialog.accept())
            page.locator("[data-review-clear]").focus()
            page.keyboard.press("Enter")
            self.assertEqual(page.locator("[data-review-selected]").inner_text(), "尚未形成复核请求")

            search.press("Control+A")
            search.press("Backspace")
            status.press("Home")
            self.assertEqual(status.input_value(), "all")
            self.assertIn("第 1 / 2 页", page.locator("[data-review-count]").inner_text())

            tab_to(page, "[data-review-page='next']:not(:disabled)")
            page.keyboard.press("Enter")
            self.assertIn("第 2 / 2 页", page.locator("[data-review-count]").inner_text())
            self.assertEqual(page.evaluate("document.activeElement?.dataset.reviewResults !== undefined"), True)

            tab_to(page, "[data-review-page='prev']:not(:disabled)", max_steps=50)
            page.keyboard.press("Enter")
            self.assertIn("第 1 / 2 页", page.locator("[data-review-count]").inner_text())
            self.assertEqual(page.evaluate("document.activeElement?.dataset.reviewResults !== undefined"), True)

            previous_focus = None
            distinct_focus = set()
            module.focus()
            for _ in range(40):
                page.keyboard.press("Tab")
                current = page.evaluate(
                    """
                    () => {
                      const node = document.activeElement;
                      const focusables = [...document.querySelectorAll("summary,button,input,select,a[href],[tabindex]")];
                      return [focusables.indexOf(node), node?.tagName,
                        node?.getAttribute("data-candidate-id"), node?.getAttribute("data-review-choice"),
                        node?.getAttribute("data-review-page"), node?.textContent?.trim().slice(0, 40)].join("|");
                    }
                    """
                )
                self.assertNotEqual(current, previous_focus, "Tab focus is trapped on one control")
                distinct_focus.add(current)
                previous_focus = current
            self.assertGreaterEqual(len(distinct_focus), 16)
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_wrong_segment_boundary_can_only_export_an_uncertain_request(self) -> None:
        context, page, errors = self.open_page(
            768,
            900,
            path=self.interactive_page_path,
        )
        try:
            page.locator("[data-review-choice='keep']").click()
            page.locator("[data-review-reason]").select_option("segment_boundary_wrong")
            page.locator("[data-review-note]").fill("扫描器边界包含了剧情文本。")
            page.locator("[data-review-copy]").click()
            self.assertIn(
                "必须选择暂不判断",
                page.locator("[data-review-copy-status]").inner_text(),
            )

            page.locator("[data-review-choice='uncertain']").click()
            page.locator("[data-review-copy]").click()
            page.wait_for_function(
                "document.querySelector('[data-review-copy-status]').textContent.includes('复核请求 JSON 已复制')"
            )
            request = json.loads(page.evaluate("window.__copiedText"))["requests"][0]
            self.assertEqual(request["verdict"], "uncertain")
            self.assertNotIn("edit_plan_id", request)
            self.assertNotIn("offset", request)
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_review_state_survives_reload_in_session_storage(self) -> None:
        context, page, errors = self.open_page(
            390,
            844,
            path=self.interactive_page_path,
        )
        try:
            page.locator("[data-review-module]").select_option("ads")
            page.locator("[data-review-scope]").select_option("all")
            page.locator("[data-review-status]").select_option("all")
            page.locator("[data-review-search]").fill("")
            page.locator("[data-review-choice='keep']").click()
            page.locator("[data-review-note]").fill("恢复备注")
            page.locator(".technical-details summary").click()
            page.locator("[data-review-check]").first.check()
            page.locator("[data-review-page='next']:not(:disabled)").click()
            self.assertIn("第 2 / 2 页", page.locator("[data-review-count]").inner_text())

            page.reload(wait_until="load")

            self.assertEqual(page.locator("[data-review-module]").input_value(), "ads")
            self.assertEqual(page.locator("[data-review-scope]").input_value(), "all")
            self.assertEqual(page.locator("[data-review-status]").input_value(), "all")
            self.assertIn("第 2 / 2 页", page.locator("[data-review-count]").inner_text())
            saved = page.evaluate("() => JSON.parse(document.querySelector('.exception-review').dataset.reviewState)")
            self.assertEqual(saved["page"], 2)
            self.assertEqual(saved["filters"]["module"], "ads")
            self.assertEqual(saved["decisions"]["AD-0001"]["note"], "恢复备注")
            self.assertEqual(saved["expanded_technical_ids"], ["AD-0001"])
            self.assertEqual(saved["checked_ids"], ["AD-0001"])
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_content_first_grouping_mixed_guard_batch_and_old_import_rejection(self) -> None:
        context, page, errors = self.open_page(
            768,
            900,
            path=self.interactive_page_path,
        )
        try:
            self.assertEqual(page.locator("details.workspace").count(), 0)
            self.assertEqual(page.locator("section.workspace > .workspace-hero .state-badge").count(), 1)
            self.assertTrue(page.locator("[data-review-reason]").is_disabled())
            self.assertTrue(page.locator("[data-review-note]").is_disabled())
            self.assertIn("TOKEN-00", page.locator(".review-original").inner_text())
            self.assertIn("候选 1", page.locator(".review-current").inner_text())
            self.assertGreater(page.locator(".queue-group").count(), 0)
            self.assertEqual(page.locator("details.review-family").count(), 0)
            self.assertNotIn("ADF-", page.locator(".review-current").inner_text())
            page.locator(".technical-details summary").click()
            self.assertIn("ADF-0001-technical", page.locator(".technical-details").inner_text())

            page.locator("[data-review-open='AD-0002']").click()
            self.assertTrue(
                page.evaluate(
                    "document.activeElement?.matches('[data-review-active-heading][tabindex=\"-1\"]') === true"
                )
            )
            self.assertIn("正文与广告混合", page.locator(".mixed-panel").inner_text())
            self.assertTrue(page.locator("[data-review-choice='delete']").is_disabled())
            self.assertIn("尚未支持", page.locator(".mixed-panel").inner_text())

            page.locator("[data-review-open='AD-0003']").click()
            previews = page.locator(".segment-preview")
            self.assertEqual(previews.count(), 2)
            self.assertIn("第 1 / 2 处", previews.nth(0).locator("summary").inner_text())
            self.assertIn("第 2 / 2 处", previews.nth(1).locator("summary").inner_text())
            previews.nth(0).locator("summary").click()
            previews.nth(1).locator("summary").click()
            self.assertIn("访问 example.one", previews.nth(0).inner_text())
            self.assertIn("访问 example.two", previews.nth(1).inner_text())
            self.assertFalse(page.locator("[data-review-choice='delete']").is_disabled())
            self.assertTrue(page.locator("[data-review-reason]").is_disabled())
            self.assertTrue(page.locator("[data-review-note]").is_disabled())
            page.locator("[data-review-choice='keep']").click()
            self.assertFalse(page.locator("[data-review-reason]").is_disabled())
            self.assertFalse(page.locator("[data-review-note]").is_disabled())
            page.locator("[data-review-undo]").click()

            page.locator("[data-review-check='AD-0001']").check()
            page.locator("[data-review-check='AD-0002']").check()
            self.assertTrue(page.locator("[data-review-batch='delete']").is_disabled())
            self.assertIn("无批量删除资格", page.locator("[data-review-batch-status]").inner_text())
            page.locator("[data-review-batch='keep']").click()
            self.assertIn("需要说明", page.locator("[data-review-batch-status]").inner_text())
            self.assertEqual(page.locator("[data-review-selected]").inner_text(), "尚未形成复核请求")
            self.assertTrue(
                page.evaluate("document.activeElement?.matches('[data-review-batch-note]') === true")
            )
            page.locator("[data-review-batch-note]").fill("两个候选均已核对上下文，批量保留。")
            page.locator("[data-review-batch='keep']").click()
            self.assertIn("已形成 2 条复核请求", page.locator("[data-review-selected]").inner_text())
            page.locator("[data-review-copy]").click()
            copied = json.loads(page.evaluate("window.__copiedText"))
            self.assertEqual(len(copied["requests"]), 2)
            self.assertTrue(all(request["note"] for request in copied["requests"]))

            payload = json.loads(page.locator(".review-payload").text_content())
            old_state = {
                "schema_version": 2,
                "review_state_id": "0" * 64,
                "filters": {},
                "page": 1,
                "active_candidate_id": "AD-0001",
                "decisions": {},
                "expanded_technical_ids": [],
                "checked_ids": [],
            }
            page.locator("[data-review-import-progress]").set_input_files(
                {
                    "name": "old-review.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(old_state).encode("utf-8"),
                }
            )
            self.assertIn("已拒绝导入", page.locator("[data-review-copy-status]").inner_text())
            self.assertNotEqual(payload["review_state_id"], old_state["review_state_id"])
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_import_restores_scroll_without_overwriting_saved_state(self) -> None:
        context, page, errors = self.open_page(
            390,
            844,
            path=self.interactive_page_path,
        )
        try:
            payload = json.loads(page.locator(".review-payload").text_content())
            imported = {
                "schema_version": 2,
                "review_state_id": payload["review_state_id"],
                "filters": {"module": "all", "scope": "all", "status": "all", "search": "", "scroll_y": 500},
                "page": 1,
                "active_candidate_id": "AD-0001",
                "decisions": {},
                "expanded_technical_ids": [],
                "checked_ids": [],
            }
            page.locator("[data-review-import-progress]").set_input_files(
                {
                    "name": "saved-progress.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(imported).encode("utf-8"),
                }
            )
            page.wait_for_function("window.scrollY >= 500")
            restored = json.loads(page.locator(".exception-review").get_attribute("data-review-state"))
            self.assertEqual(restored["filters"]["scroll_y"], 500)
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_import_control_has_visible_focus_indicator(self) -> None:
        context, page, errors = self.open_page(390, 844, path=self.interactive_page_path)
        try:
            page.locator("[data-review-import-progress]").focus()
            focus = page.evaluate(
                """
                () => {
                  const input = document.querySelector("[data-review-import-progress]");
                  const label = input.closest("label");
                  return {
                    active: document.activeElement === input,
                    inputOpacity: getComputedStyle(input).opacity,
                    outline: getComputedStyle(label).outlineStyle,
                  };
                }
                """
            )
            self.assertTrue(focus["active"])
            self.assertNotEqual(focus["inputOpacity"], "1")
            self.assertNotEqual(focus["outline"], "none")
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_bad_session_and_hostile_note_are_fail_closed(self) -> None:
        context, page, errors = self.open_page(
            390,
            844,
            path=self.interactive_page_path,
        )
        try:
            payload = json.loads(page.locator(".review-payload").text_content())
            state_key = f"cml-novel-purifier:review:v2:{payload['review_state_id']}"
            page.evaluate("([key]) => sessionStorage.setItem(key, '{bad json')", [state_key])
            page.reload(wait_until="load")
            self.assertEqual(page.locator("[data-review-selected]").inner_text(), "尚未形成复核请求")

            hostile = "</textarea><img src=x onerror='globalThis.pwned=1'>"
            page.locator("[data-review-choice='uncertain']").click()
            page.locator("[data-review-note]").fill(hostile)
            self.assertEqual(page.locator(".review-current img").count(), 0)
            page.locator("[data-review-copy]").click()
            copied = json.loads(page.evaluate("window.__copiedText"))
            self.assertEqual(copied["requests"][0]["note"], hostile)
            self.assertNotIn("original", copied["requests"][0])
            self.assertNotIn("offset", copied["requests"][0])
            self.assertEqual(page.evaluate("globalThis.pwned"), None)

            invalid_state = {
                "schema_version": 2,
                "review_state_id": payload["review_state_id"],
                "filters": {"module": "all", "scope": "all", "status": "all", "search": "", "scroll_y": 0},
                "page": 1,
                "active_candidate_id": "UNKNOWN-ID",
                "decisions": {},
                "expanded_technical_ids": [],
                "checked_ids": [],
            }
            context.close()
            context = self.browser.new_context(viewport={"width": 390, "height": 844})
            context.add_init_script(
                "sessionStorage.setItem("
                + json.dumps(state_key)
                + ", JSON.stringify("
                + json.dumps(invalid_state)
                + "));"
            )
            page = context.new_page()
            page.goto(self.interactive_page_path.as_uri(), wait_until="load")
            restored = json.loads(page.locator(".exception-review").get_attribute("data-review-state"))
            self.assertNotEqual(restored["active_candidate_id"], "UNKNOWN-ID")
            self.assertEqual(restored["decisions"], {})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_clipboard_missing_or_rejected_uses_local_copy_fallback(self) -> None:
        for clipboard_mode in ("missing", "reject"):
            with self.subTest(clipboard_mode=clipboard_mode):
                context, page, errors = self.open_page(
                    390,
                    844,
                    path=self.interactive_page_path,
                    clipboard_mode=clipboard_mode,
                )
                try:
                    page.locator("[data-review-choice='keep']").first.click()
                    page.locator("[data-review-note]").fill("确认保留剧情正文")
                    page.locator("[data-review-copy]").click()
                    page.wait_for_function(
                        "document.querySelector('[data-review-copy-status]').textContent.includes('复核请求 JSON 已复制')"
                    )
                    copied = json.loads(page.evaluate("window.__fallbackCopiedText"))
                    self.assertEqual(copied["schema"], "cml.review-request.v1")
                    self.assertEqual(copied["requests"][0]["candidate_id"], "AD-0001")
                    self.assertEqual(errors, [])
                finally:
                    context.close()

    def test_batch_index_opens_real_isolated_child_page(self) -> None:
        context, page, errors = self.open_page(
            390,
            844,
            path=self.batch_page_path,
        )
        try:
            links = page.locator(".batch-open")
            self.assertEqual(links.count(), 2)
            first_book = self.batch_books[0]
            other_book = self.batch_books[1]
            self.assertEqual(links.first.get_attribute("href"), first_book["html"])

            links.first.click()
            page.wait_for_load_state("load")

            self.assertTrue(page.url.endswith(first_book["html"]))
            self.assertIn(Path(first_book["name"]).stem, page.locator("body").inner_text())
            self.assertNotIn(Path(other_book["name"]).stem, page.locator("body").inner_text())
            self.assertEqual(page.locator("main").count(), 1)
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_automatic_semantic_checks_find_no_serious_issue(self) -> None:
        context, page, errors = self.open_page(375, 812)
        try:
            issues = page.evaluate(
                """
                () => {
                  const issues = [];
                  if (document.documentElement.lang !== "zh-CN") issues.push("missing document language");
                  if (document.querySelectorAll("main").length !== 1) issues.push("main landmark count");
                  if (document.querySelectorAll("h1").length !== 1) issues.push("h1 count");
                  const ids = [...document.querySelectorAll("[id]")].map((node) => node.id);
                  if (new Set(ids).size !== ids.length) issues.push("duplicate ids");
                  document.querySelectorAll("button").forEach((button) => {
                    if (!(button.getAttribute("aria-label") || button.textContent || "").trim()) {
                      issues.push("unnamed button");
                    }
                  });
                  document.querySelectorAll("input,select").forEach((control) => {
                    if (!control.closest("label") && !control.getAttribute("aria-label")) {
                      issues.push("unlabelled form control");
                    }
                  });
                  document.querySelectorAll("details").forEach((details) => {
                    if (!details.querySelector(":scope > summary")) issues.push("details without summary");
                  });
                  document.querySelectorAll("[aria-controls]").forEach((control) => {
                    if (!document.getElementById(control.getAttribute("aria-controls"))) {
                      issues.push("broken aria-controls");
                    }
                  });
                  document.querySelectorAll("[role='status']").forEach((status) => {
                    if (status.getAttribute("aria-live") !== "polite") issues.push("silent status update");
                  });
                  document.querySelectorAll(".status").forEach((status) => {
                    if (!(status.textContent || "").trim()) issues.push("color-only status");
                  });
                  return issues;
                }
                """
            )
            self.assertEqual(issues, [])
            self.assertEqual(errors, [])
        finally:
            context.close()


if __name__ == "__main__":
    unittest.main()
