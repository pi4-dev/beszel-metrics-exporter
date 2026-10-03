FROM python:3.13-alpine

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY beszel_exporter.py .

RUN addgroup -S exporter \
    && adduser -S -G exporter exporter

USER exporter

EXPOSE 9105

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://127.0.0.1:9105/healthz >/dev/null || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:9105", "--workers", "1", "--threads", "4", "--timeout", "30", "beszel_exporter:app"]
