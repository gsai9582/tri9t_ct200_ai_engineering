import json, os, uuid
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy.orm import Session
from app.models import Selection, SelectionItem, Node, DocumentVersion
def get_store_path() -> Path:
    return Path(os.getenv("GENERATIONS_STORE_PATH", "generated/generations.json"))

def _read():
    p = get_store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists(): return []
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: return []

def all_generations(): return _read()

def save_generation(record):
    records = _read()
    records.append(record)
    p = get_store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    return record


def create_selection(db: Session, name: str, node_ids: list[str]):
    nodes = [db.get(Node, node_id) for node_id in node_ids]
    if any(n is None for n in nodes):
        raise ValueError("One or more node IDs do not exist")
    versions = {n.version_id for n in nodes}
    if len(versions) != 1:
        raise ValueError("All selected nodes must belong to the same document version")
    version = db.get(DocumentVersion, next(iter(versions)))
    selection = Selection(name=name, document_id=version.document_id)
    db.add(selection); db.flush()
    for n in nodes:
        db.add(SelectionItem(selection_id=selection.id, node_id=n.id, version_id=n.version_id,
                             logical_id=n.logical_id, content_hash=n.content_hash,
                             heading_snapshot=n.heading, text_snapshot=n.full_text))
    db.commit(); db.refresh(selection)
    return selection

def selection_payload(db: Session, selection_id: str):
    selection = db.get(Selection, selection_id)
    if not selection: return None
    items = []
    for i in selection.items:
        v = db.get(DocumentVersion, i.version_id)
        items.append({"node_id": i.node_id, "logical_id": i.logical_id, "version_id": i.version_id,
                      "version_number": v.version_number, "heading": i.heading_snapshot,
                      "text": i.text_snapshot, "content_hash": i.content_hash})
    return {"id": selection.id, "name": selection.name, "items": items}

def staleness_for_source(db: Session, source_ref: dict):
    node_id = source_ref.get("node_id")
    node = db.get(Node, node_id)
    version = db.get(DocumentVersion, source_ref.get("version_id"))
    if not node or not version:
        return {"node_id": node_id, "status": "source_missing", "changed": True}
    latest = db.query(DocumentVersion).filter_by(document_id=version.document_id).order_by(DocumentVersion.version_number.desc()).first()
    if latest.id == version.id:
        return {"node_id": node_id, "status": "current", "changed": False, "latest_version": latest.version_number}
    peer = db.query(Node).filter_by(version_id=latest.id, logical_id=source_ref.get("logical_id")).first()
    if not peer:
        return {"node_id": node_id, "status": "source_removed", "changed": True, "latest_version": latest.version_number}
    if peer.content_hash == source_ref.get("content_hash"):
        return {"node_id": node_id, "status": "current", "changed": False, "latest_version": latest.version_number}
    return {"node_id": node_id, "status": "stale", "changed": True, "latest_version": latest.version_number,
            "old_hash": source_ref.get("content_hash"), "current_hash": peer.content_hash,
            "note": "Hash comparison detects any text change, not clinical significance."}

