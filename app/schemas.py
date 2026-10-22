from pydantic import BaseModel, Field
from typing import Literal

class IngestRequest(BaseModel):
    path: str = Field(description="Server-local PDF path; use the bundled data paths for the demo.")
    document_name: str = "CT-200 Manual"

class SelectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    node_ids: list[str] = Field(min_length=1)

class GeneratedTestCase(BaseModel):
    title: str = Field(min_length=3)
    requirement: str = Field(min_length=5)
    preconditions: list[str]
    steps: list[str] = Field(min_length=1)
    expected_result: str = Field(min_length=5)
    risk: Literal["low", "medium", "high"]
    source_node_id: str
    source_hash: str

class GenerationResponse(BaseModel):
    id: str
    selection_id: str
    created_at: str
    test_cases: list[GeneratedTestCase]
    source_refs: list[dict]
    duplicate_policy: str
