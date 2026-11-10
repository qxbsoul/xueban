from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xueban.memory.memory_bank import MemoryBank, MemoryBankError, create_memory_router


@pytest.fixture()
def client_and_bank(tmp_path: Path):
    bank = MemoryBank(tmp_path / "knowledge")
    app = FastAPI()
    app.include_router(create_memory_router(bank))
    return TestClient(app), bank


def test_patch_pack_updates_display_fields_only(client_and_bank):
    client, bank = client_and_bank
    pack = bank.create_pack("旧名称", "旧描述", metadata={"company_id": "co1"})
    source = bank.ingest_text(pack["pack_id"], "公司续费规则", source_name="规则.txt")

    response = client.patch(
        f"/api/memory-bank/packs/{pack['pack_id']}",
        json={"name": "  新名称  ", "description": "新描述"},
    )

    assert response.status_code == 200
    updated = response.json()["data"]
    assert updated["name"] == "新名称"
    assert updated["description"] == "新描述"
    assert updated["metadata"] == {"company_id": "co1"}
    assert updated["created_at"] == pack["created_at"]
    assert updated["stats"]["sources"] == 1
    assert bank.get_source(pack["pack_id"], source["source_id"])["filename"] == "规则.txt"


def test_patch_pack_rejects_blank_name_without_changes(client_and_bank):
    client, bank = client_and_bank
    pack = bank.create_pack("原名称", "原描述")

    response = client.patch(f"/api/memory-bank/packs/{pack['pack_id']}", json={"name": "   "})

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_PACK_NAME"
    assert bank.get_pack(pack["pack_id"])["name"] == "原名称"
    with pytest.raises(MemoryBankError):
        bank.update_pack(pack["pack_id"], name="")


def test_delete_source_removes_only_selected_source(client_and_bank):
    client, bank = client_and_bank
    pack = bank.create_pack("资料")
    first = bank.ingest_text(pack["pack_id"], "第一份资料", source_name="first.txt")
    second = bank.ingest_text(pack["pack_id"], "第二份资料", source_name="second.txt")

    response = client.delete(f"/api/memory-bank/packs/{pack['pack_id']}/sources/{first['source_id']}")

    assert response.status_code == 200
    assert response.json()["data"] == {"deleted": True, "source_id": first["source_id"]}
    remaining = bank.list_sources(pack["pack_id"])
    assert [item["source_id"] for item in remaining] == [second["source_id"]]
    assert bank.get_pack(pack["pack_id"])["stats"]["sources"] == 1
