# ---------------------------------------------------------------- `disk prune`: reclaim old docker build cache and images, and nothing else
#
# The two commands below, and the volume step after them, are the ONLY ones it runs, with exactly these arguments: old build cache and old unused images (a
# week), then `docker volume prune -f` (never `-a`: docker's own anonymous-only prune, which removes only anonymous volumes no container references, stopped ones
# included) and ONLY when the docker server is 23.0 or newer (before that, `volume prune` removed every unused volume, named ones too). Never a running
# container (`image prune -a` keeps every image a container uses), never `system prune`. It refuses when there is no docker. `--dry-run` prints the commands and
# runs none. Nothing runs it on its own: the docs give a launchd snippet for a weekly run, not installed.
DISK_PRUNE_COMMANDS = (("docker", "builder", "prune", "-f", "--filter", "until=168h"),
                       ("docker", "image", "prune", "-a", "-f", "--filter", "until=168h"))
DISK_PRUNE_TIMEOUT = 600
DISK_VOLUME_PRUNE = ("volume", "prune", "-f")       # NEVER -a or --all
DISK_VOLUME_MIN_ENGINE = (23, 0)                    # from here `volume prune` removes only ANONYMOUS volumes
DISK_VOLUME_MIN_API = (1, 42)                       # the API of Docker 23.0
DISK_VOLUME_READ_BYTES = 1000000                    # docker output parsed: no more than this
DISK_ANONYMOUS_LABEL = "com.docker.volume.anonymous"
DISK_VOLUME_LIMIT = 200                             # volumes examined: past it the count is "at least"
DISK_VOLUME_SECONDS = 30
_SIZE_UNITS = {"B": 1, "KB": 10 ** 3, "MB": 10 ** 6, "GB": 10 ** 9, "TB": 10 ** 12}
_RECLAIMED = re.compile(r"Total reclaimed space:\s*(\S+)", re.I)


def _version_pair(text):
    found = re.match(r"\s*(\d{1,4})\.(\d{1,4})", text or "")
    return (int(found.group(1)), int(found.group(2))) if found else None


def docker_volume_prune_gate(docker, runner):
    """(ok, why, kind): is it safe to run `docker volume prune -f`? Only when ALL hold: the server is Docker (Engine or Desktop: a compatible program such as podman
    prunes every unused volume), its version is 23.0 or newer, the API both ends speak is 1.42 or newer (an older API makes the daemon prune EVERY unused
    volume, named ones too, whatever the server version), and `DOCKER_API_VERSION` does not pin it lower. Anything unreadable refuses."""
    try:
        done = runner([docker, "version", "--format", "{{.Server.Version}}|{{.Server.Platform.Name}}|{{.Server.APIVersion}}|{{.Client.APIVersion}}"],
                      capture_output=True, text=True, timeout=DISK_VOLUME_SECONDS, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False, "the docker server version could not be read (is docker running?)", "unreadable"
    parts = (done.stdout or "")[:2000].strip().split("|") if done.returncode == 0 else []
    if len(parts) != 4 or _version_pair(parts[0]) is None or _version_pair(parts[2]) is None or _version_pair(parts[3]) is None:
        return False, "the docker server version could not be read (is docker running?)", "unreadable"
    version, platform, server_api, client_api = _version_pair(parts[0]), parts[1], _version_pair(parts[2]), _version_pair(parts[3])
    if not platform.strip().lower().startswith("docker"):
        return False, "the server is %r, not Docker Engine or Docker Desktop (use Docker's own engine, or run the prune by hand)" % _plain(platform, 40), "unsafe"
    if version < DISK_VOLUME_MIN_ENGINE:
        return False, "docker server %d.%d is older than 23.0, where `volume prune` removes named volumes too (upgrade docker)" % version, "unsafe"
    pinned = os.environ.get("DOCKER_API_VERSION", "").strip()
    if pinned and (_version_pair(pinned) is None or _version_pair(pinned) < DISK_VOLUME_MIN_API):
        return False, "DOCKER_API_VERSION=%s pins the API below 1.42, where `volume prune` removes named volumes too (unset it)" % _plain(pinned, 20), "unsafe"
    if min(server_api, client_api) < DISK_VOLUME_MIN_API:
        return False, "the docker API in use is %d.%d, below 1.42, where `volume prune` removes named volumes too (upgrade the docker client or server)" % min(server_api, client_api), "unsafe"
    return True, "", ""


def _volume_bytes(text):
    found = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKMGT]?B)\s*", text or "")
    return int(float(found.group(1)) * _SIZE_UNITS[found.group(2).upper()]) if found else None


