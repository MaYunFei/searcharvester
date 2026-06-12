"""
Searcharvester → Tavily fallback proxy.

Tries the self-hosted Searcharvester first; on any failure (timeout, 5xx,
empty results) silently falls back to Tavily API.

All endpoints validate the same API key that Searcharvester uses.

Logs every request as JSON-lines to /var/log/proxy/access.log.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

# ── logging ───────────────────────────────────────────────────────────
LOG_DIR = os.environ.get("LOG_DIR", "/var/log/proxy")
LOG_FILE = os.path.join(LOG_DIR, "access.log")

os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("proxy")
logger.setLevel(logging.INFO)

# stdout + file
_file_handler = logging.FileHandler(LOG_FILE)
_file_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_file_handler)
logger.addHandler(logging.StreamHandler())

# Dedicated structured access log (JSON lines, separate from text logs)
def _log_access(record: dict) -> None:
    """Append one JSON record to the access log."""
    try:
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


# ── config ──────────────────────────────────────────────────────────
SEARCHARVESTER_URL = os.environ.get("SEARCHARVESTER_URL", "http://127.0.0.1:8000")
TAVILY_BASE_URL = os.environ.get("TAVILY_BASE_URL", "https://api.tavily.com")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
API_KEY = os.environ.get("API_KEY", "")
SEARCHARVESTER_TIMEOUT = float(os.environ.get("SEARCHARVESTER_TIMEOUT", "15"))

app = FastAPI(title="searcharvester-proxy", version="2.0.0")


# ── auth middleware ──────────────────────────────────────────────────
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path == "/health" or not API_KEY:
        return await call_next(request)
    auth = request.headers.get("X-API-Key", "") or \
           request.headers.get("Authorization", "").removeprefix("Bearer ")
    if auth != API_KEY:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)


# ── helpers ──────────────────────────────────────────────────────────
async def try_searcharvester(endpoint: str, body: dict) -> httpx.Response | None:
    """Call Searcharvester, return response or None on failure."""
    url = f"{SEARCHARVESTER_URL.rstrip('/')}{endpoint}"
    headers = {}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    try:
        async with httpx.AsyncClient(timeout=SEARCHARVESTER_TIMEOUT) as client:
            if endpoint.startswith("/research") and endpoint.endswith("/events"):
                return None
            resp = await client.post(url, json=body, headers=headers)
            if resp.is_success:
                return resp
            logger.warning("Searcharvester %s returned %d", endpoint, resp.status_code)
            return None
    except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
        logger.warning("Searcharvester %s unreachable: %s", endpoint, e)
        return None


async def call_tavily(endpoint: str, body: dict) -> httpx.Response:
    """Call Tavily API directly."""
    url = f"{TAVILY_BASE_URL.rstrip('/')}{endpoint}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TAVILY_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, json=body, headers=headers)
        return resp


def is_empty_result(data: dict) -> bool:
    """Check if Searcharvester returned no results."""
    results = data.get("results")
    if results is not None and len(results) == 0:
        return True
    if data.get("title") is None and data.get("content") is None and data.get("raw_content") is None:
        if "results" not in data:
            return True
    return False


def _query_summary(endpoint: str, body: dict) -> str:
    """Extract a human-readable summary from the request body."""
    if endpoint == "/search":
        q = body.get("query", "")
        return q[:120] if q else "(empty query)"
    if endpoint == "/extract":
        urls = body.get("urls", body.get("url", []))
        if isinstance(urls, str):
            urls = [urls]
        return f"{len(urls)} urls" if urls else "(no urls)"
    if endpoint == "/research":
        q = body.get("query", "")
        return q[:120] if q else "(empty query)"
    return ""


# ── /health ──────────────────────────────────────────────────────────
@app.get("/health")
@app.post("/health")
async def health():
    return {"status": "ok", "proxy": "searcharvester-tavily"}


# ── /search ──────────────────────────────────────────────────────────
@app.post("/search")
async def search(request: Request):
    body = await request.json()
    t0 = time.perf_counter()
    source = "searcharvester"
    error = None

    # 1) try Searcharvester
    resp = await try_searcharvester("/search", body)
    if resp is not None:
        data = resp.json()
        if not is_empty_result(data):
            elapsed = round((time.perf_counter() - t0) * 1000)
            _log_access({
                "ts": datetime.now(timezone.utc).isoformat(),
                "endpoint": "/search",
                "source": source,
                "query": _query_summary("/search", body),
                "latency_ms": elapsed,
                "results": len(data.get("results", [])),
            })
            return JSONResponse(content=data, status_code=resp.status_code)

    # 2) fallback to Tavily
    source = "tavily"
    logger.info("/search → falling back to Tavily")
    try:
        resp = await call_tavily("/search", body)
        elapsed = round((time.perf_counter() - t0) * 1000)
        tavily_data = resp.json()
        _log_access({
            "ts": datetime.now(timezone.utc).isoformat(),
            "endpoint": "/search",
            "source": source,
            "query": _query_summary("/search", body),
            "latency_ms": elapsed,
            "results": len(tavily_data.get("results", [])),
            "fallback_reason": "searcharvester_empty_or_error",
        })
        return JSONResponse(content=tavily_data, status_code=resp.status_code)
    except Exception as e:
        elapsed = round((time.perf_counter() - t0) * 1000)
        _log_access({
            "ts": datetime.now(timezone.utc).isoformat(),
            "endpoint": "/search",
            "source": "error",
            "query": _query_summary("/search", body),
            "latency_ms": elapsed,
            "error": str(e),
        })
        raise HTTPException(status_code=502, detail="Both Searcharvester and Tavily failed")


# ── /extract ─────────────────────────────────────────────────────────
@app.post("/extract")
async def extract(request: Request):
    body = await request.json()
    t0 = time.perf_counter()
    urls: list[str] = body.get("urls", [])
    if not urls and body.get("url"):
        urls = [body["url"]]

    results: list[dict] = []
    failed_urls: list[str] = []
    searcharvester_ok = 0

    for url in urls:
        resp = await try_searcharvester("/extract", {"url": url})
        if resp is not None:
            data = resp.json()
            if not is_empty_result(data):
                results.append({
                    "url": url,
                    "title": data.get("title"),
                    "raw_content": data.get("content") or data.get("raw_content", ""),
                })
                searcharvester_ok += 1
                continue
        failed_urls.append(url)

    source = "searcharvester"
    fallback_reason = None

    if failed_urls:
        source = "tavily" if searcharvester_ok == 0 else "searcharvester+tavily"
        fallback_reason = f"{len(failed_urls)} urls fell back"
        logger.info("/extract → falling back to Tavily for %d urls", len(failed_urls))
        try:
            resp = await call_tavily("/extract", {"urls": failed_urls})
            tavily_data = resp.json()
            tavily_results = tavily_data.get("results", [])
            results.extend(tavily_results)
            failed_results = tavily_data.get("failed_results", [])
        except Exception as e:
            logger.error("Tavily /extract failed: %s", e)
            failed_results = failed_urls

        elapsed = round((time.perf_counter() - t0) * 1000)
        _log_access({
            "ts": datetime.now(timezone.utc).isoformat(),
            "endpoint": "/extract",
            "source": source,
            "query": _query_summary("/extract", body),
            "latency_ms": elapsed,
            "urls_total": len(urls),
            "searcharvester_ok": searcharvester_ok,
            "tavily_fallback": len(failed_urls),
            "fallback_reason": fallback_reason,
        })
        return JSONResponse(content={
            "results": results,
            "failed_results": failed_results if failed_urls else [],
        })

    elapsed = round((time.perf_counter() - t0) * 1000)
    _log_access({
        "ts": datetime.now(timezone.utc).isoformat(),
        "endpoint": "/extract",
        "source": "searcharvester",
        "query": _query_summary("/extract", body),
        "latency_ms": elapsed,
        "urls_total": len(urls),
        "searcharvester_ok": searcharvester_ok,
    })
    return JSONResponse(content={"results": results, "failed_results": []})


# ── /research ────────────────────────────────────────────────────────
@app.post("/research")
async def research(request: Request):
    body = await request.json()
    t0 = time.perf_counter()

    resp = await try_searcharvester("/research", body)
    if resp is not None:
        data = resp.json()
        elapsed = round((time.perf_counter() - t0) * 1000)
        _log_access({
            "ts": datetime.now(timezone.utc).isoformat(),
            "endpoint": "/research",
            "source": "searcharvester",
            "query": _query_summary("/research", body),
            "latency_ms": elapsed,
            "job_id": data.get("job_id", ""),
        })
        return JSONResponse(content=data, status_code=resp.status_code)

    elapsed = round((time.perf_counter() - t0) * 1000)
    _log_access({
        "ts": datetime.now(timezone.utc).isoformat(),
        "endpoint": "/research",
        "source": "error",
        "query": _query_summary("/research", body),
        "latency_ms": elapsed,
        "error": "searcharvester_unavailable",
    })
    raise HTTPException(
        status_code=502,
        detail="Searcharvester /research unavailable (no Tavily fallback for research)",
    )


@app.get("/research/{job_id}")
async def research_status(job_id: str):
    resp = await try_searcharvester(f"/research/{job_id}", {})
    if resp is not None:
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
    raise HTTPException(status_code=502, detail="Searcharvester unavailable")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8001")))
