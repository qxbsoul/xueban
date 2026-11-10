from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field

from xueban.runtime.contracts import WorkflowDefinition


REPO_ROOT = Path(__file__).resolve().parents[2]
OFFICIAL_EXAMPLES_MANIFEST = REPO_ROOT / "examples" / "runtime-contract" / "official-examples.yaml"


class OfficialExampleSpec(BaseModel):
    id: str
    workflow: str
    input: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    min_steps: int = 1
    expected_terminal_node: str
    expected_artifact_names: List[str] = Field(default_factory=list)
    expected_artifact_writeback_target: str = "host"
    expected_artifact_writeback_mode: str = "reference"
    expected_checkpoint_writeback_target: str = "host"
    expected_checkpoint_writeback_mode: str = "reference"
    expected_parallel_branch_count: Optional[int] = None
    resume_from_checkpoint_node_id: Optional[str] = None
    expected_resumed_nodes: List[str] = Field(default_factory=list)
    expected_resumed_terminal_node: Optional[str] = None
    expected_resume_artifact_names: List[str] = Field(default_factory=list)
    source_legacy_paths: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)

    @property
    def workflow_path(self) -> Path:
        return OFFICIAL_EXAMPLES_MANIFEST.parent / self.workflow


class OfficialExamplesManifest(BaseModel):
    version: str = "0.1"
    examples: List[OfficialExampleSpec] = Field(default_factory=list)


@lru_cache(maxsize=1)
def load_official_examples_manifest() -> OfficialExamplesManifest:
    with OFFICIAL_EXAMPLES_MANIFEST.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    return OfficialExamplesManifest.model_validate(payload)


def list_official_examples() -> List[OfficialExampleSpec]:
    return list(load_official_examples_manifest().examples)


def get_official_example(example_id: str) -> OfficialExampleSpec:
    for example in load_official_examples_manifest().examples:
        if example.id == example_id:
            return example
    raise KeyError(f"official example not found: {example_id}")


def load_official_workflow(example: str | OfficialExampleSpec) -> WorkflowDefinition:
    spec = get_official_example(example) if isinstance(example, str) else example
    with spec.workflow_path.open("r", encoding="utf-8") as handle:
        if spec.workflow_path.suffix.lower() in {".yaml", ".yml"}:
            payload = yaml.safe_load(handle)
        else:
            payload = json.load(handle)
    return WorkflowDefinition.model_validate(payload)
