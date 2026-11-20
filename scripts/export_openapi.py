"""Generate the combined and per-version OpenAPI contracts."""

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def export(version):
    worker = "import json; from app.main import app; print(json.dumps(app.openapi()))"
    output = subprocess.check_output(
        [sys.executable, "-c", worker], cwd=ROOT, text=True,
        env={**os.environ, "LAMINUS_API_VERSION": version},
    )
    return json.loads(output)


def write(name, spec):
    path = ROOT / name
    path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    print(f"Written {path}")


v1 = export("v1")
v2 = export("v2")
write("openapi-v1.json", v1)
write("openapi-v2.json", v2)

components = {kind: entries.copy() for kind, entries in v2["components"].items()}
for kind, entries in v1["components"].items():
    shared = components.setdefault(kind, {})
    for name, definition in entries.items():
        if name in shared and shared[name] != definition:
            raise ValueError(f"Conflicting OpenAPI component: {kind}/{name}")
        shared[name] = definition
write("openapi.json", {
    **v2, "paths": {**v2["paths"], **v1["paths"]},
    "components": components, "tags": [*v2["tags"], *v1["tags"]],
})
