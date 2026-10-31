# PayMates as ONE web service: FastAPI serves the API and the pre-built web app (frontend/dist).
# Build the web app first (cd frontend && npm install && npm run build) and commit frontend/dist.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PAYMATES_PACING_MS=450

WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend backend
COPY shared shared
COPY frontend/dist frontend/dist

# Secrets and model names (GEMINI_API_KEY, PAYMATES_*_MODEL, ...) come from the host's environment, never from the image.
WORKDIR /app/backend
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
