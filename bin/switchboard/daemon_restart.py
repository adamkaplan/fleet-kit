def cmd_restart(args, runner, spawn=subprocess.Popen, kill=os.kill, sleep=time.sleep):
    """Stop the daemon by its recorded pid, then `ensure`. It signals only a process that is a switchboard daemon."""
    state = ensure_state_dir()
    report = daemon_report(state)
    if report["running"]:
        pid = report["pid"]
        if pid is None:
            sys.stderr.write("%s: restart refused: the lock says a daemon runs but not which pid\n" % PROG)
            return EXIT_ERROR
        if daemon_pid_refusal(pid):
            sys.stderr.write("%s: restart refused: %s; nothing was signalled\n" % (PROG, daemon_pid_refusal(pid)))
            return EXIT_ERROR
        ours, command = daemon_process_is_ours(runner, pid)   # ONE check of "is this pid our `run`", shared with `daemon stop`
        if not ours:
            sys.stderr.write("%s: restart refused: pid %d is not a %s daemon (it is: %s); nothing was signalled\n"
                             % (PROG, pid, PROG, _one_line(command, 80) or "not running or unreadable"))
            return EXIT_ERROR
        try:
            kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as err:
            sys.stderr.write("%s: could not signal pid %d: %s\n" % (PROG, pid, err.strerror or err))
            return EXIT_ERROR
        waited = 0.0
        while run_lock_held(state):
            if waited >= RESTART_PATIENCE:
                sys.stderr.write("%s: pid %d did not stop within %ds; it was asked to, and nothing stronger was "
                                 "sent\n" % (PROG, pid, RESTART_PATIENCE))
                return EXIT_ERROR
            sleep(0.1)
            waited += 0.1
    print("daemon %s" % ensure_daemon(state, spawn))
    return EXIT_OK