def anonymous_volumes(docker, runner):
    """(names, bytes or None, complete): the volumes docker lists as dangling (no container references them, stopped ones included) whose inspect shows the
    anonymous label. Names are checked against docker's own shape, so nothing hostile is printed. `bytes` is the sum of their sizes from `docker system df -v`,
    None when docker would not say; `complete` is False past DISK_VOLUME_LIMIT. Read only. Raises OSError when docker cannot answer."""
    def run(argv):
        done = runner([docker] + list(argv), capture_output=True, text=True, timeout=DISK_VOLUME_SECONDS, stdin=subprocess.DEVNULL)
        if done.returncode != 0:
            raise OSError("docker %s: exit %s: %s" % (" ".join(argv[:2]), done.returncode, _plain(done.stderr or done.stdout, 160)))
        return (done.stdout or "")[:DISK_VOLUME_READ_BYTES]
    listed = [l.strip() for l in run(("volume", "ls", "-q", "-f", "dangling=true")).splitlines() if l.strip()]
    ids = [l for l in listed if _DOCKER_ID["volume"].fullmatch(l)]
    complete = len(ids) <= DISK_VOLUME_LIMIT
    ids = ids[:DISK_VOLUME_LIMIT]
    found = [r.get("Name") for r in (_docker_inspect(docker, runner, "volume", ids) if ids else [])
             if isinstance(r.get("Labels"), dict) and DISK_ANONYMOUS_LABEL in r["Labels"] and isinstance(r.get("Name"), str)]
    names = [n for n in found if _DOCKER_ID["volume"].fullmatch(n)]
    size = None
    if names:
        try:
            rows = json.loads(run(("system", "df", "-v", "--format", "{{json .Volumes}}")) or "[]")
            sizes = {r.get("Name"): _volume_bytes(r.get("Size")) for r in rows if isinstance(r, dict)}
            size = sum(sizes[n] for n in names) if all(sizes.get(n) is not None for n in names) else None
        except (OSError, ValueError, subprocess.SubprocessError):
            size = None
    return names, size, complete


def anonymous_volumes_line(docker, runner):
    try:
        names, size, complete = anonymous_volumes(docker, runner)
    except (OSError, subprocess.SubprocessError) as err:
        return "unmounted anonymous volumes: not readable (%s)" % _plain(str(err), 120)
    return "unmounted anonymous volumes: %s%d (%s)" % ("" if complete else ">=", len(names), _human(size) if size is not None else "size not read")


