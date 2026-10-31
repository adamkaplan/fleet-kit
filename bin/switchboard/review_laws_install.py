# ---------------------------------------------------------------- `review-laws install` (#179, slice L1)

REVIEW_LAWS_EDIT = {
    "REVIEW_LAWS.md": "the verdict format, the matrix cells you tighten (never loosen), and the floor paths your repository names",
    "CODING_STANDARDS.md": "add the rules your repository's own code needs; remove none without saying why",
    "SECURITY_STANDARDS.md": "add the rules for your repository's own identities, secrets and permissions",
    "TEST_STANDARDS.md": "add how your repository's tests are run in CI and what they must cover",
}


def review_laws_source():
    return Path(os.path.realpath(__file__)).parent.parent / "docs" / "review-laws"


def cmd_review_laws(args, runner, now):
    """`review-laws install <repo path>`: copy the generic starter set (REVIEW_LAWS.md, CODING_STANDARDS.md, SECURITY_STANDARDS.md,
    TEST_STANDARDS.md, each opening with `extends: fleet-kit-review-laws @ review-laws-<n>`) to the root of a repository and say what to edit. It
    writes only those files, never overwrites one the repository already has (`--force` replaces it), and touches nothing else (not
    AGENTS.md: the laws must not contradict it, so the pointer line is the repository's own edit)."""
    source = review_laws_source()
    try:
        version = (source / "VERSION").read_text().strip()
        texts = {name: (source / name).read_text() for name in REVIEW_LAWS_FILES}
    except OSError as err:
        sys.stderr.write("%s: review-laws: the starter set is not readable (%s): %s\n" % (PROG, source, getattr(err, "strerror", None) or err))
        return EXIT_USAGE
    target = Path(args.repo).expanduser()
    if not target.is_dir():
        sys.stderr.write("%s: review-laws: %s is not a directory\n" % (PROG, args.repo))
        return EXIT_USAGE
    header = "extends: fleet-kit-review-laws @ %s" % version
    bad = [n for n, text in texts.items() if text.splitlines()[0] != header]
    if bad:
        sys.stderr.write("%s: review-laws: the starter set is inconsistent: %s do not open with %r\n" % (PROG, ", ".join(bad), header))
        return EXIT_USAGE
    links = [n for n in REVIEW_LAWS_FILES if os.path.islink(str(target / n))]   # a link (even a dangling one) is never replaced, forced or not
    if links:
        sys.stderr.write("%s: review-laws: %s is a symbolic link in %s: nothing was written (a link could send the copy somewhere else; remove it yourself)\n"
                         % (PROG, ", ".join(links), target))
        return EXIT_USAGE
    existing = [n for n in REVIEW_LAWS_FILES if os.path.lexists(str(target / n))]
    if existing and not args.force:
        sys.stderr.write("%s: review-laws: %s already has %s: nothing was written (the repository's own copies are its law; --force replaces them)\n"
                         % (PROG, target, ", ".join(existing)))
        return EXIT_USAGE
    written = []
    for name in REVIEW_LAWS_FILES:
        temp = target / (".%s.%d.tmp" % (name, os.getpid()))
        try:
            temp.write_text(texts[name])
            os.replace(str(temp), str(target / name))
        except OSError as err:
            try:
                temp.unlink()
            except OSError:
                pass
            sys.stderr.write("%s: review-laws: could not write %s: %s (written so far: %s)\n"
                             % (PROG, name, getattr(err, "strerror", None) or err, ", ".join(written) or "none"))
            return EXIT_ERROR
        written.append(name)
    lines = ["review laws (%s) installed in %s. What to edit:" % (version, target)] + ["  %s: %s" % (n, REVIEW_LAWS_EDIT[n]) for n in written]
    lines.append("Next: keep the `extends:` line (it says which version you started from); make sure the laws do not contradict your AGENTS.md "
                 "(leave a pointer line there for any hygiene rule you moved); reviewers read these files from the base branch of a pull request.")
    if (target / "AGENTS.md").exists():
        lines.append("Your repository has an AGENTS.md: it was not touched.")
    print("\n".join(lines))
    return EXIT_OK


cmd_review_laws.direct = True   # no error mapping, no daemon start
