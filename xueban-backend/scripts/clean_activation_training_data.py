from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "scripts" / "data" / "training"
sys.path.insert(0, str(ROOT))

from xueban.runtime.training_cleaning import clean_activation_training_file


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean accumulated activation training data")
    parser.add_argument("--input", default=str(DATA_DIR / "activation_samples.jsonl"))
    parser.add_argument("--output", default=str(DATA_DIR / "activation_samples.cleaned.jsonl"))
    parser.add_argument("--report", default=str(DATA_DIR / "activation_samples.cleaned.report.json"))
    args = parser.parse_args()

    stats = clean_activation_training_file(
        input_path=Path(args.input),
        output_path=Path(args.output),
        report_path=Path(args.report),
    )

    print("Training data cleaning complete")
    print(f"  input_records: {stats.input_records}")
    print(f"  output_records: {stats.output_records}")
    print(f"  invalid_json_lines: {stats.invalid_json_lines}")
    print(f"  invalid_schema_lines: {stats.invalid_schema_lines}")
    print(f"  duplicate_records_removed: {stats.duplicate_records_removed}")
    print(f"  sample_id_collisions: {stats.sample_id_collisions}")


if __name__ == "__main__":
    main()
