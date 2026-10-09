FROM python:3.13-alpine

WORKDIR /app

COPY requirements.lock requirements.txt ./
RUN pip install --no-cache-dir -r requirements.lock

RUN addgroup -S exporter \
    && adduser -S -G exporter exporter

COPY beszel_exporter.py gunicorn.conf.py ./

RUN chown -R exporter:exporter /app \
    && chmod 0755 /app \
    && chmod 0644 /app/beszel_exporter.py /app/gunicorn.conf.py /app/requirements.lock /app/requirements.txt

USER exporter

ENV LISTEN_HOST=0.0.0.0 \
    LISTEN_PORT=9105

EXPOSE 9105

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- "http://127.0.0.1:${LISTEN_PORT}/healthz" >/dev/null || exit 1

CMD ["gunicorn", "--config", "gunicorn.conf.py", "beszel_exporter:app"]
