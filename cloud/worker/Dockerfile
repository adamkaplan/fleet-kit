# Cloud worker image.
# Nested Docker needs the Sysbox runtime. Runs as a non-root user. No secret is
# built in: the launcher supplies every credential at start (see cloud/README.md).
# Base pinned by the index digest of ubuntu:24.04 (read from the registry); bump both together.
# The real `gh` is NOT installed: the worker's GitHub verbs come from cw-gh, a small client of our own (cloud/README.md,
# "What the token allows"). That narrows accidents only; the token's scope and the repository's rules are the boundary.
# apt packages below are deliberately unversioned: they follow the base's security updates.
FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55 AS worker
ARG OPENCODE_VERSION=2.0.28
ARG OPENCODE_SHA256=15cc878cfc7462fe477238538df27ae3fbd03c1ea7631615cacf0977f5b629c6
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg git openssh-client python3 python3-pip python3-venv \
      xz-utils sudo iptables jq procps \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc \
 && echo "deb [signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update && apt-get install -y --no-install-recommends \
      docker-ce docker-ce-cli containerd.io docker-compose-plugin docker-buildx-plugin \
 && rm -rf /var/lib/apt/lists/*

# OpenCode v2: a pinned standalone binary (no node or npm), checked against its sha256 before it is unpacked.
RUN curl -fsSL "https://opencode.ai/files/bin/${OPENCODE_VERSION}/opencode-linux-x64.tar.gz" -o /tmp/oc.tgz \
 && echo "${OPENCODE_SHA256}  /tmp/oc.tgz" | sha256sum -c - \
 && tar -xzf /tmp/oc.tgz -C /usr/local/bin opencode \
 && chmod 0755 /usr/local/bin/opencode && rm /tmp/oc.tgz

# ---- Node runtime (ask 1040): one pinned release, its sha256 pinned here, checked before it is unpacked ----
# NODE_VERSION and the two sha256 constants of that release's .tar.xz (one per architecture) are the only constants.
# A version bump = update the version AND both constants (see cloud/README.md). No checksum list is downloaded.
# Root-owned /opt/node, linked into /usr/local/bin.
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

# Non-root worker. It may start dockerd (and nothing else) as root: the daemon only
# runs inside the Sysbox pod's own user namespace.
RUN userdel -r ubuntu 2>/dev/null || true \
 && useradd -m -u 1000 -s /bin/bash worker && usermod -aG docker worker \
 && echo 'worker ALL=(root) NOPASSWD: /usr/local/bin/start-dockerd' > /etc/sudoers.d/worker-dockerd \
 && chmod 0440 /etc/sudoers.d/worker-dockerd

COPY worker/start-dockerd.sh /usr/local/bin/start-dockerd
COPY worker/entrypoint.sh /usr/local/bin/worker-entrypoint
COPY worker/guard.sh worker/opencode-worker.json worker/agents/worker.md worker/agents/orchestrator.md /usr/local/share/cw/
COPY launcher/end_hook.py /usr/local/bin/cw-end-hook
COPY launcher/token_mint.py /usr/local/bin/cw-token-mint
COPY worker/activity.py /usr/local/bin/cw-worker-activity
COPY worker/guard-table.tsv /usr/local/share/cw/guard-table.tsv
COPY worker/cw-gh /usr/local/bin/cw-gh
COPY worker/probe-reach.sh /usr/local/bin/cw-probe-reach
COPY worker/bin/cloud_cli.py /usr/local/bin/cw-cloud
COPY worker/bin/cloud_client.py worker/bin/cloud_hub.py worker/bin/cloud_channel.py worker/bin/cloud_channel_server.py /usr/local/bin/
COPY worker/cw-plan-check worker/plan_check.py /usr/local/bin/
RUN chmod 0755 /usr/local/bin/start-dockerd /usr/local/bin/worker-entrypoint \
      /usr/local/bin/cw-end-hook /usr/local/bin/cw-token-mint /usr/local/bin/cw-worker-activity \
      /usr/local/bin/cw-gh /usr/local/bin/cw-probe-reach /usr/local/bin/cw-plan-check /usr/local/bin/cw-cloud \
      /usr/local/bin/cloud_client.py /usr/local/bin/cloud_hub.py /usr/local/bin/cloud_channel.py /usr/local/bin/cloud_channel_server.py \
 && chmod 0644 /usr/local/bin/plan_check.py \
 && chmod 0755 /usr/local/share/cw/guard.sh && chmod 0644 /usr/local/share/cw/opencode-worker.json /usr/local/share/cw/worker.md /usr/local/share/cw/orchestrator.md

USER worker
WORKDIR /home/worker
EXPOSE 4096
ENTRYPOINT ["/usr/local/bin/worker-entrypoint"]
# The launcher sets OPENCODE_SERVER_PASSWORD at run time; it is never in the image.
CMD ["opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"]

# ---- build-time secret scan (the stages below the line "FROM worker AS scan" are reused by cloud/tests/test-cloud) ----
# The scan runs INSIDE the (remote, linux/amd64) build, where the files can be read: `docker save` on an arm64
# workstation cannot export an amd64-only image's layers. It reads the final filesystem of the `worker` stage as root;
# a hit (exit 1) or a file it cannot read (exit 2) fails the build, so nothing is published. A secret written in one
# layer and deleted in a later one is NOT visible here (the layers are already merged): no step of `worker` takes a
# credential, and a person who adds one must not write it to disk.
FROM worker AS scan
USER root
COPY worker/scan-secrets.sh worker/scan-files.py worker/scan-allowlist.txt /
RUN /scan-secrets.sh --dir / --skip proc --skip sys --skip dev --skip scan-secrets.sh --skip scan-files.py && echo ok > /scan-ok

# The shipped image is `worker` plus one 3-byte marker file, /usr/local/share/cw/scan-ok ("ok"), COPIED from `scan`. It is
# there only to make `final` depend on `scan`: the legacy builder (`az acr build`, ACR Tasks) builds stages in order and
# has no `RUN --mount`, so a plain COPY --from is what forces the scan to run first and fail the build on a hit. The
# marker is root-owned and world-readable (mode 0644); nothing executes or reads it. This file uses no BuildKit-only
# syntax (no --mount, heredoc, # syntax=, --link, --exclude): cloud/tests/test-cloud fails if one appears.
FROM worker AS final
COPY --from=scan /scan-ok /usr/local/share/cw/scan-ok
