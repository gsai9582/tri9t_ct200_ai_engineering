import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.main import app
from app.database import Base, get_db

@pytest.fixture
def client_and_db(tmp_path, monkeypatch):
    test_store = tmp_path / "test_generations.json"
    monkeypatch.setenv("GENERATIONS_STORE_PATH", str(test_store))

    from sqlalchemy.pool import StaticPool
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()


    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()

def test_health_check(client_and_db):
    resp = client_and_db.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

def test_full_api_workflow(client_and_db):
    client = client_and_db

    # 1. Ingest v1
    resp = client.post("/documents/ingest", json={"path": "data/ct200_manual_v1.pdf"})
    assert resp.status_code == 200
    v1_data = resp.json()
    assert v1_data["version"] == 1
    assert v1_data["nodes"] == 28

    # 2. Browse top-level sections
    resp = client.get("/documents/sections?version=1")
    assert resp.status_code == 200
    sec_data = resp.json()
    assert sec_data["version"] == 1
    assert len(sec_data["sections"]) >= 8

    # 3. Search nodes
    resp = client.get("/nodes?q=battery&version=1")
    assert resp.status_code == 200
    search_data = resp.json()
    assert len(search_data["results"]) >= 1
    battery_node = search_data["results"][0]

    # 4. Get specific node with tree
    resp = client.get(f"/nodes/{battery_node['id']}")
    assert resp.status_code == 200
    node_detail = resp.json()
    assert node_detail["id"] == battery_node["id"]
    assert "content_hash" in node_detail

    # 5. Check changes on v1 (should be latest/no later version yet)
    resp = client.get(f"/nodes/{battery_node['id']}/changes")
    assert resp.status_code == 200
    assert resp.json()["status"] == "latest"

    # 6. Create version-pinned selection
    resp = client.post("/selections", json={
        "name": "Battery QA Selection",
        "node_ids": [battery_node["id"]]
    })
    assert resp.status_code == 200
    sel_data = resp.json()
    selection_id = sel_data["id"]
    assert len(sel_data["items"]) == 1

    # 7. Generate QA test cases
    resp = client.post(f"/selections/{selection_id}/generate")
    assert resp.status_code == 200
    gen_data = resp.json()
    assert len(gen_data["test_cases"]) >= 3
    assert gen_data["selection_id"] == selection_id

    # 8. Query generations before v2 re-ingest (should be current)
    resp = client.get(f"/generations?selection_id={selection_id}")
    assert resp.status_code == 200
    retrieval = resp.json()
    assert retrieval["count"] >= 1
    assert retrieval["generations"][0]["is_stale"] is False
    assert retrieval["generations"][0]["staleness"][0]["status"] == "current"

    # 9. Ingest v2
    resp = client.post("/documents/ingest", json={"path": "data/ct200_manual_v2.pdf"})
    assert resp.status_code == 200
    v2_data = resp.json()
    assert v2_data["version"] == 2

    # 10. Check changes on v1 battery node (should now show modified diff)
    resp = client.get(f"/nodes/{battery_node['id']}/changes")
    assert resp.status_code == 200
    change_data = resp.json()
    assert change_data["status"] == "modified"
    assert change_data["changed"] is True
    assert "diff" in change_data

    # 11. Query generations after v2 re-ingest (should now show stale!)
    resp = client.get(f"/generations?selection_id={selection_id}")
    assert resp.status_code == 200
    retrieval_v2 = resp.json()
    assert retrieval_v2["generations"][0]["is_stale"] is True
    assert retrieval_v2["generations"][0]["staleness"][0]["status"] == "stale"
    assert "old_hash" in retrieval_v2["generations"][0]["staleness"][0]
    assert "current_hash" in retrieval_v2["generations"][0]["staleness"][0]
