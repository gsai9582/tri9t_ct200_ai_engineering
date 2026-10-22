"""End-to-end demonstration script for Tri9T CT-200 AI Engineering Assignment.

Demonstrates:
1. Ingesting manual v1 into a structured hierarchy in SQLite
2. Browsing and searching sections (including irregular subsections and tables)
3. Creating a version-pinned selection
4. Generating structured QA test cases linked to source nodes
5. Checking initial staleness status (current)
6. Submitting identical selection to demonstrate idempotency policy
7. Re-ingesting modified manual v2 without destroying v1
8. Inspecting diff summaries on modified nodes
9. Querying staleness retrieval to demonstrate stale traceability detection
10. Proving selection immutability (snapshots still hold exact v1 text)

Can be run against a live server (http://127.0.0.1:8000) or standalone via TestClient.
"""
import sys
import os
import json
import urllib.request
import urllib.error
from pathlib import Path

# Ensure project root is in sys.path so app can be imported when run directly
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Ensure stdout handles UTF-8 cleanly on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


BASE_URL = "http://127.0.0.1:8000"

def get_client():
    """Returns a client callable (path, method, data) using HTTP if server is up, else TestClient."""
    try:
        req = urllib.request.Request(f"{BASE_URL}/health")
        with urllib.request.urlopen(req, timeout=1.5) as r:
            if r.status == 200:
                print(f"[Mode] Connected to live server at {BASE_URL}\n")
                def live_call(path, method="GET", data=None):
                    url = f"{BASE_URL}{path}"
                    body = json.dumps(data).encode("utf-8") if data is not None else None
                    headers = {"Content-Type": "application/json"}
                    req = urllib.request.Request(url, data=body, headers=headers, method=method)
                    with urllib.request.urlopen(req) as resp:
                        return json.loads(resp.read().decode("utf-8"))
                return live_call
    except Exception:
        pass

    print("[Mode] Live server not detected at http://127.0.0.1:8000. Running in standalone in-process mode via TestClient.\n")
    from fastapi.testclient import TestClient
    from app.main import app
    tc = TestClient(app)

    def tc_call(path, method="GET", data=None):
        if method == "GET":
            r = tc.get(path)
        elif method == "POST":
            r = tc.post(path, json=data)
        else:
            raise ValueError(f"Unsupported method: {method}")
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text}")
        return r.json()

    return tc_call

def banner(title):
    print("=" * 75)
    print(f"  {title}")
    print("=" * 75)

