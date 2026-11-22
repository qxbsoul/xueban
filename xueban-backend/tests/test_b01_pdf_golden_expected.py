from pathlib import Path
import sys
PDF = Path(r"C:\Users\13810\Desktop\【赛题材料】智慧学伴\赛题数据\赛题资料包\5_课程标准及相关教学资源\B01_相关资料切片.pdf")
sys.path.insert(0, r"D:\capital lion\xueban\xueban-backend")
from xueban.memory.memory_bank import MemoryBank, MemoryConfig, _clean_text, _pdf_grid_table

def extracted_pages():
    bank = MemoryBank(Path(r"D:\capital lion\_golden_expected_bank"), config=MemoryConfig())
    result = bank.extract_text(PDF.read_bytes(), PDF.name, "application/pdf", max_chars=500000, include_images=False, describe_images=False)
    records = {item["page"]: item for item in result["audit"]["page_records"]}
    return {page: _clean_text(records[page]["raw_text"]) for page in (3, 6, 7)}, records

def test_page3_formula_superscripts():
    pages, _ = extracted_pages()
    text = pages[3]
    assert "(±8)^2=64" in text, text
    assert "(±310)^2=9100" in text, text
    assert "(±0.1)^2=0.01" in text, text
    assert "√a" in text and "√0" in text

def test_page6_table_grid_is_recovered_without_fake_header():
    pages, records = extracted_pages()
    text = pages[6]
    assert "用计算器计算下表中的算术平方根" in text
    # 用 PDF 矢量网格恢复真实行列；第一行是表达式数据，不是表头。
    import pymupdf
    document = pymupdf.open(PDF)
    page = document[5]
    tables = page.find_tables().tables
    assert tables
    rows = _pdf_grid_table(page, tables[0].bbox)
    document.close()
    assert rows is not None, "vector grid table not recovered"
    assert len(rows) == 2
    assert len(rows[0]) == 9
    assert rows[0][1].replace(" ", "").startswith("√0.0625")
    assert rows[0][-1].strip() == "…"
    assert all(cell == "" for cell in rows[1][1:-1])

def test_page7_example5_math():
    pages, _ = extracted_pages()
    text = pages[7]
    for expected in ("400cm²", "300cm²", "3x·2x=300", "6x^2=300", "x^2=50", "x=√50"):
        assert expected in text, (expected, text)