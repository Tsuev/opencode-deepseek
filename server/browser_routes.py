"""No CORS grant: only the paired extension can fetch loopback jobs."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from providers.tab_bridge import authorized, broker

router = APIRouter()


class BrowserLease(BaseModel):
    provider: str = Field(max_length=16)
    id: str = Field(max_length=128)
    owner: str = Field(max_length=128)
    lease: str = Field(max_length=128)
    document: str = Field(min_length=16, max_length=128)


class BrowserResult(BrowserLease):
    result: dict


def allowed(request):
    return request.client is not None and authorized(request.client.host, request.headers.get("authorization"))


@router.get("/browser/jobs/{provider}")
def get_job(provider: str, owner: str, document: str, request: Request):
    if not allowed(request):
        return JSONResponse(status_code=403, content={"error":"Browser pairing required"})
    try:
        return {"job":broker.claim(provider, owner, document)}
    except ValueError:
        return JSONResponse(status_code=400, content={"error":"Invalid browser identity"})


@router.post("/browser/result")
def finish_job(value: BrowserResult, request: Request):
    if not allowed(request):
        return JSONResponse(status_code=403, content={"error":"Browser pairing required"})
    try:
        broker.finish(value.provider, value.id, value.owner, value.lease, value.document, value.result)
    except ValueError:
        return JSONResponse(status_code=409, content={"error":"Stale browser job"})
    return {"ok":True}


@router.post("/browser/check")
def check_job(value: BrowserLease, request: Request):
    if not allowed(request):
        return JSONResponse(status_code=403, content={"error":"Browser pairing required"})
    return {"active":broker.active(value.provider, value.id, value.owner, value.lease, value.document)}


@router.post("/browser/submit")
def begin_job(value: BrowserLease, request: Request):
    if not allowed(request):
        return JSONResponse(status_code=403, content={"error":"Browser pairing required"})
    try:
        broker.begin(value.provider, value.id, value.owner, value.lease, value.document)
    except ValueError:
        return JSONResponse(status_code=409, content={"error":"Stale or already submitted browser job"})
    return {"ok":True}


@router.post("/browser/navigate")
def navigate_job(value: BrowserLease, request: Request):
    if not allowed(request):
        return JSONResponse(status_code=403, content={"error":"Browser pairing required"})
    try:
        broker.navigate(value.provider, value.id, value.owner, value.lease, value.document)
    except ValueError:
        return JSONResponse(status_code=409, content={"error":"Stale or submitted browser job"})
    return {"ok":True}
