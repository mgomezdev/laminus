"""Breaking-change guard for /api/v1.

tests/fixtures/api_v1_surface.json is the committed v1 baseline. Anything it lists must
still exist: same path+method, no new *required* parameters, no lost response status codes.
Additions are fine (regenerate with scripts/snapshot_v1_surface.py). A failure here means a
breaking change, which requires a new major version (docs/api-versioning.md).
"""
import json
from pathlib import Path

from app.main import app

BASELINE = Path(__file__).parent / "fixtures" / "api_v1_surface.json"


def _required_body_fields(spec: dict, op: dict) -> list[str]:
    content = (op.get("requestBody") or {}).get("content", {})
    for media in content.values():
        schema = media.get("schema", {})
        if "$ref" in schema:
            schema = spec["components"]["schemas"].get(schema["$ref"].rsplit("/", 1)[-1], {})
        return sorted(schema.get("required", []))
    return []


def surface() -> dict:
    spec = app.openapi()
    out = {}
    for path, ops in spec["paths"].items():
        if path != "/api/v1" and not path.startswith("/api/v1/"):
            continue
        for method, op in ops.items():
            out[f"{method.upper()} {path}"] = {
                "required_params": sorted(f"{p['in']}:{p['name']}" for p in op.get("parameters", [])
                                          if p.get("required")),
                "required_body": _required_body_fields(spec, op),
                "responses": sorted(op.get("responses", {})),
            }
    return out


def test_v1_surface_is_backward_compatible():
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    current = surface()
    problems = []
    for key, old in baseline.items():
        new = current.get(key)
        if new is None:
            problems.append(f"removed: {key}")
            continue
        added_required = set(new["required_params"]) - set(old["required_params"])
        added_body = set(new["required_body"]) - set(old["required_body"])
        lost = set(old["responses"]) - set(new["responses"])
        if added_required:
            problems.append(f"{key}: new required params {sorted(added_required)}")
        if added_body:
            problems.append(f"{key}: new required body fields {sorted(added_body)}")
        if lost:
            problems.append(f"{key}: lost responses {sorted(lost)}")
    assert not problems, "breaking v1 change(s) - needs a new major version:\n" + "\n".join(problems)


def test_no_versionless_routes():
    spec = app.openapi()
    bad = [p for p in spec["paths"] if p.startswith("/api") and p != "/api/v1" and not p.startswith("/api/v1/")]
    assert not bad, f"routes outside /api/v1: {bad}"
