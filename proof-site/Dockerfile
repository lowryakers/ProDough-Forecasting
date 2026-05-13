FROM python:3.11-slim

# Install poppler (pdftoppm) and tesseract OCR
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY . .

# Ensure uploads directory exists
RUN mkdir -p uploads

# Railway injects PORT at runtime; default to 8080 for local Docker use
EXPOSE 8080

# Single worker — required because proof jobs run in background threads
# that live in the worker process memory. Multiple workers would each have
# their own _jobs dict and a job started in worker A wouldn't be visible to worker B.
CMD gunicorn app:app \
    --bind 0.0.0.0:${PORT:-8080} \
    --workers 1 \
    --threads 4 \
    --timeout 120 \
    --access-logfile -
