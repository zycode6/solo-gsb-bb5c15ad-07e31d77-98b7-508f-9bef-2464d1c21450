# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first to maximize layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY app ./app

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

# Shell form so UVICORN_WORKERS expands (default set in ENV above).
CMD exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers "${UVICORN_WORKERS:-2}" --log-level "${LOG_LEVEL:-info}"
