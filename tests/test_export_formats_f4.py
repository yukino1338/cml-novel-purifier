from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import apply_decisions  # noqa: E402
import export_outputs  # noqa: E402
import preprocess  # noqa: E402
import verify  # noqa: E402
from tests.support_formal_ads import formalize_clean_ads  # noqa: E402


class ExportFormatsF4Tests(unittest.TestCase):
    @staticmethod
    def rewrite_epub_member(
        path: Path,
        member: str,
        before: str,
        after: str,
    ) -> None:
        replacement = path.with_suffix(".tmp")
        with (
            zipfile.ZipFile(path, "r") as source,
            zipfile.ZipFile(replacement, "w") as target,
        ):
            for entry in source.infolist():
                payload = source.read(entry.filename)
                if entry.filename == member:
                    text = payload.decode("utf-8")
                    if before not in text:
                        raise AssertionError(f"missing EPUB fixture fragment: {before}")
                    payload = text.replace(before, after, 1).encode("utf-8")
                target.writestr(entry, payload)
        replacement.replace(path)

    @staticmethod
    def add_epub_member(path: Path, member: str, payload: str) -> None:
        replacement = path.with_suffix(".tmp")
        with (
            zipfile.ZipFile(path, "r") as source,
            zipfile.ZipFile(replacement, "w") as target,
        ):
            for entry in source.infolist():
                target.writestr(entry, source.read(entry.filename))
            target.writestr(member, payload.encode("utf-8"))
        replacement.replace(path)

    def make_verified_workspace(self, root: Path, text: str) -> Path:
        source = root / "anonymous-book.txt"
        source.write_text(text, encoding="utf-8")
        workspace = preprocess.run(source)
        formalize_clean_ads(workspace)
        apply_decisions.run(
            workspace,
            "ads",
            "versions/v1_preprocessed.txt",
            "decisions/ads_decisions.jsonl",
            "versions/v2_ads_removed.txt",
            "2_ads",
        )
        report = verify.run(
            workspace,
            "ads",
            "versions/v1_preprocessed.txt",
            "versions/v2_ads_removed.txt",
            "decisions/ads_decisions.jsonl",
            False,
        )
        self.assertEqual(report["status"], "passed")
        return workspace

    def test_txt_markdown_and_epub_preserve_the_same_semantic_text(self) -> None:
        text = (
            "匿名前置甲\n匿名前置乙\n\n"
            "第一章 起点\n正文甲。\n第一章 起点\n正文中重复标题之后的文字。\n"
            "番外 一封信\n正文乙。\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.make_verified_workspace(root, text)

            report = export_outputs.run(
                workspace,
                "auto",
                None,
                root / "exports",
                requested_formats=export_outputs.ALL_FORMATS,
            )
            current = workspace / "versions/v2_ads_removed.txt"
            txt = Path(report["outputs"]["txt"])
            markdown = Path(report["outputs"]["markdown"])
            epub = Path(report["outputs"]["epub"])

            self.assertEqual(txt.read_bytes(), current.read_bytes())
            self.assertEqual(
                export_outputs.semantic_text(
                    export_outputs.markdown_body_text(markdown.read_text(encoding="utf-8"))
                ),
                export_outputs.semantic_text(text),
            )
            self.assertTrue(export_outputs.validate_epub(epub, text)["passed"])
            for kind, path in (("txt", txt), ("markdown", markdown), ("epub", epub)):
                artifact = report["output_artifacts"][kind]
                self.assertEqual(artifact["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertEqual(artifact["size_bytes"], path.stat().st_size)
                self.assertTrue(artifact["self_check"]["passed"])
            self.assertEqual(report["requested_formats"], ["txt", "markdown", "epub"])
            self.assertEqual(report["produced_formats"], ["txt", "markdown", "epub"])
            self.assertEqual(report["primary_output"], report["outputs"]["txt"])

    def test_no_chapter_text_is_not_dropped_or_given_a_synthetic_body_heading(self) -> None:
        text = "匿名说明\n这是一篇没有章节标题的短正文。\n正文继续。\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.make_verified_workspace(root, text)

            report = export_outputs.run(
                workspace,
                "auto",
                None,
                root / "exports",
                requested_formats=("markdown",),
            )
            markdown = Path(report["outputs"]["markdown"]).read_text(encoding="utf-8")

            self.assertNotIn("## 正文", markdown)
            self.assertEqual(
                export_outputs.semantic_text(markdown),
                export_outputs.semantic_text(text),
            )

    def test_default_exports_only_txt_and_does_not_touch_epub(self) -> None:
        text = "第一章 起点\n这是正文。\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.make_verified_workspace(root, text)

            with mock.patch.object(
                export_outputs,
                "write_epub",
                side_effect=OSError("EPUB must remain isolated"),
            ) as write_epub:
                report = export_outputs.run(workspace, "auto", None, root / "exports")

            write_epub.assert_not_called()
            self.assertEqual(report["requested_formats"], ["txt"])
            self.assertEqual(report["produced_formats"], ["txt"])
            self.assertEqual(set(report["outputs"]), {"txt"})
            self.assertEqual(report["primary_output"], report["outputs"]["txt"])
            output_dir = Path(report["output_dir_abs"])
            self.assertEqual([path.suffix for path in output_dir.iterdir()], [".txt"])

    def test_requested_formats_are_validated_and_have_stable_order(self) -> None:
        self.assertEqual(export_outputs.normalize_requested_formats(None), ("txt",))
        self.assertEqual(
            export_outputs.normalize_requested_formats(("epub", "txt", "epub")),
            ("txt", "epub"),
        )
        with self.assertRaisesRegex(ValueError, "at least one"):
            export_outputs.normalize_requested_formats(())
        with self.assertRaisesRegex(ValueError, "iterable of format names"):
            export_outputs.normalize_requested_formats("txt")
        with self.assertRaisesRegex(ValueError, "only format names"):
            export_outputs.normalize_requested_formats(("txt", 1))
        with self.assertRaisesRegex(ValueError, "unsupported export format"):
            export_outputs.normalize_requested_formats(("pdf",))

        incomplete_plan = {
            "workspace": Path("workspace"),
            "requested_formats": ("txt", "markdown"),
        }
        with self.assertRaisesRegex(ValueError, "every requested format"):
            export_outputs.export_report(incomplete_plan, {"txt": "book.txt"}, {})

    def test_markdown_and_epub_preserve_indent_tabs_and_blank_lines(self) -> None:
        text = (
            "第一章 起点\n"
            "    四空格缩进正文\n"
            "\t制表符缩进正文\n"
            "\n\n"
            "空白行后的正文\n"
        )
        markdown = export_outputs.markdown_from_text(text)
        self.assertEqual(export_outputs.markdown_body_text(markdown), text)

        with tempfile.TemporaryDirectory() as directory:
            epub = Path(directory) / "anonymous.epub"
            export_outputs.write_epub(epub, text, "匿名书名", "匿名作者", "zh-CN")
            with zipfile.ZipFile(epub) as archive:
                chapter = archive.read("OEBPS/Text/chapter-0001.xhtml").decode("utf-8")
            self.assertTrue(export_outputs.validate_epub(epub, text)["passed"])

        self.assertIn("    四空格缩进正文", chapter)
        self.assertIn("\t制表符缩进正文", chapter)
        self.assertIn("\n\n空白行后的正文", chapter)

    def test_epub_self_check_rejects_inconsistent_package_mappings(self) -> None:
        text = "Chapter 1 Start\nAlpha\nChapter 2 Next\nBeta\n"
        mutations = (
            (
                "nav-order",
                "OEBPS/Text/nav.xhtml",
                (
                    '<li><a href="chapter-0001.xhtml">Chapter 1 Start</a></li>\n'
                    '<li><a href="chapter-0002.xhtml">Chapter 2 Next</a></li>'
                ),
                (
                    '<li><a href="chapter-0002.xhtml">Chapter 2 Next</a></li>\n'
                    '<li><a href="chapter-0001.xhtml">Chapter 1 Start</a></li>'
                ),
            ),
            (
                "manifest-href",
                "OEBPS/content.opf",
                'id="c2" href="Text/chapter-0002.xhtml"',
                'id="c2" href="Text/chapter-0001.xhtml"',
            ),
            (
                "manifest-media-type",
                "OEBPS/content.opf",
                (
                    'id="c2" href="Text/chapter-0002.xhtml" '
                    'media-type="application/xhtml+xml"'
                ),
                'id="c2" href="Text/chapter-0002.xhtml" media-type="text/plain"',
            ),
            (
                "spine-order",
                "OEBPS/content.opf",
                '<itemref idref="c1"/>\n<itemref idref="c2"/>',
                '<itemref idref="c2"/>\n<itemref idref="c1"/>',
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, member, before, after in mutations:
                epub = root / f"{label}.epub"
                export_outputs.write_epub(epub, text, "Book", "Author", "en")
                self.rewrite_epub_member(epub, member, before, after)
                with self.subTest(label=label), self.assertRaises(ValueError):
                    export_outputs.validate_epub(epub, text)

    def test_epub_self_check_uses_one_safe_container_rootfile(self) -> None:
        text = "Chapter 1 Start\nAlpha\n"
        rootfile = (
            '<rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/>'
        )
        mutations = (
            (
                "missing-full-path",
                rootfile,
                '<rootfile media-type="application/oebps-package+xml"/>',
            ),
            (
                "missing-package",
                rootfile,
                (
                    '<rootfile full-path="OEBPS/missing.opf" '
                    'media-type="application/oebps-package+xml"/>'
                ),
            ),
            (
                "absolute-package",
                rootfile,
                (
                    '<rootfile full-path="/OEBPS/content.opf" '
                    'media-type="application/oebps-package+xml"/>'
                ),
            ),
            (
                "escaping-package",
                rootfile,
                (
                    '<rootfile full-path="../OEBPS/content.opf" '
                    'media-type="application/oebps-package+xml"/>'
                ),
            ),
            (
                "wrong-media-type",
                rootfile,
                '<rootfile full-path="OEBPS/content.opf" media-type="text/xml"/>',
            ),
            ("multiple-rootfiles", rootfile, f"{rootfile}{rootfile}"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, before, after in mutations:
                epub = root / f"{label}.epub"
                export_outputs.write_epub(epub, text, "Book", "Author", "en-GB")
                self.rewrite_epub_member(
                    epub,
                    "META-INF/container.xml",
                    before,
                    after,
                )
                with self.subTest(label=label), self.assertRaises(ValueError):
                    export_outputs.validate_epub(epub, text)

            alternate = root / "alternate-rootfile.epub"
            export_outputs.write_epub(alternate, text, "Book", "Author", "en-GB")
            self.add_epub_member(alternate, "OEBPS/alternate.opf", "<not-opf")
            self.rewrite_epub_member(
                alternate,
                "META-INF/container.xml",
                'full-path="OEBPS/content.opf"',
                'full-path="OEBPS/alternate.opf"',
            )
            with self.assertRaises(ValueError):
                export_outputs.validate_epub(alternate, text)

    def test_epub_self_check_rejects_wrong_xml_roots(self) -> None:
        text = "Chapter 1 Start\nAlpha\n"
        mutations = (
            (
                "container",
                "META-INF/container.xml",
                ("<container version=", "<wrong-container version="),
                ("</container>", "</wrong-container>"),
            ),
            (
                "package",
                "OEBPS/content.opf",
                ("<package xmlns=", "<wrong-package xmlns="),
                ("</package>", "</wrong-package>"),
            ),
            (
                "navigation",
                "OEBPS/Text/nav.xhtml",
                ("<html xmlns=", "<wrong-html xmlns="),
                ("</html>", "</wrong-html>"),
            ),
            (
                "chapter",
                "OEBPS/Text/chapter-0001.xhtml",
                ("<html xmlns=", "<wrong-html xmlns="),
                ("</html>", "</wrong-html>"),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, member, opening, closing in mutations:
                epub = root / f"wrong-{label}-root.epub"
                export_outputs.write_epub(epub, text, "Book", "Author", "en-GB")
                self.rewrite_epub_member(epub, member, *opening)
                self.rewrite_epub_member(epub, member, *closing)
                with self.subTest(label=label), self.assertRaises(ValueError):
                    export_outputs.validate_epub(epub, text)

    def test_epub_self_check_rejects_ambiguous_direct_structures(self) -> None:
        text = "Chapter 1 Start\nAlpha\n"
        mutations = (
            (
                "duplicate-metadata",
                "OEBPS/content.opf",
                (
                    (
                        (
                            '<metadata xmlns:dc="'
                            'http://purl.org/dc/elements/1.1/">'
                        ),
                        (
                            '<metadata xmlns:dc="'
                            'http://purl.org/dc/elements/1.1/"></metadata>'
                            '<metadata xmlns:dc="'
                            'http://purl.org/dc/elements/1.1/">'
                        ),
                    ),
                ),
            ),
            (
                "duplicate-manifest",
                "OEBPS/content.opf",
                (("<manifest>", "<manifest></manifest><manifest>"),),
            ),
            (
                "duplicate-spine",
                "OEBPS/content.opf",
                (("<spine>", "<spine></spine><spine>"),),
            ),
            (
                "language-outside-metadata",
                "OEBPS/content.opf",
                (
                    ("<dc:language>en-GB</dc:language>", ""),
                    (
                        "</metadata>",
                        (
                            "</metadata><dc:language "
                            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                            "en-GB</dc:language>"
                        ),
                    ),
                ),
            ),
            (
                "duplicate-navigation-body",
                "OEBPS/Text/nav.xhtml",
                (("</body>", "</body><body></body>"),),
            ),
            (
                "duplicate-chapter-body",
                "OEBPS/Text/chapter-0001.xhtml",
                (("</body>", "</body><body></body>"),),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, member, replacements in mutations:
                epub = root / f"{label}.epub"
                export_outputs.write_epub(epub, text, "Book", "Author", "en-GB")
                for before, after in replacements:
                    self.rewrite_epub_member(epub, member, before, after)
                with self.subTest(label=label), self.assertRaises(ValueError):
                    export_outputs.validate_epub(epub, text)

    def test_epub_language_is_consistent_across_package_and_xhtml(self) -> None:
        text = "Chapter 1 Start\nAlpha\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            epub = root / "language.epub"
            export_outputs.write_epub(epub, text, "Book", "Author", "en-GB")
            with zipfile.ZipFile(epub) as archive:
                opf = archive.read("OEBPS/content.opf").decode("utf-8")
                nav = archive.read("OEBPS/Text/nav.xhtml").decode("utf-8")
                chapter = archive.read("OEBPS/Text/chapter-0001.xhtml").decode("utf-8")
            self.assertIn("<dc:language>en-GB</dc:language>", opf)
            for document in (nav, chapter):
                self.assertIn('lang="en-GB"', document)
                self.assertIn('xml:lang="en-GB"', document)
            self.assertTrue(export_outputs.validate_epub(epub, text)["passed"])

            mutations = (
                (
                    "missing-opf-language",
                    "OEBPS/content.opf",
                    "<dc:language>en-GB</dc:language>",
                    "",
                ),
                (
                    "nav-language",
                    "OEBPS/Text/nav.xhtml",
                    'lang="en-GB"',
                    'lang="fr"',
                ),
                (
                    "chapter-language",
                    "OEBPS/Text/chapter-0001.xhtml",
                    'xml:lang="en-GB"',
                    'xml:lang="fr"',
                ),
            )
            for label, member, before, after in mutations:
                mutated = root / f"{label}.epub"
                export_outputs.write_epub(mutated, text, "Book", "Author", "en-GB")
                self.rewrite_epub_member(mutated, member, before, after)
                with self.subTest(label=label), self.assertRaises(ValueError):
                    export_outputs.validate_epub(mutated, text)

    def test_corrupt_epub_self_check_rolls_back_the_entire_delivery(self) -> None:
        text = "第一章 起点\n" + "正文甲。" * 20 + "\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.make_verified_workspace(root, text)
            manifest_before = (workspace / "manifest.json").read_bytes()
            output_root = root / "exports"
            original = export_outputs.write_epub

            def write_corrupt(path: Path, *_args: object) -> None:
                path.write_bytes(b"not-an-epub")

            export_outputs.write_epub = write_corrupt
            try:
                with self.assertRaisesRegex(ValueError, "EPUB"):
                    export_outputs.run(
                        workspace,
                        "auto",
                        None,
                        output_root,
                        requested_formats=export_outputs.ALL_FORMATS,
                    )
            finally:
                export_outputs.write_epub = original

            self.assertEqual((workspace / "manifest.json").read_bytes(), manifest_before)
            self.assertFalse(output_root.exists() and any(output_root.rglob("*")))

    def test_output_names_are_portable_and_collisions_never_reuse_a_directory(self) -> None:
        self.assertEqual(export_outputs.safe_name(" CON. "), "_CON")
        self.assertEqual(export_outputs.safe_name("aux.txt"), "_aux.txt")
        self.assertEqual(export_outputs.safe_name("Cafe\u0301"), "Caf\u00e9")
        self.assertLessEqual(len(export_outputs.safe_name("书" * 200).encode("utf-8")), 90)

        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            (parent / "Caf\u00e9").mkdir()
            normalized_collision = export_outputs.unique_child_dir(parent, "CAF\u0045\u0301")
            self.assertEqual(normalized_collision.name, "CAF\u00c9-2")

            reserved = {export_outputs.portable_path_key(parent / "book")}
            case_collision = export_outputs.reserved_child_dir(parent, "BOOK", reserved)
            self.assertEqual(case_collision.name, "BOOK-2")

    def test_portable_path_limit_fails_before_delivery(self) -> None:
        overlong = Path("x" * (export_outputs.PORTABLE_PATH_LIMIT + 1))
        with self.assertRaisesRegex(ValueError, "too long"):
            export_outputs._assert_portable_paths([overlong])


if __name__ == "__main__":
    unittest.main()
