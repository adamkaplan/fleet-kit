# Cloud worker image.
# Nested Docker needs the Sysbox runtime. Runs as a non-root user. No secret is
# built in: the launcher supplies every credential at start (see cloud/README.md).
# Base pinned by the index digest of ubuntu:24.04 (read from the registry); bump both together.
# The real `gh` is NOT installed: the worker's GitHub verbs come from cw-gh, a small client of our own (cloud/README.md,
# "What the token allows"). That narrows accidents only; the token's scope and the repository's rules are the boundary.
# apt packages below are deliberately unversioned: they follow the base's security updates.
FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55 AS worker
ARG OPENCODE_VERSION=2.1.4
ARG OPENCODE_SHA256=2edaf8cf96e81dec5ee996a3196b03345cf59e5a1673d83a4599bb4afc5c5f6d
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

# ---- #1165: the build tool `make` (one package, no compiler toolchain: --no-install-recommends) ----
RUN apt-get update && apt-get install -y --no-install-recommends make \
 && rm -rf /var/lib/apt/lists/*
# ---- end #1165 ----

# ask #2353: an init as the pod's first process (reaps orphans, forwards signals): the distribution's tini, no download.
RUN apt-get update && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

# #2047: system libraries (and fonts for screenshots) of Playwright's headless Chromium shell, which exits 127 without them.
RUN apt-get update && apt-get install -y --no-install-recommends libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 libcairo2 libcups2t64 libdbus-1-3 libdrm2 libfontconfig1 libfreetype6 libgbm1 libglib2.0-0t64 libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 fonts-liberation fonts-noto-color-emoji fonts-unifont fonts-freefont-ttf \
 && rm -rf /var/lib/apt/lists/*

# ---- #1165 identity: ONE root-owned read-only file written at image build; the entrypoint copies it into the global git file ----
# The pair below is the ONE constant (line one: name, line two: email). The entrypoint sets user.name and user.email in the worker
# user's global git configuration from this file, each only when unset. A repository's own configured identity, and a local
# `git config user.name/user.email`, still win over it (git's own order: system, then global, then local). No identity variable is exported.
ARG CW_DEFAULT_IDENT_NAME=cloud-worker
ARG CW_DEFAULT_IDENT_EMAIL=cloud-worker@users.noreply.github.com
RUN printf '%s\n%s\n' "${CW_DEFAULT_IDENT_NAME}" "${CW_DEFAULT_IDENT_EMAIL}" > /etc/cw-default-identity \
 && chown root:root /etc/cw-default-identity && chmod 0444 /etc/cw-default-identity
# ---- end #1165 identity ----

# OpenCode v2: a pinned standalone binary (no node or npm), checked against its sha256 before it is unpacked.
# The pin is a security floor: 2.1.4 is built on Bun 1.4.4 or later (cloud/README.md); a bump updates both ARGs.
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

# ask #2383: Go, one pinned release, its sha256 pinned here, checked before it is unpacked (the form of the Node block above).
# GO_VERSION and GO_SHA256 (of go${GO_VERSION}.linux-amd64.tar.gz) are the only constants; a bump updates both (cloud/README.md).
# Root-owned /usr/local/go, not writable by others, linked into /usr/local/bin. The two test keys of the toolchain's sources are named in cloud/worker/scan-allowlist.txt.
ARG GO_VERSION=1.27.4
ARG GO_SHA256=7226736c2339719cda28fbf3a54d71aa63bcefe376e98cef00034268dab24840
RUN curl -fsSL https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz -o /tmp/go.tgz \
 && echo "${GO_SHA256}  /tmp/go.tgz" | sha256sum -c - \
 && tar -C /usr/local -xzf /tmp/go.tgz \
 && chown -R root:root /usr/local/go && chmod -R go-w /usr/local/go \
 && ln -s /usr/local/go/bin/go /usr/local/bin/go && ln -s /usr/local/go/bin/gofmt /usr/local/bin/gofmt \
 && rm /tmp/go.tgz
# The pinned toolchain is the only one: no other toolchain is downloaded at run time.
ENV GOTOOLCHAIN=local
# ---- end Go ----

# Non-root worker. It may start dockerd (and nothing else) as root: the daemon only
# runs inside the Sysbox pod's own user namespace.
RUN userdel -r ubuntu 2>/dev/null || true \
 && useradd -m -u 1000 -s /bin/bash worker && usermod -aG docker worker \
 && echo 'worker ALL=(root) NOPASSWD: /usr/local/bin/start-dockerd, /usr/local/bin/cw-stack-pull' > /etc/sudoers.d/worker-dockerd \
 && echo 'Defaults:worker env_reset' >> /etc/sudoers.d/worker-dockerd \
 && install -d -m 0700 -o root -g root /opt/cw-registry \
 && chmod 0440 /etc/sudoers.d/worker-dockerd

COPY worker/start-dockerd.sh /usr/local/bin/start-dockerd
COPY worker/cw-stack-pull /usr/local/bin/cw-stack-pull
COPY worker/entrypoint.sh /usr/local/bin/worker-entrypoint
COPY worker/guard.sh worker/opencode-worker.json worker/agents/worker.md worker/agents/orchestrator.md /usr/local/share/cw/
COPY launcher/end_hook.py /usr/local/bin/cw-end-hook
COPY worker/cw-prestop /usr/local/bin/cw-prestop
COPY launcher/token_mint.py /usr/local/bin/cw-token-mint
COPY worker/activity.py /usr/local/bin/cw-worker-activity
COPY worker/guard-table.tsv /usr/local/share/cw/guard-table.tsv
COPY worker/cw-gh /usr/local/bin/cw-gh
COPY worker/probe-reach.sh /usr/local/bin/cw-probe-reach
COPY worker/bin/cloud_cli.py /usr/local/bin/cw-cloud
COPY worker/bin/cloud_client.py worker/bin/cloud_hub.py worker/bin/cloud_channel.py worker/bin/cloud_channel_server.py worker/bin/cloud_relay.py /usr/local/bin/
COPY worker/cw-plan-check worker/plan_check.py /usr/local/bin/
COPY worker/cw-stack-up /usr/local/bin/
COPY worker/cw-shell-check worker/cw-run /usr/local/bin/
RUN chmod 0755 /usr/local/bin/cw-shell-check /usr/local/bin/cw-run
RUN chmod 0755 /usr/local/bin/start-dockerd /usr/local/bin/cw-stack-pull /usr/local/bin/worker-entrypoint \
      /usr/local/bin/cw-end-hook /usr/local/bin/cw-prestop /usr/local/bin/cw-token-mint /usr/local/bin/cw-worker-activity \
      /usr/local/bin/cw-gh /usr/local/bin/cw-probe-reach /usr/local/bin/cw-plan-check /usr/local/bin/cw-cloud \
      /usr/local/bin/cloud_client.py /usr/local/bin/cloud_hub.py /usr/local/bin/cloud_channel.py /usr/local/bin/cloud_channel_server.py /usr/local/bin/cloud_relay.py \
 && chmod 0644 /usr/local/bin/plan_check.py \
 && chmod 0755 /usr/local/share/cw/guard.sh && chmod 0644 /usr/local/share/cw/opencode-worker.json /usr/local/share/cw/worker.md /usr/local/share/cw/orchestrator.md
RUN chmod 0755 /usr/local/bin/cw-stack-up && chown root:root /usr/local/bin/cw-stack-up
# The token service (cloud/k8s/token-mint.yaml runs `python3 /opt/cloud/launcher/token_service.py`): the launcher modules it imports,
# directly or through cloud_worker (token_service -> app_token, cloud_worker; app_token -> token_mint; cloud_worker -> run_record, wip_save,
# path_walk). path_walk.py is a byte-identical copy of bin/path_walk.py (bin/ is outside the build context); a CI test pins it.
# Root-owned, mode 0644, no secret, not executable; the secret scan below reads them. end_hook and token_mint keep their COPY lines above.
COPY launcher/token_service.py launcher/app_token.py launcher/token_mint.py launcher/cloud_worker.py launcher/run_record.py launcher/wip_save.py launcher/path_walk.py /opt/cloud/launcher/
RUN chown -R root:root /opt/cloud && chmod 0755 /opt/cloud /opt/cloud/launcher && chmod 0644 /opt/cloud/launcher/*.py

USER worker
WORKDIR /home/worker
EXPOSE 4096
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/usr/local/bin/worker-entrypoint"]
# The launcher sets OPENCODE_SERVER_PASSWORD at run time; it is never in the image.
CMD ["opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"]

# ---- #1353 PostgreSQL build stage: compilers live ONLY here; the worker stage copies the installed files (/opt/pg) and nothing else ----
# The pins below are the only constants: PostgreSQL 16 = one version + one sha256 (the release tarball from the project's own
# download area, checked with `sha256sum -c` BEFORE it is unpacked); each extension = one version (directory/label only) + one
# 40-hex COMMIT (a tag can be moved, a commit cannot): the build clones the publisher's repository, checks out that commit and
# fails unless `git rev-parse HEAD` equals it, BEFORE any build step. The base (index digest of buildpack-deps:24.04, read from the registry) already holds the compilers,
# git, curl and bzip2, so the file adds no apt package; a mismatch fails the build. A bump edits that source's version AND its sha256 or commit (cloud/README.md).
FROM buildpack-deps:24.04@sha256:390065857fa32d431d0e09d2442f16470fbf474337fca5a0113add96db5516ed AS pgbuild
ARG PG_VERSION=16.15
ARG PG_SHA256=c1575341fa7bd40f5274ea465b34390f4dc64cdd0770af327005caaeb9f6b7ed
ARG PGVECTOR_VERSION=0.8.7
ARG PGVECTOR_COMMIT=f37c13f68b57d2c3472b2214fbcff699d6d34876
ARG PGMQ_VERSION=1.13.0
ARG PGMQ_COMMIT=32c075bb6dbed66a303d1a792393c93e36c09a97
RUN set -eu; curl -fsSL "https://ftp.postgresql.org/pub/source/v${PG_VERSION}/postgresql-${PG_VERSION}.tar.bz2" -o /tmp/pg.tar.bz2 \
 && echo "${PG_SHA256}  /tmp/pg.tar.bz2" | sha256sum -c - \
 && mkdir -p /src/pg && tar -xjf /tmp/pg.tar.bz2 -C /src/pg --strip-components=1 && rm /tmp/pg.tar.bz2 \
 && cd /src/pg && ./configure --prefix=/opt/pg --without-readline --without-icu --without-zlib --with-openssl \
 && make -j"$(nproc)" && make install
RUN set -eu; git clone --filter=blob:none https://github.com/pgvector/pgvector /src/pgvector \
 && cd /src/pgvector && git checkout --detach "${PGVECTOR_COMMIT}" \
 && test "$(git rev-parse HEAD)" = "${PGVECTOR_COMMIT}" \
 && make PG_CONFIG=/opt/pg/bin/pg_config && make PG_CONFIG=/opt/pg/bin/pg_config install
RUN set -eu; git clone --filter=blob:none https://github.com/pgmq/pgmq /src/pgmq \
 && cd /src/pgmq && git checkout --detach "${PGMQ_COMMIT}" \
 && test "$(git rev-parse HEAD)" = "${PGMQ_COMMIT}" \
 && cd pgmq-extension && make PG_CONFIG=/opt/pg/bin/pg_config && make PG_CONFIG=/opt/pg/bin/pg_config install
# ---- end #1353 PostgreSQL build stage ----

# ---- build-time secret scan (the stages below the line "FROM worker AS scan" are reused by cloud/tests/test-cloud) ----
# The scan runs INSIDE the (remote, linux/amd64) build, where the files can be read: `docker save` on an arm64
# workstation cannot export an amd64-only image's layers. It reads the final filesystem of the `worker` stage as root;
# a hit (exit 1) or a file it cannot read (exit 2) fails the build, so nothing is published. A secret written in one
# layer and deleted in a later one is NOT visible here (the layers are already merged): no step of `worker` takes a
# credential, and a person who adds one must not write it to disk.
FROM worker AS scan
USER root
# The built PostgreSQL tree is only COPIED into `final`, so the scan stage takes the same copy: the scan below then reads /opt/pg too.
COPY --from=pgbuild /opt/pg /opt/pg
COPY worker/scan-secrets.sh worker/scan-files.py worker/scan-allowlist.txt /
RUN /scan-secrets.sh --dir / --skip proc --skip sys --skip dev --skip scan-secrets.sh --skip scan-files.py && echo ok > /scan-ok

# The shipped image is `worker` plus one 3-byte marker file, /usr/local/share/cw/scan-ok ("ok"), COPIED from `scan`. It is
# there only to make `final` depend on `scan`: the legacy builder (`az acr build`, ACR Tasks) builds stages in order and
# has no `RUN --mount`, so a plain COPY --from is what forces the scan to run first and fail the build on a hit. The
# marker is root-owned and world-readable (mode 0644); nothing executes or reads it. This file uses no BuildKit-only
# syntax (no --mount, heredoc, # syntax=, --link, --exclude): cloud/tests/test-cloud fails if one appears.
FROM worker AS final
COPY --from=scan /scan-ok /usr/local/share/cw/scan-ok
# #1353: PostgreSQL 16 and the two extensions, installed files only (root-owned, no compiler, nothing starts them): see the pgbuild stage.
COPY --from=pgbuild /opt/pg /opt/pg
# ask #2329: /opt/pg-bundle, the Debian-style tree that scripts/dev/ephemeral-pg.sh reads through PG_BUNDLE, built by COPIES (cp -a) of /opt/pg:
# no download, no build tool, no server started. The relocatable build resolves share beside bin, so /opt/pg/share/postgresql is also
# copied to usr/lib/postgresql/16/share/postgresql. Root-owned, read-only for others; the run user is unchanged (set again below).
USER root
RUN set -eu; b=/opt/pg-bundle; l="$b/usr/lib/postgresql/16"; e="$b/usr/share/postgresql/16/extension"; s=/opt/pg/share/postgresql \
 && mkdir -p "$l" "$e" \
 && cp -a /opt/pg/bin /opt/pg/lib "$l/" \
 && cp -a /opt/pg/share "$l/share" \
 && cp -a "$s"/extension/vector.control "$s"/extension/vector--*.sql "$s"/extension/pgmq.control "$s"/extension/pgmq--*.sql "$e/" \
 && cp -a "$s/postgresql.conf.sample" "$b/usr/share/postgresql/postgresql.conf.sample" \
 && chown -R root:root "$b" && chmod -R go-w,a+rX "$b"
ENV \
  PG_BUNDLE=/opt/pg-bundle
USER worker
