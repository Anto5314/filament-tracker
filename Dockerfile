FROM python:3.12-slim

WORKDIR /app
COPY collector.py /app/collector.py
COPY gcode_parser.py /app/gcode_parser.py
COPY mock_k1.py /app/mock_k1.py
COPY static /app/static
RUN pip install --no-cache-dir websockets aiohttp

ENV K1_HOST=192.168.1.41 \
    K1_PORT=9999 \
    DB_PATH=/data/k1_sessions.db \
    SPOOLMAN_URL=http://spoolman:8000 \
    WEB_PORT=8123 \
    STATIC_DIR=/app/static

VOLUME /data
EXPOSE 8123

CMD ["python", "/app/collector.py"]