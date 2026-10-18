# Computer service: Chromium (Playwright) + restricted terminal. Unprivileged user (same UID as the app image, so
# the shared workspace volume is writable by both), no DB credentials, no Docker socket, no host home mount.
# rlimits apply to terminal processes (Linux).
# Newest published image is v1.63.0; the code pins playwright==1.64.0, so the matching Chromium build is installed
# below (system dependencies come from the base image).
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_BREAK_SYSTEM_PACKAGES=1
WORKDIR /app
COPY services/api/requirements.in services/api/requirements.lock services/api/
RUN python3 -m pip install -r services/api/requirements.in -c services/api/requirements.lock \
 && python3 -m playwright install --with-deps chromium \
 && chmod -R a+rX "${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}"
COPY services/api/localcrew services/api/localcrew
COPY services/computer services/computer
ENV PYTHONPATH=/app/services/api COMPUTER_HOST=0.0.0.0 COMPUTER_PORT=8765
RUN useradd --create-home --uid 10001 lc && mkdir -p /data/browser-profiles && chown -R lc:lc /data
USER lc
EXPOSE 8765
CMD ["python3", "services/computer/computer.py"]
