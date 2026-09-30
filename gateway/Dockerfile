# Build on a host that cannot reach Docker Hub / PyPI directly by overriding
# BASE_IMAGE and PIP_INDEX_URL with mirrors.
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CX2CC_GW_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r requirements.txt
COPY cx2cc_gateway ./cx2cc_gateway

RUN useradd --system --uid 10001 --home /app gateway && mkdir -p /data && chown gateway /data
USER gateway

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
CMD ["python", "-m", "cx2cc_gateway", "serve"]
