FROM python:3.10-slim AS base

# system deps
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .

# Python deps
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]