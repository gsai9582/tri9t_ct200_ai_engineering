import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base
from app.models import Document, DocumentVersion, Node
from app.services.versioning import ingest_pdf, compare_node
from app.services.pdf_extractor import sha256_text

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

def test_hash_changes_when_requirement_changes():
    assert sha256_text("250 cycles") != sha256_text("300 cycles")

def test_same_text_same_hash():
    assert sha256_text("±3 mmHg") == sha256_text("±3 mmHg")

def test_v1_and_v2_ingest_and_logical_matching(test_db):
    # Ingest v1
    doc, v1, details_v1 = ingest_pdf(test_db, "data/ct200_manual_v1.pdf")
    assert v1.version_number == 1
    assert details_v1["nodes"] == 28

    # Idempotent re-ingest of identical source PDF
    _, v1_repeat, details_repeat = ingest_pdf(test_db, "data/ct200_manual_v1.pdf")
    assert details_repeat["unchanged_source"] is True
    assert v1_repeat.id == v1.id

    # Ingest v2
    doc, v2, details_v2 = ingest_pdf(test_db, "data/ct200_manual_v2.pdf")
    assert v2.version_number == 2
    assert details_v2["nodes"] == 29

    v2_nodes = test_db.query(Node).filter_by(version_id=v2.id).all()
    unchanged = [n for n in v2_nodes if n.match_status == "unchanged"]
    modified = [n for n in v2_nodes if n.match_status == "modified"]
    added = [n for n in v2_nodes if n.match_status == "added"]

    assert len(unchanged) == 24
    assert len(modified) == 4
    assert len(added) == 1

    added_heading = added[0].heading
    assert "5.3 Data Export" in added_heading

    modified_headings = [n.heading for n in modified]
    assert any("2.1.1.1" in h for h in modified_headings)
    assert any("3.2" in h for h in modified_headings)
    assert any("4.2" in h for h in modified_headings)
    assert any("4.3" in h for h in modified_headings)

def test_compare_node_diff_summary(test_db):
    ingest_pdf(test_db, "data/ct200_manual_v1.pdf")
    ingest_pdf(test_db, "data/ct200_manual_v2.pdf")

    # Find v1 battery node (2.1.1.1)
    v1_battery = test_db.query(Node).filter(Node.heading.contains("2.1.1.1")).order_by(Node.id.asc()).first()
    assert v1_battery is not None

    diff = compare_node(test_db, v1_battery.id)
    assert diff is not None
    assert diff["status"] == "modified"
    assert diff["changed"] is True
    assert diff["from_version"] == 1
    assert diff["to_version"] == 2
    assert "diff" in diff
    assert "similarity" in diff["diff"]
    assert "300 measurement cycles" in diff["diff"]["old_excerpt"]
    assert "250 measurement cycles" in diff["diff"]["new_excerpt"]

    # Find an unchanged node in v1 (e.g. 1.1 Intended Use)
    v1_intended = test_db.query(Node).filter(Node.heading.contains("1.1 Intended Use")).first()
    diff_unchanged = compare_node(test_db, v1_intended.id)
    assert diff_unchanged["status"] == "unchanged"
    assert diff_unchanged["changed"] is False

