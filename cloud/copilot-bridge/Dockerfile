# Copilot bridge image (ask #2341): the pinned copilot-api 0.7.0 (package-lock.json pins every dependency by integrity) and the policy front.
# No secret is built in: the owner's token is a Secret mounted at run time (cloud/k8s/copilot-bridge.yaml). Non-root. Only the front's port is exposed.
# Build from this directory: docker build -t <registry>/copilot-bridge cloud/copilot-bridge
# Base and Node pinned exactly as cloud/worker/Dockerfile; a bump updates both files together.
FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
 && rm -rf /var/lib/apt/lists/*

# ---- Node runtime: the same one pinned release and sha256 constants as cloud/worker/Dockerfile, checked before it is unpacked ----
ARG NODE_VERSION=22.23.4
ARG NODE_SHA256_X64=1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce
ARG NODE_SHA256_ARM64=6287aa46b6c375d816e63ae66d024fbbfd70404825aabdfdf898a970871d2f4b
RUN set -eu; case "$(dpkg --print-architecture)" in amd64) a=x64; h="${NODE_SHA256_X64}" ;; arm64) a=arm64; h="${NODE_SHA256_ARM64}" ;; *) echo "unsupported architecture" >&2; exit 1 ;; esac \
 && f="node-v${NODE_VERSION}-linux-${a}.tar.xz" \
 && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/${f}" -o "/tmp/${f}" \
 && echo "${h}  /tmp/${f}" | sha256sum -c - \
 && mkdir -p /opt/node && tar -xJf "/tmp/${f}" -C /opt/node --strip-components=1 \
 && ln -s /opt/node/bin/node /usr/local/bin/node && ln -s /opt/node/bin/npm /usr/local/bin/npm && ln -s /opt/node/bin/npx /usr/local/bin/npx \
 && chown -R root:root /opt/node && rm -f "/tmp/${f}"
# ---- end Node runtime ----

RUN userdel -r ubuntu 2>/dev/null || true && useradd -m -u 10001 -s /usr/sbin/nologin bridge
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --omit=dev && chown -R root:root /app && chmod -R go-w /app
COPY bridge-front.mjs entrypoint.sh ./
RUN chown root:root bridge-front.mjs entrypoint.sh && chmod 0755 entrypoint.sh && chmod 0644 bridge-front.mjs
USER 10001
ENV HOME=/run/home
EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
