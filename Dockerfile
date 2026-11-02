# Rename this file to exactly `Dockerfile` (no extension) when you push to GitHub.
# The Databricks workspace editor appends `.py` to extension-less files.

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent_app_public_proxy.py .

# Render/Cloud Run inject PORT; the app reads it at startup
ENV PORT=8080
EXPOSE 8080

CMD ["python", "agent_app_public_proxy.py"]
