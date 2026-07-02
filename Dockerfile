FROM python:3.11-slim

# LibreOffice provides the headless conversion engine.
# --no-install-recommends keeps the image smaller.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libreoffice \
        fonts-dejavu \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Render sets $PORT at runtime; default to 10000 for local testing.
ENV PORT=10000
EXPOSE 10000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
