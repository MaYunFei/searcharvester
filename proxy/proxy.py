"""
Searcharvester → Tavily fallback proxy.

Tries the self-hosted Searcharvester first; on any failure (timeout, 5xx,
empty results) silently falls back to Tavily API.

All endpoints validate the same API key that Searcharvester uses.
"""

import asyncio
import logging
import os
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("proxy")

# ── config ──────────────────────────────────────────────────────────
SEARCHARVESTER_URL = os.environ.get("SEARCHARVESTER_URL", "http://127.0.0.1:8000")
TAVILY_BASE_URL = os.environ.get("TAVILY_BASE_URL", "https://api.tavily.com")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
API_KEY = os.environ.get("API_KEY", "")
SEARCHARVESTER_TIMEOUT = float(os.environ.get("SEARCHARVESTER_TIMEOUT", "15"))

app = FastAPI(title="searcharvester-proxy", version="1.0.0")


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
                # SSE — don't try to buffer, just return None to fallback
                return None
            resp = await client.post(url, json=body, headers=headers)
            if resp.status_code == 200:
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
    # extract: no content at all
    if data.get("title") is None and data.get("content") is None and data.get("raw_content") is None:
        if "results" not in data:
            return True
    return False


# ── /health ──────────────────────────────────────────────────────────
@app.get("/health")
@app.post("/health")
async def health():
    return {"status": "ok", "proxy": "searcharvester-tavily"}


# ── /search ──────────────────────────────────────────────────────────
@app.post("/search")
async def search(request: Request):
    body = await request.json()

    # 1) try Searcharvester
    resp = await try_searcharvester("/search", body)
    if resp is not None:
        data = resp.json()
        if not is_empty_result(data):
            logger.info("/search → Searcharvester ✓")
            # Merge response headers (content-type etc.)
            return JSONResponse(content=data, status_code=resp.status_code)

    # 2) fallback to Tavily
    logger.info("/search → falling back to Tavily")
    try:
        resp = await call_tavily("/search", body)
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception as e:
        logger.error("Tavily /search failed: %s", e)
        raise HTTPException(status_code=502, detail="Both Searcharvester and Tavily failed")


# ── /extract ─────────────────────────────────────────────────────────
@app.post("/extract")
async def extract(request: Request):
    body = await request.json()
    urls: list[str] = body.get("urls", [])

    # Searcharvester extracts one URL at a time, Tavily extracts many.
    # Strategy: try Searcharvester first for all URLs individually,
    # then fall back to Tavily for any that failed.

    results: list[dict] = []
    failed_urls: list[str] = []

    for url in urls:
        resp = await try_searcharvester("/extract", {"url": url})
        if resp is not None:
            data = resp.json()
            if not is_empty_result(data):
                # Normalize to Tavily format
                results.append({
                    "url": url,
                    "title": data.get("title"),
                    "raw_content": data.get("content") or data.get("raw_content", ""),
                })
                continue
        failed_urls.append(url)

    # If Searcharvester got nothing and there are failed URLs, try Tavily
    if failed_urls:
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

        return JSONResponse(content={
            "results": results,
            "failed_results": failed_results if failed_urls else [],
        })

    tag = "Searcharvester" if not failed_urls else "Searcharvester+Tavily"
    logger.info("/extract → %s ✓", tag)
    return JSONResponse(content={"results": results, "failed_results": []})


# ── /research ────────────────────────────────────────────────────────
@app.post("/research")
async def research(request: Request):
    body = await request.json()

    resp = await try_searcharvester("/research", body)
    if resp is not None:
        return JSONResponse(content=resp.json(), status_code=resp.status_code)

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
