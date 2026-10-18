"""Regenerate packages/contracts from the code (single source of truth: pydantic models / FastAPI)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "api"))
from localcrew.api import app  # noqa: E402
from localcrew.tools import TOOLS  # noqa: E402

out = ROOT / "packages" / "contracts"
out.mkdir(parents=True, exist_ok=True)
(out / "openapi.json").write_text(json.dumps(app.openapi(), indent=1))
(out / "tools.json").write_text(json.dumps({n: {"effect": t.effect, "description": t.description,
                                                "parameters": t.args.model_json_schema()} for n, t in TOOLS.items()}, indent=1))
print("wrote", out)
