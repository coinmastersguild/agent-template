# The agent runtime. `make run` builds it locally; Pioneer runs the same image
# built from the upstream template at a pinned tag. Your copy is used only locally.
FROM ubuntu:24.04
ARG TARGETARCH
ARG OPENHUMAN_VERSION=0.64.10
ARG DOTENVX_VERSION=2.33.0
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git python3 python3-cryptography \
 && rm -rf /var/lib/apt/lists/*
RUN set -eu; case "$TARGETARCH" in \
    arm64) oh=aarch64; oh_sum=445be5fd3d60d642635165412d6ea1dd445c75fa23feedb81e35200f4c8b338f; dx_sum=e75dea840fe409fbd16c9958bfb9927cfb6176ed72bea98c2308fc018c384455 ;; \
    amd64) oh=x86_64;  oh_sum=84dba6f33305bada121df8c59b7531e57918b4be623b4dfb6de7232a2b950665; dx_sum=a216db3b230ab97b29e564944e6f70646a843c5ee2c81d6fa7dddedcd950da39 ;; \
    *) echo "unsupported arch $TARGETARCH"; exit 1 ;; esac; \
  curl -fsSL -o /tmp/oh.tgz "https://github.com/tinyhumansai/openhuman/releases/download/v${OPENHUMAN_VERSION}/openhuman-core-${OPENHUMAN_VERSION}-${oh}-unknown-linux-gnu.tar.gz"; \
  echo "$oh_sum  /tmp/oh.tgz" | sha256sum -c -; tar -xzf /tmp/oh.tgz -C /usr/local/bin; rm -f /usr/local/bin/openhuman-tui; \
  curl -fsSL -o /tmp/dx.tgz "https://github.com/dotenvx/dotenvx/releases/download/v${DOTENVX_VERSION}/dotenvx-${DOTENVX_VERSION}-linux-${TARGETARCH}.tar.gz"; \
  echo "$dx_sum  /tmp/dx.tgz" | sha256sum -c -; tar -xzf /tmp/dx.tgz -C /usr/local/bin; rm /tmp/*.tgz
RUN userdel --remove ubuntu && useradd --uid 1000 --create-home agent
# The trusted supervisor lives in the image, never in the user's checkout. It runs as
# root to hold the unlock key; OpenHuman and every tool run as `agent` (uid 1000).
COPY runtime/supervisor.py runtime/envfile.py /opt/agent-runtime/
RUN chmod 0755 /opt/agent-runtime && chmod 0644 /opt/agent-runtime/*.py
WORKDIR /agent
ENV OPENHUMAN_WORKSPACE=/data OPENHUMAN_CORE_HOST=0.0.0.0
EXPOSE 7788
# Run with --init and a root-only tmpfs at /run/agent (see README).
CMD ["python3", "/opt/agent-runtime/supervisor.py"]
