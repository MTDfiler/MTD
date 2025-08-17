# --- Build React UI (in subfolder vat-filer-ui) ---
FROM node:20-slim AS ui
WORKDIR /ui
COPY vat-filer-ui/ ./
RUN npm ci && npm run build

# --- Python runtime with all deps installed ---
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# (optional) system build tools
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# python deps
COPY requirements.txt ./
RUN python -m pip install --upgrade pip setuptools wheel \
 && python -m pip install --no-cache-dir -r requirements.txt

# app code
COPY . .
# copy built UI into FastAPI static dir
COPY --from=ui /ui/dist/ ./static/app/

EXPOSE 8000
CMD ["python","-m","uvicorn","main:app","--host","0.0.0.0","--port","8000"]
