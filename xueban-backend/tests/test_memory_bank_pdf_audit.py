"""PDF 解析可读性回归测试（2026-11-22）。"""
from __future__ import annotations

import pymupdf
from pathlib import Path

from xueban.memory.memory_bank import MemoryBank, MemoryConfig


def _make_pdf(path: Path, pages: list[dict]) -> bytes:
    document = pymupdf.open()
    for spec in pages:
        page = document.new_page()
        if spec.get("text"):
            page.insert_text((72, 72), spec["text"], fontsize=12, fontname=spec.get("font", "china-s"))
        if spec.get("garbled"):
            page.insert_text((72, 140), spec["garbled"], fontsize=12, fontname="helv")
        if spec.get("image"):
            pix = pymupdf.open(stream=spec["image"], filetype="png")[0].get_pixmap()
            page.insert_image(pymupdf.Rect(40, 180, 420, 500), pixmap=pix)
    data = document.tobytes()
    document.close()
    path.write_bytes(data)
    return data


def _bank(tmp_path: Path, **kwargs) -> MemoryBank:
    return MemoryBank(tmp_path / "bank", config=MemoryConfig(**kwargs))


def _parse(bank: MemoryBank, data: bytes, name: str) -> dict:
    return bank.extract_text(
        data,
        name,
        "application/pdf",
        include_images=True,
        describe_images=False,
        max_chars=500_000,
    )


def _sample_image() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "扫描页中的图片内容", fontname="china-s", fontsize=18)
    data = page.get_pixmap().tobytes("png")
    doc.close()
    return data


def test_normal_chinese_pdf_is_not_garbled(tmp_path: Path):
    pdf = _make_pdf(tmp_path / "normal.pdf", [{
        "text": "第一章 有理数\n正数和负数表示相反意义的量。\n习题 1：计算 2+3。",
    }])
    result = _parse(_bank(tmp_path), pdf, "normal.pdf")
    readability = result["audit"]["readability"]
    assert readability["level"] == "complete"
    assert readability["text_pages"] == 1
    assert readability["fallback_pages"] == 0
    assert "正数和负数" in result["text"]


def test_formula_heavy_pdf_is_not_garbled(tmp_path: Path):
    pdf = _make_pdf(tmp_path / "formula.pdf", [{
        "text": "公式：a + (-a) = 0\n不等式 x ≤ 5，面积 S = πr²。\n计算：(-4) + 6 = 2。",
        "font": "china-s",
    }])
    result = _parse(_bank(tmp_path), pdf, "formula.pdf")
    readability = result["audit"]["readability"]
    assert readability["level"] == "complete"
    assert readability["fallback_pages"] == 0
    assert "x" in result["text"]


def test_substitution_glyph_page_falls_back_to_page_image(tmp_path: Path):
    pdf = _make_pdf(tmp_path / "garbled.pdf", [{
        "garbled": "ᣏᕊ ☒ □ " + ("·" * 16),
    }])
    result = _parse(_bank(tmp_path), pdf, "garbled.pdf")
    audit = result["audit"]
    assert audit["garbled_pages"] == [1]
    assert audit["fallback_pages"] == [1]
    assert audit["readability"]["level"] == "partial"
    assert result["images"][0]["is_fallback"] is True
    assert result["images"][0]["page"] == 1
    assert "ᣏ" not in result["text"]
    assert "☒" not in result["text"]


def test_scanned_page_keeps_page_number_and_image(tmp_path: Path):
    image = _sample_image()
    pdf = _make_pdf(tmp_path / "scan.pdf", [{"text": "", "image": image}])
    result = _parse(_bank(tmp_path), pdf, "scan.pdf")
    audit = result["audit"]
    assert audit["fallback_pages"] == [1]
    assert result["images"][0]["page"] == 1
    assert "页码：1" in result["text"]


def test_mixed_pdf_preserves_normal_table_image_and_garbled_pages(tmp_path: Path):
    image = _sample_image()
    pdf = _make_pdf(tmp_path / "mixed.pdf", [
        {"text": "第一章 有理数\n正常文字段落。"},
        {"text": "| 概念 | 例子 |\n| --- | --- |\n| 正数 | +3 |"},
        {"text": "", "image": image},
        {"garbled": "ᣏᕏ ☒ " + ("·" * 12)},
    ])
    result = _parse(_bank(tmp_path), pdf, "mixed.pdf")
    audit = result["audit"]
    assert audit["pages"] == 4
    assert 1 in audit["text_pages"]
    assert 4 in audit["garbled_pages"]
    assert 4 in audit["fallback_pages"]
    assert any(item["page"] == 4 and item["is_fallback"] for item in result["images"])


def test_fallback_budget_marks_unreadable_pages_instead_of_silent_loss(tmp_path: Path):
    pages = [{"garbled": "☒ " + ("·" * 12)} for _ in range(5)]
    pdf = _make_pdf(tmp_path / "over-budget.pdf", pages)
    result = _parse(_bank(tmp_path, max_pdf_fallback_pages=2), pdf, "over-budget.pdf")
    audit = result["audit"]
    assert audit["fallback_pages"] == [1, 2]
    assert audit["unreadable_pages"] == [3, 4, 5]
    assert audit["readability"]["level"] == "needs_review"
    assert len(result["images"]) == 2


def test_english_and_numeric_page_is_not_garbled(tmp_path: Path):
    pdf = _make_pdf(tmp_path / "english.pdf", [{
        "text": "Chapter 1 Numbers\nPositive and negative numbers.\nTable 1: 1 2 3 4 5\nEquation: x <= 5.",
        "font": "china-s",
    }])
    result = _parse(_bank(tmp_path), pdf, "english.pdf")
    audit = result["audit"]
    assert audit["garbled_pages"] == []
    assert audit["readability"]["level"] == "complete"


def test_visual_budget_omissions_are_recorded(tmp_path: Path):
    pages = [{"garbled": "☒ " + ("·" * 12)} for _ in range(3)]
    pdf = _make_pdf(tmp_path / "visual-budget.pdf", pages)
    result = _parse(_bank(tmp_path, max_pdf_fallback_pages=3, max_visual_images=1), pdf, "visual-budget.pdf")
    audit = result["audit"]
    assert len(result["images"]) == 1
    assert len(audit["images_omitted"]) == 2
    assert audit["skipped_visual_pages"] == [2, 3]
    assert audit["readability"]["level"] == "needs_review"
