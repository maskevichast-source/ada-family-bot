FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ADA_STATE_DIR=/app/data CHAT_HISTORY_FILE=/app/data/chat_history.json MPLCONFIGDIR=/tmp/matplotlib
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils ffmpeg fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip check
COPY . .
RUN mkdir -p /app/data
CMD ["python", "run.py"]
