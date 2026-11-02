"""
Public reverse proxy for a private Databricks App.

Flow:  public user --> this proxy --> (SP bearer token) --> Databricks App URL

Environment variables (all required unless noted):
  APP_URL            The Databricks App URL to forward to
  DATABRICKS_HOST    Workspace URL, e.g. https://dbc-xxxx.cloud.databricks.com
  SP_CLIENT_ID       Service principal application_id (OAuth client id)
  SP_CLIENT_SECRET   Service principal OAuth secret
  PROXY_API_KEY      Optional passcode. Browsers get a login page; API clients send X-API-Key.

Run locally:
  pip install -r requirements.txt
  python agent_app_public_proxy.py            # listens on $PORT (default 8080)

Security notes:
  - Never commit real env values; set them in your host's dashboard or a .env file.
  - All app traffic runs under the SP identity: costs, audit logs, and any
    user-specific app behavior are attributed to the SP, not to the end user.
"""

import os
import time
import threading
from collections import defaultdict, deque

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from urllib.parse import parse_qs

REQUIRED_ENV_VARS = ["APP_URL", "DATABRICKS_HOST", "SP_CLIENT_ID", "SP_CLIENT_SECRET"]
_missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
if _missing:
    raise RuntimeError(
        "Missing required environment variables: " + ", ".join(_missing)
        + ". Set them in your host's dashboard (e.g. Render > Environment) and redeploy."
    )

APP_URL = os.environ["APP_URL"].rstrip("/")
DATABRICKS_HOST = os.environ["DATABRICKS_HOST"].rstrip("/")
SP_CLIENT_ID = os.environ["SP_CLIENT_ID"]
SP_CLIENT_SECRET = os.environ["SP_CLIENT_SECRET"]
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "")  # empty = no key required

RATE_LIMIT_REQUESTS = 30   # per client per window
RATE_LIMIT_SECONDS = 60

app = FastAPI()

# ---------- Databricks M2M token (cached, auto-refreshed) ----------
_token_lock = threading.Lock()
_token_cache = {"token": None, "expires_at": 0.0}


