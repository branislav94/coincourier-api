FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system reporter \
    && useradd --system --gid reporter reporter \
    && mkdir -p /data/reports \
    && chown -R reporter:reporter /data/reports
COPY app.py drive_delivery.py apps_script_delivery.py ./
USER reporter
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OUTPUT_DIR=/data/reports
# Keep the container available for Dokploy Application Schedule Job (docker exec).
# No public ports, no HTTP server, and no internal cron daemon.
CMD ["sleep", "infinity"]
