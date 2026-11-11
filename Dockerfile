# Binance VWAP + EMA9 bot
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC

WORKDIR /app

# Install Python packages first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Non-root user (uid 1000 matches the first user on most Linux machines)
RUN useradd --create-home --uid 1000 botuser

COPY --chown=botuser:botuser . .
RUN mkdir -p /app/data && chown -R botuser:botuser /app/data

USER botuser

# Keep state, logs and trade CSV in /app/data (mounted as a volume by docker-compose)
ENV STATE_FILE=/app/data/state.json \
    LOG_FILE=/app/data/logs/bot.log \
    TRADE_LOG_CSV=/app/data/trades.csv

VOLUME ["/app/data"]

# Dashboard port (only used by the dashboard service)
EXPOSE 8080

# Safe default: whatever config.yaml says (paper mode out of the box)
CMD ["python", "bot.py"]
