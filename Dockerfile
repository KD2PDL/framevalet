FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends rclone curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# cloudflared for the optional Cloudflare Tunnel (Admin > Remote access)
RUN curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$(dpkg --print-architecture)" \
      -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared

WORKDIR /srv/framevalet
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

ENV DATA_DIR=/data PORT=8470 HOST=0.0.0.0
RUN useradd --system --create-home --uid 10001 framevalet \
    && mkdir -p /data && chown framevalet:framevalet /data
USER framevalet
VOLUME /data
EXPOSE 8470

HEALTHCHECK --interval=60s --timeout=5s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/login')" || exit 1

CMD ["framevalet"]
