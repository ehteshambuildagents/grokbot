# Computer service: Chromium (Playwright) + restricted terminal. Unprivileged user, no DB credentials,
# no Docker socket, no host home mount. rlimits apply to terminal processes (Linux).
FROM mcr.microsoft.com/playwright/python:v1.64.0-noble
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY services/api/requirements.in services/api/requirements.lock services/api/
RUN pip install -r services/api/requirements.in -c services/api/requirements.lock
COPY services/api/localcrew services/api/localcrew
COPY services/computer services/computer
ENV PYTHONPATH=/app/services/api COMPUTER_HOST=0.0.0.0 COMPUTER_PORT=8765
USER pwuser
EXPOSE 8765
CMD ["python", "services/computer/computer.py"]
