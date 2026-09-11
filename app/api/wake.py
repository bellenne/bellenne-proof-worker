from __future__ import annotations

import hashlib
import hmac
import json
import time

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import SecretStr


def create_wake_app(token: SecretStr, wake_event) -> FastAPI:
    app = FastAPI(title="BellenneProof Worker Wake API", docs_url=None, redoc_url=None, openapi_url=None)
    signing_key = hashlib.sha256(token.get_secret_value().encode("utf-8")).digest()

    @app.post("/wake")
    async def wake(
        request: Request,
        x_proof_timestamp: str = Header(...),
        x_proof_signature: str = Header(...),
    ) -> JSONResponse:
        try:
            timestamp = int(x_proof_timestamp)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail="Invalid wake signature.") from exc
        if abs(int(time.time()) - timestamp) > 60:
            raise HTTPException(status_code=401, detail="Expired wake signature.")
        body = await request.body()
        expected = hmac.new(
            signing_key,
            x_proof_timestamp.encode("ascii") + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, x_proof_signature):
            raise HTTPException(status_code=401, detail="Invalid wake signature.")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail="Invalid wake payload.") from exc
        if not isinstance(payload, dict) or payload.get("event") != "queue.changed":
            raise HTTPException(status_code=422, detail="Unsupported wake event.")
        wake_event.set()
        return JSONResponse({"accepted": True}, status_code=status.HTTP_202_ACCEPTED)

    return app
