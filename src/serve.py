# src/serve.py
import os
import json
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from fastapi.openapi.utils import get_openapi

API_KEY = os.getenv("API_KEY", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()

app = FastAPI(
    title="Crypto Snapshot API",
    version="1.0.0",
    description="Crypto Snapshot API",
)

# ---------- Auth ----------
def require_api_key(req: Request):
    key = req.headers.get("X-API-Key")
    if not API_KEY or key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

# ---------- Routes ----------
@app.get("/health", response_model=dict, summary="Health")
def health():
    return {"ok": True}

@app.get("/snapshot-min", summary="Snapshot minifié", dependencies=[Depends(require_api_key)])
def getSnapshotMin():
    p = Path("snapshot_min.json")
    if not p.exists():
        return JSONResponse(status_code=503, content={"error": "snapshot_min.json not ready"})
    # Parse le JSON avant de renvoyer
    return JSONResponse(content=json.loads(p.read_text(encoding="utf-8")))

#
@app.get("/snapshot", summary="Snapshot complet", dependencies=[Depends(require_api_key)])
def getSnapshotFull():
    p = Path("snapshot.json")
    if not p.exists():
        return JSONResponse(status_code=503, content={"error": "snapshot.json not ready"})
    return JSONResponse(content=json.loads(p.read_text(encoding="utf-8")))

# Root and version endpoints
@app.get("/")
def root():
    return {"service": "crypto-scanner-api", "status": "ok"}

@app.get("/__version__")
def version():
    return {"app": app.title, "version": app.version}

# ---------- OpenAPI (schémas minimaux + servers + sécurité) ----------
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )

    # servers[]
    if PUBLIC_BASE_URL:
        schema["servers"] = [{"url": PUBLIC_BASE_URL}]

    # securitySchemes (clé API en header)
    comps = schema.setdefault("components", {})
    sec = comps.setdefault("securitySchemes", {})
    sec["ApiKeyAuth"] = {"type": "apiKey", "in": "header", "name": "X-API-Key"}

    # Schémas minimaux attendus par le Builder
    sch = comps.setdefault("schemas", {})
    sch["HealthResponse"] = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    # objets “ouverts” (clés dynamiques) pour éviter de décrire tout le JSON
    sch["SnapshotMin"] = {
        "type": "object",
        "description": "snapshot_min.json (structure dynamique)",
        "additionalProperties": True,
    }
    sch["Snapshot"] = {
        "type": "object",
        "description": "snapshot.json complet (structure dynamique)",
        "additionalProperties": True,
    }

    # Patcher les réponses pour pointer vers ces schémas
    try:
        schema["paths"]["/health"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] = {
            "$ref": "#/components/schemas/HealthResponse"
        }
    except Exception:
        pass

    try:
        schema["paths"]["/snapshot-min"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] = {
            "$ref": "#/components/schemas/SnapshotMin"
        }
        # marquer la nécessité de la clé API
        schema["paths"]["/snapshot-min"]["get"]["security"] = [{"ApiKeyAuth": []}]
    except Exception:
        pass

    try:
        schema["paths"]["/snapshot"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] = {
            "$ref": "#/components/schemas/Snapshot"
        }
        schema["paths"]["/snapshot"]["get"]["security"] = [{"ApiKeyAuth": []}]
    except Exception:
        pass

    app.openapi_schema = schema
    return app.openapi_schema

app.openapi = custom_openapi