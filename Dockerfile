FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements/build.lock requirements/server.lock requirements/mcp.lock requirements/
RUN python -m venv /opt/glassboard-venv \
    && /opt/glassboard-venv/bin/pip install --no-cache-dir --require-hashes \
       -r requirements/build.lock -r requirements/server.lock -r requirements/mcp.lock
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
RUN /opt/glassboard-venv/bin/pip install --no-cache-dir --no-deps --no-build-isolation .

FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
ENV PATH=/opt/glassboard-venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GLASSBOARD_DB=/data/board.db \
    GLASSBOARD_ALLOWED_HOSTS=localhost,127.0.0.1
RUN apt-get update \
    && apt-get install -y --no-install-recommends age \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 glassboard \
    && useradd --uid 10001 --gid glassboard --home-dir /data --no-create-home --shell /usr/sbin/nologin glassboard \
    && install -d -o glassboard -g glassboard -m 0700 /data /client /client/config /client/state
COPY --from=builder /opt/glassboard-venv /opt/glassboard-venv
USER 10001:10001
WORKDIR /data
EXPOSE 8000
CMD ["uvicorn", "glassboard.serve:app", "--host", "127.0.0.1", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