def disk_volume_step(docker, runner, dry_run):
    """The third prune step. Returns (status, lines). Refused unless the server is 23.0 or newer."""
    ok, why, kind = docker_volume_prune_gate(docker, runner)
    if not ok:
        return (EXIT_ERROR if kind == "unreadable" else EXIT_OK), ["volume prune: refused: %s; %s" % (why, "the other two would still run" if dry_run else "the other two ran")]
    lines = []
    if dry_run:
        try:
            names, size, complete = anonymous_volumes(docker, runner)
            lines.append("candidate anonymous volumes: %s%d (%s)" % ("" if complete else ">=", len(names), _human(size) if size is not None else "size not read"))
            lines.extend("  " + n for n in names[:20])
            if len(names) > 20:
                lines.append("  %d more" % (len(names) - 20))
        except (OSError, subprocess.SubprocessError) as err:
            lines.append("candidate anonymous volumes: not readable (%s)" % _plain(str(err), 120))
        lines.append("would run %s (never -a)" % " ".join(["docker"] + list(DISK_VOLUME_PRUNE)))
        return EXIT_OK, lines
    try:
        done = runner([docker] + list(DISK_VOLUME_PRUNE), capture_output=True, text=True, timeout=DISK_PRUNE_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as err:
        return EXIT_ERROR, ["volume prune: failed: %s" % _plain(str(err), 160)]
    if done.returncode != 0:
        return EXIT_ERROR, ["volume prune: failed (exit %s): %s" % (done.returncode, _plain(done.stderr or done.stdout, 200))]
    found = _RECLAIMED.search(done.stdout or "")
    return EXIT_OK, ["volume prune: reclaimed %s" % (_plain(found.group(1), 40) if found else "nothing reported")]


def cmd_disk_reap_docker(args, runner, now):
    """`disk reap-docker [--apply]`: list (the default) or remove the expired labelled docker objects."""
    docker = _docker_executable(args.docker)
    if docker is None:
        sys.stderr.write("%s: disk reap-docker: %s: nothing was run\n" % (PROG, _docker_message(args.docker)))
        return EXIT_USAGE
    apply = bool(args.apply)
    if apply and not load_config().get("docker_reaper", True):
        sys.stderr.write("%s: disk reap-docker: docker_reaper is false in the config (the kill switch): nothing was removed\n" % PROG)
        return EXIT_USAGE
    audit = AuditLog(ensure_state_dir() / "audit.jsonl") if apply else None
    result = docker_reap(docker, runner, now(), apply, audit, "operator")
    print("%s: %d expired labelled object(s)%s" % ("removed" if apply else "dry run (nothing removed; --apply removes)", len(result["removed"]), ""))
    for kind, ident, owner, expires in result["removed"]:
        print("  %s %s  owner %s  expired %s" % (kind, _plain(ident, 80), _plain(owner, 60), iso(expires)))
    for kind, ident, owner, expires, why in result["kept"]:
        if why != "not expired":
            print("  kept %s %s: %s" % (kind, _plain(ident, 80), why))
    for error in result["errors"]:
        print("  error: %s" % error)
    return EXIT_ERROR if result["errors"] else EXIT_OK


cmd_disk_reap_docker.direct = True


def cmd_disk_prune(args, runner, now):
    """`disk prune [--dry-run]`."""
    docker = _docker_executable(args.docker)
    if docker is None:
        sys.stderr.write("%s: disk prune: %s: nothing was run\n" % (PROG, _docker_message(args.docker)))
        return EXIT_USAGE
    commands = [(docker,) + argv[1:] for argv in DISK_PRUNE_COMMANDS]
    if args.dry_run:
        print("dry run: %s would run, in order, and nothing else (never `-a` on a volume, a running container or `system prune`):" % PROG)
        for argv in commands:
            print("  " + " ".join(shlex.quote(a) for a in argv))
        volume_status, volume_lines = disk_volume_step(docker, runner, True)
        for line in volume_lines:
            print(line)
        return volume_status
    before = disk_free(ensure_state_dir())
    status = EXIT_OK
    for argv in commands:
        try:
            done = runner(list(argv), capture_output=True, text=True, timeout=DISK_PRUNE_TIMEOUT, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as err:
            print("%s: failed: %s" % (" ".join(argv[1:3]), _plain(str(err), 160)))
            status = EXIT_ERROR
            continue
        found = _RECLAIMED.search(done.stdout or "")
        if done.returncode != 0:
            print("%s: failed (exit %s): %s" % (" ".join(argv[1:3]), done.returncode, _plain(done.stderr or done.stdout, 200)))
            status = EXIT_ERROR
        else:
            print("%s: reclaimed %s" % (" ".join(argv[1:3]), _plain(found.group(1), 40) if found else "nothing reported"))
    volume_status, volume_lines = disk_volume_step(docker, runner, False)
    for line in volume_lines:
        print(line)
    status = max(status, volume_status)
    after = disk_free(ensure_state_dir())
    if before and after:
        print("disk: %d GB free before, %d GB after" % (before[0] / 1e9, after[0] / 1e9))
    return status
