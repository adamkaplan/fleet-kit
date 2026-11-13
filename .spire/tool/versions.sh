#!/usr/bin/env bash
# versions.sh: source it, then `spire_versions ROOT`. The shell and EVERY
# command it starts from then on run with the project's pinned tool versions
# first in PATH (versions.mjs: .node-version, .nvmrc, mise.toml, .tool-versions,
# package.json engines), never the host's defaults. It prints one startup line
# on stderr naming the versions, and returns 1 with a plain reason (which tool,
# which pin, what is installed, how to install it) when a pin has no match: the
# caller exits, nothing runs.
# Run it before anything else of the entry: the node that reads versions.mjs is
# only the host's, and it runs nothing of the project's.
spire_versions() {
  local out
  out=$(node "${BASH_SOURCE[0]%/*}/versions.mjs" env "${1:?spire_versions ROOT}") || return 1
  eval "$out"
}
