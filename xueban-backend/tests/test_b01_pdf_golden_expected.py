from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PDF = Path(__file__).resolve().parent / "fixtures" / "B01_相关资料切片.pdf"
sys.path.insert(0, str(ROOT))

from xueban.memory.memory_bank import (  # noqa: E402
    MemoryBank,
    MemoryConfig,
    _clean_text,
    _font_repair_maps,
    _pdf_grid_table,
)


def _extract():
    with tempfile.TemporaryDirectory(prefix="b01-golden-") as tmp:
        bank = MemoryBank(Path(tmp), config=MemoryConfig())
        result = bank.extract_text(
            PDF.read_bytes(), PDF.name, "application/pdf",
            max_chars=500000, include_images=False, describe_images=False,
        )
    records = {item["page"]: item for item in result["audit"]["page_records"]}
    pages = {page: _clean_text(records[page]["raw_text"]) for page in (3, 6, 7)}
    return result, pages, records


def test_page3_formula_superscripts():
    _, pages, _ = _extract()
    text = pages[3]
    assert "(±8)^2=64" in text, text
    assert "(±310)^2=9100" in text, text
    assert "(±0.1)^2=0.01" in text, text
    assert "√a" in text and "√0" in text


def test_page6_table_grid_is_recovered_without_fake_header():
    import pymupdf
    document = pymupdf.open(PDF)
    page = document[5]
    tables = page.find_tables().tables
    assert tables
    rows = _pdf_grid_table(page, tables[0].bbox, _font_repair_maps(page))
    document.close()
    assert rows is not None, "vector grid table not recovered"
    assert len(rows) == 2
    assert len(rows[0]) == 9
    assert rows[0][1].replace(" ", "").startswith("√0.0625")
    assert rows[0][-1].strip() == "…"
    assert all(cell == "" for cell in rows[1][1:-1])


def test_page7_example5_math():
    _, pages, _ = _extract()
    text = pages[7]
    for expected in ("400cm²", "300cm²", "3x·2x=300", "6x^2=300", "x^2=50", "x=√50"):
        assert expected in text, (expected, text)


def test_final_extract_text_and_structured_table():
    result, _, records = _extract()
    text = result["text"]
    assert "狏" not in text and "犵" not in text and "槡" not in text
    assert "(±8)^2=64" in text
    assert "400cm²" in text and "300cm²" in text
    assert "6x^2=300" in text and "x^2=50" in text and "x=√50" in text
    # 第 6 页表格必须进入最终输出，只出现一次，不伪造表头。
    assert 'data-table-reliable="true"' in text
    assert "<th" not in text
    assert text.count("√0.0625") == 1
    assert records[3]["status"] in {"normal", "minor"}
    assert records[6]["status"] in {"normal", "minor"}
    assert records[6]["font_repair"]["fonts"]