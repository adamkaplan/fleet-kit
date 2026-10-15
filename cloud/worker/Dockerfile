# Cloud worker image.
# Nested Docker needs the Sysbox runtime. Runs as a non-root user. No secret is
# built in: the launcher supplies every credential at start (see cloud/README.md).
FROM ubuntu:24.04
ARG OPENCODE_VERSION=2.0.28
ARG OPENCODE_SHA256=15cc878cfc7462fe477238538df27ae3fbd03c1ea7631615cacf0977f5b629c6
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg git openssh-client python3 python3-pip python3-venv \
      sudo iptables jq procps \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc \
 && echo "deb [signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" \
      > /etc/apt/sources.list.d/docker.list \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli.gpg \
 && echo "deb [signed-by=/etc/apt/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends \
      docker-ce docker-ce-cli containerd.io docker-compose-plugin docker-buildx-plugin gh \
 && rm -rf /var/lib/apt/lists/*

# OpenCode v2: a pinned standalone binary (no node or npm), checked against its sha256 before it is unpacked.
RUN curl -fsSL "https://opencode.ai/files/bin/${OPENCODE_VERSION}/opencode-linux-x64.tar.gz" -o /tmp/oc.tgz \
 && echo "${OPENCODE_SHA256}  /tmp/oc.tgz" | sha256sum -c - \
 && tar -xzf /tmp/oc.tgz -C /usr/local/bin opencode \
 && chmod 0755 /usr/local/bin/opencode && rm /tmp/oc.tgz

# Non-root worker. It may start dockerd (and nothing else) as root: the daemon only
# runs inside the Sysbox pod's own user namespace.
RUN userdel -r ubuntu 2>/dev/null || true \
 && useradd -m -u 1000 -s /bin/bash worker && usermod -aG docker worker \
 && echo 'worker ALL=(root) NOPASSWD: /usr/local/bin/start-dockerd' > /etc/sudoers.d/worker-dockerd \
 && chmod 0440 /etc/sudoers.d/worker-dockerd

COPY worker/start-dockerd.sh /usr/local/bin/start-dockerd
COPY worker/entrypoint.sh /usr/local/bin/worker-entrypoint
COPY launcher/end_hook.py /usr/local/bin/cw-end-hook
COPY launcher/token_mint.py /usr/local/bin/cw-token-mint
COPY worker/activity.py /usr/local/bin/cw-worker-activity
RUN chmod 0755 /usr/local/bin/start-dockerd /usr/local/bin/worker-entrypoint \
      /usr/local/bin/cw-end-hook /usr/local/bin/cw-token-mint /usr/local/bin/cw-worker-activity

USER worker
WORKDIR /home/worker
EXPOSE 4096
ENTRYPOINT ["/usr/local/bin/worker-entrypoint"]
# The launcher sets OPENCODE_SERVER_PASSWORD at run time; it is never in the image.
CMD ["opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"]
