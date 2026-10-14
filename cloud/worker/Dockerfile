# fleet-kit cloud worker image: OpenCode headless, git, gh, Node, Python, and Docker
# for nested use under the Sysbox runtime. Runs as a non-root user. No secret is
# built in: the launcher supplies every credential at start (see cloud/README.md).
FROM node:22-bookworm-slim AS node

FROM ubuntu:24.04
ARG OPENCODE_VERSION=latest
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

# Node from the official image (copied, not piped from the network).
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && npm install -g "opencode-ai@${OPENCODE_VERSION}" && npm cache clean --force

# Non-root worker. It may start dockerd (and nothing else) as root: the daemon only
# runs inside the Sysbox pod's own user namespace.
RUN userdel -r ubuntu 2>/dev/null || true \
 && useradd -m -u 1000 -s /bin/bash worker && usermod -aG docker worker \
 && echo 'worker ALL=(root) NOPASSWD: /usr/local/bin/start-dockerd' > /etc/sudoers.d/worker-dockerd \
 && chmod 0440 /etc/sudoers.d/worker-dockerd

COPY worker/start-dockerd.sh /usr/local/bin/start-dockerd
COPY worker/entrypoint.sh /usr/local/bin/worker-entrypoint
RUN chmod 0755 /usr/local/bin/start-dockerd /usr/local/bin/worker-entrypoint

USER worker
WORKDIR /home/worker
EXPOSE 4096
ENTRYPOINT ["/usr/local/bin/worker-entrypoint"]
# The launcher sets OPENCODE_SERVER_PASSWORD at run time; it is never in the image.
CMD ["opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"]
