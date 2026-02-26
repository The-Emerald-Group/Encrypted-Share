# ── Builder: install Python deps ─────────────────────────────────────────────
FROM python:3.13-alpine AS builder

WORKDIR /build
COPY app/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Runner ────────────────────────────────────────────────────────────────────
FROM python:3.13-alpine

WORKDIR /app

# Copy installed packages
COPY --from=builder /install /usr/local

# Copy application
COPY app/main.py .
COPY app/VERSION .
COPY frontend ./frontend

ENV FRONTEND_PATH="./frontend"
ENV REDIS="redis://redis/"
ENV VERBOSITY="INFO"

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