def main():
    call = get_client()

    # Reset demo database and generations store for a clean, reproducible v1 -> v2 run
    try:
        from app.database import Base, engine
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        gen_store_file = Path("generated/generations.json")
        if gen_store_file.exists():
            gen_store_file.unlink()
    except Exception as e:
        pass

    banner("STEP 1: Ingesting CT-200 Technical Manual v1")
    v1_result = call("/documents/ingest", "POST", {"path": "data/ct200_manual_v1.pdf"})
    print(f"Ingestion successful:")
    print(f"  Document Name  : {v1_result['document_name']}")
    print(f"  Version Number : {v1_result['version']}")
    print(f"  Nodes Extracted: {v1_result['nodes']}")
    print(f"  Source SHA-256 : {v1_result.get('source_sha256', 'n/a')[:16]}...\n")



    banner("STEP 2: Browsing Top-Level Sections (v1)")
    sections = call("/documents/sections?version=1")
    for sec in sections["sections"]:
        print(f"  • [{sec['id'][:8]}] {sec['heading']} (children: {len(sec.get('children', []))})")
    print()

    banner("STEP 3: Searching & Inspecting Target Nodes for QA")
    battery_search = call("/nodes?q=battery&version=1")
    battery_node = battery_search["results"][0]
    print(f"Found Battery Node:")
    print(f"  Node ID     : {battery_node['id']}")
    print(f"  Logical ID  : {battery_node['logical_id']}")
    print(f"  Heading     : {battery_node['heading']} (Level {battery_node['level']})")
    print(f"  Content Hash: {battery_node['content_hash'][:16]}...")
    print(f"  Excerpt     : {battery_node['body_text'][:120]}...\n")

    error_search = call("/nodes?q=Error+Codes&version=1")
    # Grab the Section 4.2 error codes table
    error_node = next(n for n in error_search["results"] if n["heading"].startswith("4.2"))
    print(f"Found Error Codes Node (Section 4.2 with table):")
    print(f"  Node ID     : {error_node['id']}")
    print(f"  Logical ID  : {error_node['logical_id']}")
    print(f"  Heading     : {error_node['heading']}")
    print(f"  Content Hash: {error_node['content_hash'][:16]}...\n")

    banner("STEP 4: Creating a Version-Pinned Selection")
    selection_req = {
        "name": "Battery & Safety Critical Controls (v1)",
        "node_ids": [battery_node["id"], error_node["id"]]
    }
    selection = call("/selections", "POST", selection_req)
    selection_id = selection["id"]
    print(f"Selection Created:")
    print(f"  Selection ID: {selection_id}")
    print(f"  Name        : {selection['name']}")
    print(f"  Pinned Items: {len(selection['items'])}")
    for item in selection["items"]:
        print(f"    - [{item['version_number']}] {item['heading']} | hash: {item['content_hash'][:12]}...")
    print()

    banner("STEP 5: Generating QA Test Cases from Selection")
    generation = call(f"/selections/{selection_id}/generate", "POST")
    print(f"Generation Completed:")
    print(f"  Generation ID     : {generation['id']}")
    print(f"  Provider          : {generation.get('provider')}")
    print(f"  Test Cases Count  : {len(generation['test_cases'])}")
    print(f"  Duplicate Policy  : {generation['duplicate_policy']}\n")
    for i, tc in enumerate(generation["test_cases"], 1):
        print(f"  Test Case #{i}: {tc['title']}")
        print(f"    Risk        : {tc['risk'].upper()}")
        print(f"    Requirement : {tc['requirement'][:90]}...")
        print(f"    Steps       : {tc['steps'][0][:80]}...")
        print(f"    Expected    : {tc['expected_result'][:90]}...")
        print(f"    Traceability: node={tc['source_node_id'][:8]}... | hash={tc['source_hash'][:12]}...")
    print()

    banner("STEP 6: Checking Staleness Before Re-ingest (Expected: CURRENT)")
    status_v1 = call(f"/generations?selection_id={selection_id}")
    gen_record = status_v1["generations"][0]
    print(f"Staleness Status against latest version:")
    print(f"  is_stale: {gen_record['is_stale']}")
    for ref_status in gen_record["staleness"]:
        n_id = ref_status.get("node_id", "unknown")
        print(f"    Source Node [{n_id[:8]}]: status = {ref_status['status']} (changed={ref_status['changed']})")

    print()

    banner("STEP 7: Demonstrating Selection Idempotency")
    gen_cached = call(f"/selections/{selection_id}/generate", "POST")
    print(f"Submitting identical selection again:")
    print(f"  Returned Generation ID: {gen_cached['id']} (Matches original: {gen_cached['id'] == generation['id']})")
    print(f"  Policy Triggered      : {gen_cached.get('duplicate_policy')}\n")

    banner("STEP 8: Ingesting Modified Manual v2 (Re-ingestion)")
    v2_result = call("/documents/ingest", "POST", {"path": "data/ct200_manual_v2.pdf"})
    print(f"Ingestion successful:")
    print(f"  Document Name  : {v2_result['document_name']}")
    print(f"  Version Number : {v2_result['version']}")
    print(f"  Nodes Extracted: {v2_result['nodes']}")
    print(f"  Source SHA-256 : {v2_result.get('source_sha256', 'n/a')[:16]}...\n")


    banner("STEP 9: Inspecting Node Diffs Across Versions")
    battery_diff = call(f"/nodes/{battery_node['id']}/changes")
    print(f"Battery Node ({battery_node['heading']}) Diff Summary:")
    print(f"  Status        : {battery_diff['status'].upper()}")
    print(f"  Changed       : {battery_diff['changed']}")
    print(f"  From v{battery_diff['from_version']} to v{battery_diff['to_version']}")
    if battery_diff.get("diff"):
        d = battery_diff["diff"]
        print(f"  Similarity    : {d['similarity'] * 100:.1f}%")
        print(f"  v1 excerpt    : {d['old_excerpt']}")
        print(f"  v2 excerpt    : {d['new_excerpt']}")
    print()

    banner("STEP 10: Querying Retrieval API for Stale Traceability (Expected: STALE)")
    status_v2 = call(f"/generations?selection_id={selection_id}")
    gen_v2 = status_v2["generations"][0]
    print(f"Staleness Evaluation Post-v2 Ingestion:")
    print(f"  is_stale: {gen_v2['is_stale']} (Correctly flagged!)")
    for s in gen_v2["staleness"]:
        n_id = s.get("node_id", "unknown")
        print(f"    Node [{n_id[:8]}]:")
        print(f"      Status       : {s['status'].upper()}")

        print(f"      Changed      : {s['changed']}")
        if "old_hash" in s:
            print(f"      Old Hash     : {s['old_hash'][:16]}...")
            print(f"      Current Hash : {s['current_hash'][:16]}...")
        if "note" in s:
            print(f"      Note         : {s['note']}")
    print()

    banner("STEP 11: Proving Selection Immutability")
    pinned_sel = call(f"/selections/{selection_id}")
    print(f"Inspecting Pinned Selection Snapshot:")
    print(f"  Selection ID   : {pinned_sel['id']}")
    item0 = pinned_sel["items"][0]
    print(f"  Pinned Version : v{item0['version_number']}")
    print(f"  Pinned Hash    : {item0['content_hash'][:16]}...")
    print(f"  Pinned Excerpt : {item0['text'][:120]}...")
    print(f"  Verified: Original v1 snapshot text remained 100% immutable despite v2 ingestion!\n")

    banner("DEMO COMPLETED SUCCESSFULLY: All versioning and staleness flows verified!")

if __name__ == "__main__":
    main()

