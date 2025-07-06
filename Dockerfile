# Dockerfile

# 1) Base image
FROM python:3.10-slim

# 2) Set workdir
WORKDIR /app

# 3) (Optional) Switch to faster Russian mirrors if the default exists
RUN if [ -f /etc/apt/sources.list ]; then \
      sed -i 's|http://deb.debian.org/debian|http://mirror.yandex.ru/debian|g' /etc/apt/sources.list && \
      sed -i 's|http://security.debian.org/debian-security|http://mirror.yandex.ru/debian-security|g' /etc/apt/sources.list; \
    fi

# 4) Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      python3-pip \
      ffmpeg \
      build-essential \
      gcc \
      python3-dev \
      git && \
    rm -rf /var/lib/apt/lists/*

# 5) Install Python dependencies
COPY requirements.txt ./
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 6) Install FS-EEND (Hitachi-speech) into both API and workers
#     клонируем репозиторий и ставим локально — чтобы появился модуль eend.inference
RUN git clone https://github.com/hitachi-speech/EEND.git /app/eend

# 7) Copy application code
COPY . .

# 8) Default command — run the FastAPI app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]