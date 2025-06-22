# Dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY GetNewsAPI/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY GetNewsAPI/ .

CMD ["python", "app.py"]
