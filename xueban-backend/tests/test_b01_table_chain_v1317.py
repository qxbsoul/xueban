from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from xueban.memory.memory_bank import MemoryBank, MemoryConfig  # noqa: E402


def test_b01_pdf_parser_output_matches_cross_language_fixture():
    pdf = Path(__file__).resolve().parent / "fixtures" / "B01_相关资料切片.pdf"
    fixture = ROOT / "server" / "src" / "__tests__" / "fixtures" / "b01-page6-table.html"
    with __import__("tempfile").TemporaryDirectory(prefix="b01-chain-") as tmp:
        bank = MemoryBank(Path(tmp), config=MemoryConfig())
        result = bank.extract_text(
            pdf.read_bytes(), pdf.name, "application/pdf",
            max_chars=500000, include_images=False, describe_images=False,
        )
    tables = re.findall(r"<table.*?</table>", result["text"], re.S)
    matches = [item for item in tables if "√0.0625" in item]
    assert len(matches) == 1, "actual PDF parser did not produce exactly one page6 table HTML"
    actual = matches[0].strip()
    expected = fixture.read_text(encoding="utf-8").strip()
    assert actual == expected
    assert 'data-table-bbox="' in actual
    assert 'data-table-status="structure_validated"' in actual
    assert "content_verified" not in actual
    assert actual.count("√0.0625") == 1
    assert actual.count("<tr>") == 2
    assert actual.count("<td>") == 18
    records = {item["page"]: item for item in result["audit"]["page_records"]}
    assert records[6]["status"] in {"normal", "minor"}
    assert records[6]["font_repair"]["resources"]