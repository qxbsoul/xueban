from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from xueban.memory.memory_bank import (  # noqa: E402
    MemoryBank, MemoryConfig, _font_repair_diagnostics, _font_mapped_span_text,
)


class FakePage:
    def __init__(self, spans, fonts):
        self.spans = spans
        self.fonts = fonts

    def get_texttrace(self):
        return self.spans

    def get_fonts(self, full=False):
        return self.fonts


def _span(font, pairs):
    return {"font": font, "chars": [(ord(source), gid, (0, 0), (0, 0, 1, 1)) for source, gid in pairs]}


def test_a_cross_font_same_source_does_not_pollute():
    page = FakePage([
        _span("E-BX9-A", [("Q", 0x53)]),
        _span("E-BX9-B", [("Q", 0x62)]),
    ], [(1, "cid", "Type0", "E-BX9-A", "A", "", 0), (2, "cid", "Type0", "E-BX9-B", "B", "", 0)])
    maps, conflicts, ambiguous = _font_repair_diagnostics(page)
    assert maps["E-BX9-A"]["Q"] == "R"
    assert maps["E-BX9-B"]["Q"] == "a"
    assert _font_mapped_span_text(_span("E-BX9-A", [("Q", 0)]), maps) == "R"
    assert _font_mapped_span_text(_span("E-BX9-B", [("Q", 0)]), maps) == "a"
    assert not conflicts


def test_b_same_font_conflict_is_not_applied():
    page = FakePage([
        _span("E-BX9-A", [("Q", 0x53)]),
        _span("E-BX9-A", [("Q", 0x62)]),
    ], [(1, "cid", "Type0", "E-BX9-A", "A", "", 0)])
    maps, conflicts, _ = _font_repair_diagnostics(page)
    assert "Q" not in maps.get("E-BX9-A", {})
    assert conflicts["E-BX9-A"]["Q"] == ["R", "a"]


def test_c_alternate_text_keeps_font_identity_raw():
    pdf = Path(__file__).resolve().parent / "fixtures" / "B01_相关资料切片.pdf"
    with __import__("tempfile").TemporaryDirectory(prefix="v1314-font-") as tmp:
        bank = MemoryBank(Path(tmp), config=MemoryConfig())
        result = bank.extract_text(pdf.read_bytes(), pdf.name, "application/pdf", max_chars=200000, include_images=False, describe_images=False)
    raw = result["audit"]["page_records"][0]["alternate_text"]
    assert "\u72cf" in raw or "\u72b5" in raw or "\u69e1" in raw
    assert "\u72cf" not in result["text"] and "\u72b5" not in result["text"]
