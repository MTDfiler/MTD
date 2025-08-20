# ---------- Build the Vite UI ----------
FROM node:18-alpine AS ui
WORKDIR /ui

# Install UI deps
COPY vat-filer-ui/package*.json ./
RUN npm ci

# Build UI
COPY vat-filer-ui/ ./
RUN npm run build

# ---------- Python backend ----------
FROM python:3.11-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# (optional) system build tools for any pip wheels
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Backend deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Bring in the built UI -> served from /static/app
COPY --from=ui /ui/dist /app/static/app

# Render will supply $PORT; locally we can map 8000
EXPOSE 8000
CMD python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
