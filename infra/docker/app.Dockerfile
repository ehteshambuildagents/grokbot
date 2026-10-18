# API + worker image (also serves the built web app). Runs as an unprivileged user.
FROM node:24.19.0-bookworm-slim AS web
WORKDIR /web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci
COPY apps/web/ ./
RUN npm run build

FROM python:3.13.15-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd --create-home --uid 10001 app && mkdir -p /data /keys && chown app:app /data /keys
WORKDIR /app
COPY services/api/requirements.in services/api/requirements.lock services/api/
RUN pip install -r services/api/requirements.in -c services/api/requirements.lock
COPY services/ services/
COPY infra/scripts/ infra/scripts/
COPY --from=web /web/dist apps/web/dist
ENV PYTHONPATH=/app/services/api
USER app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "localcrew.api:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "services/api"]
