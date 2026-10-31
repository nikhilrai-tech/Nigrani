# ---- build the web UI ----
FROM node:25-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- runtime ----
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    NIGRANI_STATIC_DIR=/app/static NIGRANI_DATA_DIR=/data
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/nigrani ./nigrani
COPY --from=web /web/dist ./static
RUN useradd --system --uid 10001 nigrani && mkdir -p /data && chown nigrani /data
USER nigrani
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)" || exit 1
CMD ["uvicorn", "nigrani.main:app", "--host", "0.0.0.0", "--port", "8080"]
