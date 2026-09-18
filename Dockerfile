# AdSpy v2 — Docker image (Coolify deploy ke liye)
#
# Build context: ye folder (adspy2-docker/). Runtime code src/ me hai
# (server package se COPY kiya hua — original files ko haath nahi lagaya gaya).
#
# Build:  docker build -t adspy2 ./adspy2-docker
# Run:    docker run -p 4022:4022 \
#           -e ADSPY2_SERVER_MODE=1 \
#           -e ADSPY2_ADMIN_PASSWORD='...' \
#           -e ADSPY2_SECRET_KEY='...' \
#           -e ADSPY2_PUBLIC_URL='https://ads.example.com' \
#           -v adspy2-data:/app/data \
#           adspy2

FROM python:3.12-slim

# Gunicorn flags — adspy2 launcher ke barabar:
#   --workers 1  : SQLite ka exactly ONE writer ho sakta hai. KABHI badhana nahi.
#   --threads 4  : concurrency threads se aati hai, workers se nahi.
#   --timeout 120: lambi page-scan requests katni nahi chahiye.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Pehle sirf requirements (layer cache ke liye), phir code.
COPY src/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src/app /app/app
COPY src/migrations /app/migrations
COPY src/templates /app/templates
COPY src/static /app/static
COPY src/deploy/backup.sh /app/deploy/backup.sh
RUN chmod +x /app/deploy/backup.sh

# Data dir (SQLite DBs, dataset sidecar, backups, media thumbs).
# Isko Coolify me persistent volume se mount karo, warna redeploy par data udega.
VOLUME ["/app/data"]
RUN mkdir -p /app/data /app/logs

# Port: ADSPY2_PORT jeetta hai, phir Coolify ka PORT, phir default 4022.
# Container ke andar 0.0.0.0 par bind ZAROORI hai — bahar se Coolify ka Traefik
# aayega (host networking nahi hai). 127.0.0.1 yahan kaam nahi karega.
EXPOSE 4022

# /health bina login ke public hai (server mode me bhi) — isiliye healthcheck ke liye sahi hai.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request; p=os.environ.get('ADSPY2_PORT') or os.environ.get('PORT') or '4022'; urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=4)"

CMD gunicorn "app:create_app()" \
      --bind "0.0.0.0:${ADSPY2_PORT:-${PORT:-4022}}" \
      --workers 1 \
      --threads "${ADSPY2_THREADS:-4}" \
      --timeout "${ADSPY2_TIMEOUT:-120}" \
      --access-logfile - \
      --error-logfile -
