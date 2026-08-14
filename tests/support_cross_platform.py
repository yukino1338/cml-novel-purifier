from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import export_outputs  # noqa: E402
import preprocess  # noqa: E402


SOURCE_TEXT_CRLF = (
    "匿名😀样本\r\n"
    "第一章 起点\r\n"
    "正文甲，ASCII v1.2.3。\r\n"
)
NORMALIZED_TEXT = SOURCE_TEXT_CRLF.replace("\r\n", "\n")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def epub_semantic_text(path: Path) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(path, "r") as archive:
        chapter_names = sorted(
            name
            for name in archive.namelist()
            if name.startswith("OEBPS/Text/chapter-") and name.endswith(".xhtml")
        )
        for name in chapter_names:
            document = ET.fromstring(archive.read(name))
            body = document.find("{http://www.w3.org/1999/xhtml}body")
            if body is None:
                raise ValueError(f"EPUB chapter has no body: {name}")
            parts.append("".join(body.itertext()))
    return export_outputs.semantic_text("".join(parts))


def run_probe() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        unicode_root = Path(directory) / "兼容 路径 😀"
        unicode_root.mkdir()
        source = unicode_root / "匿名 文本😀.txt"
        source.write_bytes(SOURCE_TEXT_CRLF.encode("utf-8"))

        workspace = preprocess.run(source)
        output = (workspace / "versions/v1_preprocessed.txt").read_text(encoding="utf-8")
        report = json.loads(
            (workspace / "report/preprocess_report.json").read_text(encoding="utf-8")
        )
        if output != NORMALIZED_TEXT:
            raise AssertionError("UTF-8/CRLF preprocessing changed semantic text")

        markdown = export_outputs.markdown_from_text(output)
        markdown_semantic = export_outputs.semantic_text(
            export_outputs.markdown_body_text(markdown)
        )
        epub = unicode_root / "导出 结果😀.epub"
        export_outputs.write_epub(epub, output, "匿名 标题😀", "匿名作者", "zh-CN")
        epub_check = export_outputs.validate_epub(epub, output)
        epub_semantic = epub_semantic_text(epub)

        semantic = export_outputs.semantic_text(output)
        return {
            "schema_version": 1,
            "stdout_encoding": (sys.stdout.encoding or "").lower(),
            "filesystem_encoding": sys.getfilesystemencoding().lower(),
            "unicode_path_roundtrip": source.name == "匿名 文本😀.txt" and epub.is_file(),
            "crlf_count": report["metrics"]["crlf_count"],
            "v1_sha256": sha256_text(output),
            "semantic_sha256": sha256_text(semantic),
            "markdown_semantic_sha256": sha256_text(markdown_semantic),
            "epub_semantic_sha256": sha256_text(epub_semantic),
            "epub_passed": epub_check["passed"],
            "epub_chapter_count": epub_check["chapter_count"],
        }


if __name__ == "__main__":
    print(json.dumps(run_probe(), sort_keys=True))
