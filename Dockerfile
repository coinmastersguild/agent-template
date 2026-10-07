# Generic local core from the public Pioneer fork. The hosted native desktop
# runtime is a separate trusted image; your checkout is used only locally.
FROM ubuntu:24.04
ARG TARGETARCH
ARG OPENHUMAN_RELEASE=pioneer-local-v0.1.0
ARG OPENHUMAN_SHA256=ee63b0ee156e3cd1470bd88bcb1e0b608be353d2f22a22f56d82f2b1e5dc7a4a
ARG DOTENVX_VERSION=2.33.0
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git python3 python3-cryptography \
 && rm -rf /var/lib/apt/lists/*
RUN set -eu; case "$TARGETARCH" in \
    amd64) dx_sum=a216db3b230ab97b29e564944e6f70646a843c5ee2c81d6fa7dddedcd950da39 ;; \
    *) echo "The verified fork package requires linux/amd64; use Docker emulation on Apple Silicon"; exit 1 ;; esac; \
  curl -fsSL -o /tmp/oh.tgz "https://github.com/coinmastersguild/openhuman/releases/download/${OPENHUMAN_RELEASE}/openhuman-core-${OPENHUMAN_RELEASE}-x86_64-unknown-linux-gnu.tar.gz"; \
  echo "$OPENHUMAN_SHA256  /tmp/oh.tgz" | sha256sum -c -; \
  tar -xzf /tmp/oh.tgz -C /usr/local/bin openhuman-core; \
  mkdir -p /usr/share/doc/pioneer-openhuman; \
  tar -xzf /tmp/oh.tgz -C /usr/share/doc/pioneer-openhuman LICENSE BUILD.txt SOURCE.txt; \
  curl -fsSL -o /tmp/dx.tgz "https://github.com/dotenvx/dotenvx/releases/download/v${DOTENVX_VERSION}/dotenvx-${DOTENVX_VERSION}-linux-${TARGETARCH}.tar.gz"; \
  echo "$dx_sum  /tmp/dx.tgz" | sha256sum -c -; tar -xzf /tmp/dx.tgz -C /usr/local/bin; rm /tmp/*.tgz
RUN userdel --remove ubuntu && useradd --uid 1000 --create-home agent
# The trusted supervisor lives in the image, never in the user's checkout. It runs as
# root to hold the unlock key; OpenHuman and every tool run as `agent` (uid 1000).
COPY runtime/supervisor.py runtime/envfile.py /opt/agent-runtime/
RUN chmod 0755 /opt/agent-runtime && chmod 0644 /opt/agent-runtime/*.py
WORKDIR /agent
ENV OPENHUMAN_WORKSPACE=/data OPENHUMAN_CORE_HOST=0.0.0.0 OPENHUMAN_TOOL_DISPATCHER=native
EXPOSE 7788
# Run with --init and a root-only tmpfs at /run/agent (see README).
CMD ["python3", "/opt/agent-runtime/supervisor.py"]
