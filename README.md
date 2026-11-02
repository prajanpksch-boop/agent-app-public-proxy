# Agent App Public Proxy

A public reverse proxy that exposes a **private Databricks App** to external users
— people who are NOT members of your Databricks workspace.

```
public user --> this proxy (Render / Cloud Run, free tier)
                   \--> SP OAuth token --> Databricks App URL (private)
```

## Files

| File | Purpose |
| --- | --- |
| `agent_app_public_proxy.py` | The proxy (FastAPI + httpx) |
| `requirements.txt` | Python dependencies |
| `Dockerfile.py` | Docker build file — **rename to `Dockerfile` (no extension)** when you push to GitHub |

## Step 1 — One-time Databricks setup (needs a workspace admin)

```bash
# Create the proxy identity (service principal). Note the `application_id` (UUID).
databricks service-principals create --display-name "agent-app-public-proxy" --output JSON

# Create its OAuth secret. Note the `client_secret`. (Replace <SP_ID> with the numeric `id`.)
databricks service-principals create-oauth-secret <SP_ID> --output JSON

# Grant it access to the app (Replace <SP_APPLICATION_ID> with the UUID from step 1.)
databricks apps update-permissions agent-app-first-prajanpks \
  --json '{"permission_changes":[{"service_principal_name":"<SP_APPLICATION_ID>","permission_level":"CAN_USE"}]}'
```

## Step 2 — Environment variables

| Variable | Required | Value |
| --- | --- | --- |
| `APP_URL` | yes | `https://agent-app-first-prajanpks-7474653427732724.aws.databricksapps.com` |
| `DATABRICKS_HOST` | yes | `https://dbc-79e3d78c-27f0.cloud.databricks.com` |
| `SP_CLIENT_ID` | yes | SP `application_id` from Step 1 |
| `SP_CLIENT_SECRET` | yes | OAuth `client_secret` from Step 1 |
| `PROXY_API_KEY` | recommended | Any long random string; users must send it as `X-API-Key` header. Empty = open to everyone (not recommended) |

Set these in your host's dashboard (Render → Environment, Cloud Run → `--set-env-vars`) — **never commit them to Git**.

## Step 3 — Deploy on Render (free, easiest)

1. Push this folder to a GitHub repo (rename `Dockerfile.py` → `Dockerfile` first).
2. On [render.com](https://render.com): **New → Web Service**, connect the repo.
3. Build command: `pip install -r requirements.txt`
   Start command: `python agent_app_public_proxy.py`
4. Add the environment variables from Step 2.
5. Deploy, then share your `https://<service>.onrender.com` URL with external users.

Free instances sleep after ~15 min idle — the first request after a pause takes
~30–60s (proxy wake-up) plus the Databricks app's own cold start.

## Alternative — Google Cloud Run (free tier)

```bash
gcloud run deploy agent-app-proxy --source . --allow-unauthenticated \
  --region us-central1 \
  --set-env-vars APP_URL=...,DATABRICKS_HOST=...,SP_CLIENT_ID=...,SP_CLIENT_SECRET=...,PROXY_API_KEY=...
```

## Test it

```bash
curl -H "X-API-Key: <your key>" https://<your-proxy-url>/
```

You should see your app's chat UI HTML. Open the proxy URL in a browser — the app
loads without any Databricks login.

## Security notes

* All public traffic runs under the service principal: Databricks costs and audit
  logs are attributed to the SP, not to individual end users.
* Keep `SP_CLIENT_SECRET` secret — anyone holding it can use your app directly.
* Rate limiting is in-memory (30 req/min per IP); use Redis if you scale to multiple instances.
* Share only the proxy URL; never share the direct Databricks app URL or secrets.
