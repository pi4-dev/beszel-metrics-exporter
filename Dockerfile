FROM python:3.13-alpine

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN addgroup -S exporter \
    && adduser -S -G exporter exporter

COPY beszel_exporter.py .

# Do not rely on source-file permissions from the build host/NAS.
# The runtime user must always be able to traverse /app and read the module.
RUN chown -R exporter:exporter /app \
    && chmod 0755 /app \
    && chmod 0644 /app/beszel_exporter.py /app/requirements.txt

USER exporter

EXPOSE 9105

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://127.0.0.1:9105/healthz >/dev/null || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:9105", "--workers", "1", "--threads", "4", "--timeout", "30", "beszel_exporter:app"]
