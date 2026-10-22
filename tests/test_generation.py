import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base
from app.models import Document, DocumentVersion, Node, Selection
from app.schemas import GeneratedTestCase
from app.services.versioning import ingest_pdf
from app.services.generation_store import create_selection, selection_payload, staleness_for_source
from app.services.llm_generator import generate_for_selection

@pytest.fixture
def test_db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    db = Session()
    try:
        yield db
    finally:
        db.close()

def test_pydantic_rejects_missing_expected_result():
    with pytest.raises(Exception):
        GeneratedTestCase(
            title="Check pressure",
            requirement="Verify pressure range",
            preconditions=[],
            steps=["Apply pressure"],
            risk="high",
            source_node_id="n1",
            source_hash="abc"
        )

def test_risk_must_be_allowed_value():
    with pytest.raises(Exception):
        GeneratedTestCase(
            title="Check pressure",
            requirement="Verify pressure range",
            preconditions=[],
            steps=["Apply pressure"],
            expected_result="Range is respected",
            risk="urgent",  # Invalid risk value
            source_node_id="n1",
            source_hash="abc"
        )

def test_pydantic_rejects_empty_steps():
    with pytest.raises(Exception):
        GeneratedTestCase(
            title="Check pressure",
            requirement="Verify pressure range",
            preconditions=[],
            steps=[],  # Min length 1 required
            expected_result="Range is respected",
            risk="medium",
            source_node_id="n1",
            source_hash="abc"
        )

def test_selection_is_version_pinned_and_immutable(test_db):
    # Ingest v1
    ingest_pdf(test_db, "data/ct200_manual_v1.pdf")
    v1_battery = test_db.query(Node).filter(Node.heading.contains("2.1.1.1")).first()
    assert v1_battery is not None
    assert "300 measurement cycles" in v1_battery.body_text

    # Create named selection pinned to v1
    sel = create_selection(test_db, "Battery Safety v1", [v1_battery.id])
    assert sel.id is not None

    payload_before = selection_payload(test_db, sel.id)
    assert len(payload_before["items"]) == 1
    assert payload_before["items"][0]["version_number"] == 1
    assert payload_before["items"][0]["content_hash"] == v1_battery.content_hash
    assert "300 measurement cycles" in payload_before["items"][0]["text"]

    # Now ingest v2 (where 2.1.1.1 changes to 250 cycles)
    ingest_pdf(test_db, "data/ct200_manual_v2.pdf")

    # Verify that the old selection payload did NOT mutate
    payload_after = selection_payload(test_db, sel.id)
    assert len(payload_after["items"]) == 1
    assert payload_after["items"][0]["version_number"] == 1
    assert payload_after["items"][0]["content_hash"] == v1_battery.content_hash
    assert "300 measurement cycles" in payload_after["items"][0]["text"]

@pytest.mark.anyio
async def test_staleness_detection_lifecycle(test_db, tmp_path, monkeypatch):
    test_store = tmp_path / "generations.json"
    monkeypatch.setenv("GENERATIONS_STORE_PATH", str(test_store))

    # Ingest v1
    ingest_pdf(test_db, "data/ct200_manual_v1.pdf")
    v1_battery = test_db.query(Node).filter(Node.heading.contains("2.1.1.1")).first()
    v1_intended = test_db.query(Node).filter(Node.heading.contains("1.1 Intended Use")).first()

    # Create selection on both a node that will change (battery) and one that won't (intended use)
    sel = create_selection(test_db, "Mixed Selection", [v1_battery.id, v1_intended.id])

    # Generate test cases against v1
    gen = await generate_for_selection(test_db, sel.id)
    assert len(gen["test_cases"]) >= 3

    # On v1, all source references should be "current"
    for ref in gen["source_refs"]:
        st = staleness_for_source(test_db, ref)
        assert st["status"] == "current"
        assert st["changed"] is False

    # Now ingest v2
    ingest_pdf(test_db, "data/ct200_manual_v2.pdf")

    # Evaluate staleness against latest version
    battery_ref = next(r for r in gen["source_refs"] if r["node_id"] == v1_battery.id)
    intended_ref = next(r for r in gen["source_refs"] if r["node_id"] == v1_intended.id)

    battery_st = staleness_for_source(test_db, battery_ref)
    assert battery_st["status"] == "stale"
    assert battery_st["changed"] is True
    assert battery_st["old_hash"] != battery_st["current_hash"]

    intended_st = staleness_for_source(test_db, intended_ref)
    assert intended_st["status"] == "current"
    assert intended_st["changed"] is False

@pytest.mark.anyio
async def test_generation_idempotency_and_force(test_db, tmp_path, monkeypatch):
    test_store = tmp_path / "generations.json"
    monkeypatch.setenv("GENERATIONS_STORE_PATH", str(test_store))

    ingest_pdf(test_db, "data/ct200_manual_v1.pdf")
    v1_node = test_db.query(Node).filter(Node.heading.contains("1.1 Intended Use")).first()
    sel = create_selection(test_db, "Idempotency Test", [v1_node.id])

    # First generation
    gen1 = await generate_for_selection(test_db, sel.id, force=False)
    assert gen1["id"] is not None

    # Second generation without force returns cached generation
    gen2 = await generate_for_selection(test_db, sel.id, force=False)
    assert gen2["id"] == gen1["id"]
    assert "idempotent" in gen2["duplicate_policy"]

    # Third generation with force=True produces a fresh generation
    gen3 = await generate_for_selection(test_db, sel.id, force=True)
    assert gen3["id"] != gen1["id"]

