from fastapi import FastAPI, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_
from app.database import Base, engine, get_db
from app.models import Document, DocumentVersion, Node, Selection
from app.schemas import IngestRequest, SelectionCreate
from app.services.versioning import ingest_pdf, compare_node
from app.services.generation_store import create_selection, selection_payload, all_generations, staleness_for_source
from app.services.llm_generator import generate_for_selection

Base.metadata.create_all(bind=engine)
app = FastAPI(title="Tri9T CT-200 AI Engineering API", version="0.1.0",
              description="Versioned CT-200 manual hierarchy, pinned selections, QA generation and staleness checks.")

@app.get("/health")
def health():
    return {"status":"ok"}

@app.post("/documents/ingest")
def ingest(req: IngestRequest, db: Session = Depends(get_db)):
    try:
        doc, version, details = ingest_pdf(db, req.path, req.document_name)
        return {"document_id":doc.id,"document_name":doc.name,"version_id":version.id,
                "version":version.version_number,"source_sha256":version.source_sha256,**details}

    except FileNotFoundError:
        raise HTTPException(404, "PDF path not found on server")
    except Exception as exc:
        db.rollback()
        raise HTTPException(500, f"Ingestion failed: {type(exc).__name__}: {exc}")

def _version(db, document_name, version_number):
    doc = db.query(Document).filter_by(name=document_name).first()
    if not doc: raise HTTPException(404, "Document not ingested")
    q = db.query(DocumentVersion).filter_by(document_id=doc.id)
    if version_number is None:
        ver = q.order_by(DocumentVersion.version_number.desc()).first()
    else:
        ver = q.filter_by(version_number=version_number).first()
    if not ver: raise HTTPException(404, "Version not found")
    return doc, ver

@app.get("/documents/sections")
def top_sections(document_name: str = "CT-200 Manual", version: int | None = None, db: Session = Depends(get_db)):
    doc, ver = _version(db, document_name, version)
    nodes = db.query(Node).filter_by(version_id=ver.id, parent_id=None).order_by(Node.page_start, Node.heading).all()
    # Include only top-level numbered sections; front matter is retained but separately typed.
    return {"document":doc.name,"version":ver.version_number,"sections":[_node_dict(n, db, False) for n in nodes]}

def _node_dict(n, db, include_children=True):
    v = db.get(DocumentVersion, n.version_id)
    result = {"id":n.id,"logical_id":n.logical_id,"version_id":n.version_id,"version":v.version_number,
              "parent_id":n.parent_id,"heading":n.heading,"level":n.level,"type":n.node_type,
              "body_text":n.body_text,"full_text":n.full_text,"content_hash":n.content_hash,
              "page_start":n.page_start,"page_end":n.page_end,"match_status":n.match_status}
    if include_children:
        children = db.query(Node).filter_by(version_id=n.version_id,parent_id=n.id).order_by(Node.page_start, Node.level).all()
        result["children"] = [_node_dict(c, db, True) for c in children]
    return result

@app.get("/nodes/{node_id}")
def get_node(node_id: str, db: Session = Depends(get_db)):
    n = db.get(Node,node_id)
    if not n: raise HTTPException(404,"Node not found")
    return _node_dict(n,db,True)

@app.get("/nodes")
def search_nodes(q: str = Query(min_length=1), version: int | None = None, document_name: str = "CT-200 Manual", db: Session = Depends(get_db)):
    doc, ver = _version(db, document_name, version)
    pattern = f"%{q}%"
    nodes = db.query(Node).filter(Node.version_id==ver.id, or_(Node.heading.ilike(pattern),Node.body_text.ilike(pattern))).limit(100).all()
    return {"document":doc.name,"version":ver.version_number,"query":q,"results":[_node_dict(n,db,False) for n in nodes]}

@app.get("/nodes/{node_id}/changes")
def node_changes(node_id: str, db: Session = Depends(get_db)):
    result = compare_node(db,node_id)
    if result is None: raise HTTPException(404,"Node not found")
    return result

@app.post("/selections")
def selections(req: SelectionCreate, db: Session = Depends(get_db)):
    try:
        s = create_selection(db, req.name, req.node_ids)
        return {"id":s.id,"name":s.name,"items":selection_payload(db,s.id)["items"]}
    except ValueError as exc:
        raise HTTPException(400,str(exc))

@app.get("/selections/{selection_id}")
def get_selection(selection_id: str, db: Session = Depends(get_db)):
    result = selection_payload(db,selection_id)
    if not result: raise HTTPException(404,"Selection not found")
    return result

@app.post("/selections/{selection_id}/generate")
async def generate(selection_id: str, force: bool = False, db: Session = Depends(get_db)):
    try:
        return await generate_for_selection(db,selection_id,force)
    except ValueError as exc:
        raise HTTPException(404,str(exc))
    except Exception as exc:
        raise HTTPException(502,f"Generation/validation failed: {type(exc).__name__}: {exc}")

@app.get("/generations")
def retrieve_generations(selection_id: str | None = None, node_id: str | None = None, db: Session = Depends(get_db)):
    records = all_generations()
    out = []
    for record in records:
        if selection_id and record["selection_id"] != selection_id: continue
        if node_id and not any(ref["node_id"] == node_id or ref["logical_id"] == node_id for ref in record["source_refs"]): continue
        status = [staleness_for_source(db,ref) for ref in record["source_refs"]]
        item = dict(record)
        item["staleness"] = status
        item["is_stale"] = any(s.get("changed") for s in status)
        out.append(item)
    return {"count":len(out),"generations":out}
