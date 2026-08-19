FROM python:3.13-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ENERGYLAB_HOST=0.0.0.0 \
    ENERGYLAB_PORT=8090 \
    ENERGYLAB_DATA_DIR=/data

RUN apk add --no-cache tzdata \
    && addgroup -S energylab \
    && adduser -S -G energylab energylab \
    && mkdir -p /app /data \
    && chown -R energylab:energylab /app /data

COPY --chown=energylab:energylab app.py /app/app.py

USER energylab
WORKDIR /app
VOLUME ["/data"]
EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/api/health', timeout=3)" || exit 1

CMD ["python", "/app/app.py"]