def get_access_token() -> str:
    """Fetch a short-lived OAuth token using the SP's client credentials."""
    with _token_lock:
        if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
            return _token_cache["token"]
        resp = httpx.post(
            f"{DATABRICKS_HOST}/oidc/v1/token",
            data={"grant_type": "client_credentials", "scope": "all-apis"},
            auth=(SP_CLIENT_ID, SP_CLIENT_SECRET),
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
        _token_cache["token"] = payload["access_token"]
        _token_cache["expires_at"] = time.time() + max(payload.get("expires_in", 3600) - 60, 60)
        return _token_cache["token"]


# ---------- simple per-IP rate limiting (in-memory) ----------
_hits: dict = defaultdict(deque)


def rate_limited(client_ip: str) -> bool:
    now = time.time()
    q = _hits[client_ip]
    while q and now - q[0] > RATE_LIMIT_SECONDS:
        q.popleft()
    if len(q) >= RATE_LIMIT_REQUESTS:
        return True
    q.append(now)
    return False


# ---------- passcode login (browsers cannot send X-API-Key headers) ----------
LOGIN_PAGE = """<!doctype html>
<html><head><title>Agent App</title></head>
<body style="font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#f5f5f5;">
  <form method="post" action="/__proxy_login" style="background:#fff;padding:2rem;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.15);">
    <h2 style="margin-top:0;">Agent App</h2>
    <p>Enter the access passcode to continue.</p>
    <input type="password" name="passcode" placeholder="Passcode" required
           style="width:100%;padding:.5rem;margin:.5rem 0;box-sizing:border-box;">
    <p style="color:#c00;margin:0;"><!--ERROR--></p>
    <button type="submit" style="width:100%;padding:.5rem;margin-top:.5rem;">Enter</button>
  </form>
</body></html>"""


def _authorized(request: Request) -> bool:
    if not PROXY_API_KEY:
        return True  # open access (rate limiting still applies)
    if request.headers.get("x-api-key") == PROXY_API_KEY:
        return True
    if request.cookies.get("proxy_key") == PROXY_API_KEY:
        return True
    return False


@app.get("/__proxy_login")
async def login_form():
    return HTMLResponse(LOGIN_PAGE)


@app.post("/__proxy_login")
async def login_submit(request: Request):
    body = (await request.body()).decode("utf-8", "replace")
    passcode = parse_qs(body).get("passcode", [""])[0]
    if passcode == PROXY_API_KEY:
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("proxy_key", PROXY_API_KEY, httponly=True, samesite="lax", max_age=7 * 86400)
        return resp
    return HTMLResponse(LOGIN_PAGE.replace("<!--ERROR-->", "Wrong passcode — try again."), status_code=401)


@app.get("/__proxy_health")
async def health(request: Request):
    """Diagnostics: reports whether the Databricks token flow works and why not."""
    if not _authorized(request):
        return HTMLResponse(LOGIN_PAGE, status_code=401)
    try:
        token = get_access_token()
        return JSONResponse({"status": "ok", "detail": "Databricks token fetched successfully"})
    except httpx.HTTPStatusError as e:
        return JSONResponse(
            {"status": "error", "step": "token",
             "http_status": e.response.status_code, "detail": e.response.text[:500]},
            status_code=502,
        )
    except Exception as e:
        return JSONResponse(
            {"status": "error", "step": "token", "detail": f"{type(e).__name__}: {e}"},
            status_code=502,
        )


# ---------- transparent reverse proxy ----------
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailers", "transfer-encoding", "upgrade", "host", "authorization"}

http = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=15))  # long timeout: agent replies can stream slowly


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy(path: str, request: Request):
    client_ip = request.client.host if request.client else "unknown"

    # Auth gate: X-API-Key header (API clients) or passcode cookie (browsers)
    if not _authorized(request):
        if request.method in ("GET", "HEAD"):
            return HTMLResponse(LOGIN_PAGE, status_code=401)
        return JSONResponse(
            {"error": "unauthorized; send X-API-Key or log in via /__proxy_login"},
            status_code=401,
        )

    if rate_limited(client_ip):
        return JSONResponse({"error": "rate limit exceeded, slow down"}, status_code=429)

    # Build upstream request: same method/path/query, safe headers, SP bearer token
    upstream_url = f"{APP_URL}/{path}"
    if request.url.query:
        upstream_url += f"?{request.url.query}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    try:
        token = get_access_token()
    except httpx.HTTPStatusError as e:
        return JSONResponse(
            {"error": f"Databricks rejected the service principal credentials: HTTP {e.response.status_code} {e.response.text[:300]}"},
            status_code=502,
        )
    except httpx.HTTPError as e:
        return JSONResponse(
            {"error": f"could not reach the Databricks token endpoint: {type(e).__name__}: {e}"},
            status_code=502,
        )
    headers["authorization"] = f"Bearer {token}"

    body = await request.body()
    if not body:
        body = None

    try:
        upstream = await http.build_request(request.method, upstream_url, headers=headers, content=body)
        upstream_resp = await http.send(upstream, stream=True)
    except httpx.HTTPError as e:
        return JSONResponse(
            {"error": f"could not reach the Databricks app: {type(e).__name__}: {e}"},
            status_code=502,
        )

    resp_headers = [(k, v) for k, v in upstream_resp.headers.items() if k.lower() not in HOP_BY_HOP]
    return StreamingResponse(
        upstream_resp.aiter_raw(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        background=_close_resp(upstream_resp),
    )


def _close_resp(resp):
    async def close():
        await resp.aclose()
    return close


# Free-tier hosts (Render, Cloud Run, Koyeb...) inject the port via $PORT
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
