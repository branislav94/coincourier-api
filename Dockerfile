FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY GetNewsAPI/requirements.txt .

RUN pip install --no-cache-dir --requirement requirements.txt \
    && groupadd --gid 10001 getnewsapi \
    && useradd --uid 10001 --gid getnewsapi --no-create-home \
        --shell /usr/sbin/nologin getnewsapi \
    && mkdir --parents /data \
    && chown 10001:10001 /data

COPY GetNewsAPI/ .
COPY maintenance/migrations/ /app/maintenance/migrations/
COPY maintenance/sql/ /app/maintenance/sql/
COPY maintenance/vector_migrations/ /app/maintenance/vector_migrations/

USER 10001:10001

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=3).read()"]

CMD ["python", "app.py"]
