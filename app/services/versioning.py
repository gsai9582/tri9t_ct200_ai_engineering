import re, hashlib
from difflib import SequenceMatcher
from sqlalchemy.orm import Session
from app.models import Document, DocumentVersion, Node
from app.services.pdf_extractor import extract_pdf, sha256_text
from app.services.hierarchy_parser import parse_blocks

def norm(s: str) -> str:
    return re.sub(r"\W+", " ", s.casefold()).strip()

def stable_key(node, parent_logical="root"):
    # Heading path is primary; duplicate same-title siblings are disambiguated by occurrence index upstream.
    return f"{parent_logical}/{norm(node.heading)}"

def ingest_pdf(db: Session, path: str, document_name: str = "CT-200 Manual"):
    import os
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    blocks, raw_text = extract_pdf(path)
    parsed = parse_blocks(blocks)
    doc = db.query(Document).filter_by(name=document_name).first()
    if not doc:
        doc = Document(name=document_name)
        db.add(doc); db.flush()
    latest = db.query(DocumentVersion).filter_by(document_id=doc.id).order_by(DocumentVersion.version_number.desc()).first()
    source_hash = hashlib.sha256(open(path, "rb").read()).hexdigest()
    if latest and latest.source_sha256 == source_hash:
        return doc, latest, {"unchanged_source": True, "nodes": len(latest.nodes)}
    version_number = 1 if latest is None else latest.version_number + 1
    version = DocumentVersion(document_id=doc.id, version_number=version_number,
                              source_filename=os.path.basename(path), source_sha256=source_hash)
    db.add(version); db.flush()
    old_nodes = latest.nodes if latest else []
    old_by_key = {}
    for old in old_nodes:
        # Matching path uses parent logical identity plus normalized heading; duplicate siblings receive occurrence suffix.
        parent = db.get(Node, old.parent_id) if old.parent_id else None
        key = f"{parent.logical_id if parent else 'root'}/{norm(old.heading)}"
        old_by_key.setdefault(key, []).append(old)
    new_key_to_id = {}  # temp parser key -> persisted node ID
    new_logical_by_temp = {}  # temp parser key -> stable logical ID
    parsed_by_key = {}
    pending = []
    for p in parsed:
        parent_logical = new_logical_by_temp.get(p.parent_key, "root") if p.parent_key else "root"
        base_key = f"{parent_logical}/{norm(p.heading)}"
        occurrence = len(parsed_by_key.get(base_key, []))
        candidates = old_by_key.get(base_key, [])
        if occurrence < len(candidates):
            logical_id = candidates[occurrence].logical_id
        else:
            logical_id = hashlib.sha256((base_key if occurrence == 0 else f"{base_key}#{occurrence}").encode()).hexdigest()[:24]
        parsed_by_key.setdefault(base_key, []).append(p)
        new_logical_by_temp[p.temp_key] = logical_id
        dbnode = Node(version_id=version.id, logical_id=logical_id, heading=p.heading, level=p.level,
                      node_type=p.node_type, body_text=p.body_text, full_text=p.full_text,
                      content_hash=sha256_text(p.full_text), page_start=p.page_start, page_end=p.page_end,
                      match_status="new")
        db.add(dbnode); db.flush()
        new_key_to_id[p.temp_key] = dbnode.id
        pending.append((p, dbnode))
    # Resolve actual parent IDs in a second pass via temp-key to persisted node mapping.
    # Build from insertion order and parsed key mapping.
    temp_to_db = {}
    dbnodes = [n for _, n in pending]
    for p, n in zip(parsed, dbnodes):
        temp_to_db[p.temp_key] = n
    for p, n in zip(parsed, dbnodes):
        n.parent_id = temp_to_db[p.parent_key].id if p.parent_key and p.parent_key in temp_to_db else None
    # Mark match status by logical identity against prior version.
    old_by_logical = {n.logical_id: n for n in old_nodes}
    for n in dbnodes:
        old = old_by_logical.get(n.logical_id)
        if old is None:
            n.match_status = "added"
        elif old.content_hash == n.content_hash:
            n.match_status = "unchanged"
        else:
            n.match_status = "modified"
    current_logicals = {n.logical_id for n in dbnodes}
    for old in old_nodes:
        if old.logical_id not in current_logicals:
            # Old version remains intact; removal is inferred by comparison endpoint.
            pass
    db.commit()
    db.refresh(version)
    return doc, version, {"unchanged_source": False, "nodes": len(dbnodes), "version": version_number}

def compare_node(db: Session, node_id: str):
    node = db.get(Node, node_id)
    if not node:
        return None
    version = db.get(DocumentVersion, node.version_id)
    later = db.query(DocumentVersion).filter(
        DocumentVersion.document_id == version.document_id,
        DocumentVersion.version_number > version.version_number
    ).order_by(DocumentVersion.version_number.asc()).first()
    if not later:
        return {"node_id": node.id, "logical_id": node.logical_id, "status": "latest", "changed": False}
    peer = db.query(Node).filter_by(version_id=later.id, logical_id=node.logical_id).first()
    if not peer:
        return {"node_id": node.id, "logical_id": node.logical_id, "status": "removed", "changed": True,
                "from_version": version.version_number, "to_version": later.version_number}
    if peer.content_hash == node.content_hash:
        status = "unchanged"
        summary = None
    else:
        status = "modified"
        a, b = node.full_text, peer.full_text
        ratio = round(SequenceMatcher(None, a, b).ratio(), 3)
        summary = {"similarity": ratio, "old_length": len(a), "new_length": len(b),
                   "old_excerpt": a[:180], "new_excerpt": b[:180]}
    return {"node_id": node.id, "logical_id": node.logical_id, "status": status, "changed": status != "unchanged",
            "from_version": version.version_number, "to_version": later.version_number, "diff": summary}
