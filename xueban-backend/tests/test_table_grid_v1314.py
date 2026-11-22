from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from xueban.memory.memory_bank import _grid_table_reliable

class _Page:
    class rect:
        width = 595.0
        height = 842.0

def test_d_bad_grids_are_not_reliable():
    page = _Page()
    bbox = (10, 10, 300, 100)
    assert not _grid_table_reliable([["a", "b"], ["c"]], bbox, page)
    assert not _grid_table_reliable([["", ""], ["", ""]], bbox, page)
    assert not _grid_table_reliable([["a", "b"], ["c", "d"]], (0, 0, 590, 800), page)
    assert not _grid_table_reliable([["a", "b"], ["c", "x" * 301]], bbox, page)
    assert _grid_table_reliable([["a", "b"], ["c", ""]], bbox, page)