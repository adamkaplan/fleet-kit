#!/usr/bin/env python3
"""Write the `permissions:` list of the Chief of Staff and its subagent (agents/opencode/{chief-of-staff,cos-subagent}.md).

The rules keep the Chief of Staff FOCUSED on its job (read and coordinate across the fleet's repos, the switchboard, GitHub and Azure
status); they are not a security boundary against it. Allowed = serves the job. Denied = nothing in the job justifies it. Nothing asks:
the base rule is `* * deny` (OpenCode v2: last match wins, any deny denies), so every guard is a deny and an exception cannot be
carved out of a deny: a shape that needs an exception is simply not matched by the guards.

  gen_permissions.py [--check]     rewrite both files' frontmatter (or, with --check, exit 1 when they differ)
"""
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENTS = os.path.join(REPO, "agents", "opencode")
TEMP = "/private/var/folders/*/T/opencode"  # the opencode temp dir: the Chief of Staff's own message files


def build(subagent=False):
    rules = []
    allow = lambda action, res: rules.append((action, res, "allow"))
    deny = lambda action, res: rules.append((action, res, "deny"))
    rules.append(("*", "*", "deny"))
    for tool in ("read", "glob", "grep", "skill", "question", "todoread", "todowrite"):
        allow(tool, "*")
    allow("external_directory", TEMP + "*")
    allow("external_directory", "/Users/*/Code/*")  # the fleet's repos
    for config in ("/Users/*/.config/fleet-switchboard*", "/Users/*/.local/state/fleet-switchboard*", "/Users/*/.config/opencode*"):
        allow("external_directory", config)  # readable (config, standing orders, audit, installed definitions); nothing writes there
    allow("edit", TEMP + "/*")  # the only place the Chief of Staff writes
    for nouns in ("send", "report", "remind", "intent", "intents", "status", "pending", "whoami", "version", "notice", "decisions", "orders", "launch"):
        allow("shell", "fleet-switchboard %s" % nouns)
        allow("shell", "fleet-switchboard %s *" % nouns)
    if not subagent:
        allow("shell", "fleet-switchboard handoff *")
    for shape in ("pr view", "pr list", "pr checks", "pr diff", "pr status", "pr comment", "pr close", "pr reopen", "pr merge", "pr edit", "pr ready",
                  "issue view", "issue list", "issue status", "issue comment", "issue close", "issue reopen", "issue edit", "run view", "run list",
                  "workflow view", "workflow list", "repo view", "repo list", "search", "api repos/"):
        allow("shell", "gh %s*" % shape if shape.endswith("/") else "gh %s *" % shape)
    for noun in ("workspace", "tab", "pane", "agent"):  # herdr: look around (list, get, read), never type into or control another agent's pane
        for verb in ("list", "get", "read"):
            allow("shell", "herdr %s %s" % (noun, verb))
            allow("shell", "herdr %s %s *" % (noun, verb))
    for shape in ("api snapshot", "workspace rename *", "tab rename *", "workspace focus *", "tab focus *", "pane current", "pane layout *", "pane process-info *"):
        allow("shell", "herdr " + shape)
    for command in ("git log", "git show", "git status", "git diff", "git rev-parse", "git grep", "git branch", "git blame", "git ls-files"):
        allow("shell", command + " *")
    readers = ("ls", "cat", "head", "tail", "wc", "grep", "cut", "sort", "uniq", "tr", "jq", "echo", "printf")
    for command in readers:
        allow("shell", command + " *")
    for command in ("pwd", "uptime", "date", "sleep *"):
        allow("shell", command)
    allow("shell", "date *")
    allow("shell", "cat > %s/*" % TEMP)
    allow("shell", "cat >> %s/*" % TEMP)
    for wrapped in ("fleet-switchboard", "gh", "az", "git"):
        for digits in ("?", "??", "???"):  # `timeout 5|90|300 <command>`
            allow("shell", "timeout %s %s *" % (digits, wrapped))
    groups = ["account", "group", "resource", "vm", "vmss", "disk", "snapshot", "image", "network vnet", "network nsg", "network public-ip", "network nic",
              "network lb", "network application-gateway", "network dns zone", "webapp", "functionapp", "aks", "acr", "sql server", "sql db",
              "storage account", "storage container", "monitor metrics", "monitor activity-log", "monitor log-analytics workspace", "role assignment",
              "role definition", "identity", "cognitiveservices account", "postgres flexible-server", "mysql flexible-server", "cosmosdb", "redis",
              "servicebus namespace", "eventhub namespace", "provider", "policy assignment", "tag", "lock", "deployment group", "deployment sub",
              "cdn profile", "apim", "staticwebapp", "appservice plan", "batch account",
              "k8s-extension", "ad signed-in-user", "ad sp", "ad app", "ad user", "ad group"]
    for group in groups:
        for verb in ("show", "list"):
            allow("shell", "az %s %s *" % (group, verb))
    for shape in ("graph query *", "monitor log-analytics query *", "webapp log tail *", "account show", "account list"):
        allow("shell", "az " + shape)

    # ---- guards: nothing in the job justifies these. Each is a deny; an anchored copy covers the `timeout N` wrapper.
    anchored = ("fleet-switchboard", "gh", "az", "git", "herdr")
    def guard(pattern, action="shell"):
        deny(action, pattern)
        if action == "shell" and pattern.split(" ")[0] in anchored:
            deny(action, "timeout * " + pattern)
    # other people's work and forbidden gh: merge bypass, approvals, repo/secret/release/auth, workflow runs, API writes
    for flag in ("--admin", "--auto", "--bypass", "--merge-queue"):
        guard("gh *%s*" % flag)
    for pattern in ("gh pr review*", "gh *--approve*", "gh repo create*", "gh repo delete*", "gh repo edit*", "gh repo fork*", "gh repo clone*", "gh repo rename*",
                    "gh repo archive*", "gh repo sync*", "gh repo deploy-key*", "gh secret*", "gh variable*", "gh workflow run*", "gh workflow enable*",
                    "gh workflow disable*", "gh release*", "gh auth*", "gh extension*", "gh alias*", "gh pr checkout*", "gh run rerun*", "gh run cancel*",
                    "gh run delete*", "gh run download*", "gh api * -X*", "gh api *--method*", "gh api * -f*", "gh api * -F*", "gh api *--field*",
                    "gh api *--raw-field*", "gh api *--input*", "gh api *--hostname*", "gh api graphql*", "gh api *--paginate --slurp*",
                    "gh * -R*/*/*", "gh * --repo*/*/*", "gh * --body-file*", "gh * -F *", "gh *--output*", "gh *-q env*", "gh *--jq env*", "gh *-qenv*", "gh *--jq=env*"):
        guard(pattern)
    for pattern in ("git push*", "git commit*", "git add*", "git checkout*", "git switch*", "git reset*", "git clone*", "git fetch*", "git pull*", "git merge*",
                    "git rebase*", "git stash*", "git tag*", "git config*", "git remote*", "git worktree*", "git * --output*", "git * --no-index*", "git *--ext-diff*",
                    "git *--textconv*", "git grep *-O*", "git grep *--open-files-in-pager*", "git -c *", "git -C *"):
        guard(pattern)
    # az: writes, and secret VALUES
    for verb in ("create", "delete", "update", "set", "invoke", "deploy", "start", "stop", "restart", "add", "remove", "purge", "import", "restore", "scale",
                 "swap", "login", "logout", "run-command", "up", "exec", "assign", "grant", "revoke", "regenerate", "rotate", "upload", "copy", "enable",
                 "disable", "apply", "patch", "deallocate", "redeploy", "resize", "open-port", "cancel", "approve", "reject", "move", "export"):
        guard("az * %s" % verb)
        guard("az * %s *" % verb)
    for word in ("secret", "key", "token", "password", "credential", "connection-string", "connectionstring", "appsettings", "extension", "sas", "ssh",
                 "identity-token", "access-token", "Secret", "Key", "Token", "Password", "Credential", "ConnectionString", "AdminPassword"):
        guard("az *%s*" % word)
    for pattern in ("az rest*", "az login*", "az account get-access-token*", "az * --debug*", "az containerapp*"):
        guard(pattern)
    # the switchboard: the Chief of Staff does not raise orchestrators' batches, nor launches itself or its delegate
    for pattern in ("fleet-switchboard decisions batch*", "fleet-switchboard launch *--agent chief-of-staff*", "fleet-switchboard launch *--agent cos-subagent*"):
        guard(pattern)
    for pattern in ("herdr agent prompt*", "herdr agent start*", "herdr agent stop*", "herdr pane send*", "herdr pane input*", "herdr pane close*", "herdr pane split*",
                    "herdr * close*", "herdr * stop*", "herdr * start*", "herdr * send-keys*", "herdr server*", "herdr update*", "herdr config*"):
        guard(pattern)
    if subagent:
        guard("fleet-switchboard launch*")
    # reading outside the job: other folders of the laptop, credentials, the switchboard's own config
    places = ("/etc", "/private/etc", "/root", "/var/root", "/.ssh", "/.aws", "/.azure", "/.gnupg", "/.config/gh", "/.config/git", "/.config/gcloud", "/.kube",
              "/.docker", "/.netrc", "/.npmrc", "/.zsh_history", "/.bash_history", "/.local/share", "/Documents", "/Desktop", "/Downloads", "/Pictures", "/Movies",
              "/Music", "/Library", ".env", "id_rsa", "id_ed25519", ".pem", ".p12", "auth.json", "credentials", "secrets")
    for command in ("head", "tail", "grep", "wc", "ls", "cut", "sort", "uniq", "tr", "jq", "git show", "git diff", "git grep", "git log"):
        for place in places:
            guard("%s *%s*" % (command, place))
    for place in places:  # `cat`: only when its first argument is a path, so the text of a `cat > file <<EOF` message is never matched
        for first in ("/*", "~*", "$*", ""):
            guard("cat %s%s*" % (first, place))
        guard("cat %s*" % place.lstrip("/"))
    guard("jq *env*")
    printable = "".join(chr(i) for i in range(33, 127) if chr(i) not in "*?")  # `*` and `?` are the glob characters themselves: they cannot be named

    def redirects(command):
        """Deny every redirect except the stream merge `2>&1` (`>&1` and nothing after it): `>file`, `>>file`, `&>file`, `>&file`.
        A glob cannot say "anything but", so the character after `>` (and after `>&1`) is enumerated."""
        guard("%s *&>*" % command)
        guard("%s *>" % command)
        for ch in printable:
            if ch != "&":
                guard("%s *>%s*" % (command, ch))
            elif True:
                for after in printable.replace("1", ""):
                    guard("%s *>&%s*" % (command, after))
        guard("%s *> *" % command)
        for after in printable:
            guard("%s *>&1%s*" % (command, after))

    # writes: a redirect to anywhere but the message temp dir (the cat > temp form above is the one allowed redirect)
    for command in ("ls", "grep", "head", "tail", "wc", "cut", "sort", "uniq", "tr", "jq", "echo", "printf", "date", "git", "gh pr view", "gh pr list",
                    "gh issue view", "gh issue list", "gh api", "az", "fleet-switchboard status", "fleet-switchboard pending", "fleet-switchboard decisions", "timeout"):
        redirects(command)
    guard("cat <*")
    for target in ("~", "$", ".", "/Users", "/home", "/etc", "/usr", "/bin", "/opt", "/Library", "/System", "/tmp", "/root", "/var/folders", "/private/etc", '"', "'") + tuple("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"):
        guard("cat >%s*" % target if False else "cat > %s*" % target)
        guard("cat >> %s*" % target)
        guard("cat >%s*" % target)
    for tool in ("read", "grep", "glob"):  # the file tools: the same places
        for place in ("*/.ssh*", "*/.aws*", "*/.azure*", "*/.gnupg*", "*/.config/gh*", "*/.config/git/*", "*/.config/gcloud*", "*/.local/share/*", "*/.kube*", "*/.docker*", "*/.netrc", "*/.npmrc", "*.env", "*.env.*",
                      "*/Documents/*", "*/Desktop/*", "*/Downloads/*", "*/Library/*", "*id_rsa*", "*id_ed25519*", "*.pem", "*.p12", "*auth.json", "*credentials*",
                      "*secrets*"):
            deny(tool, place)
    for tool in ("subagent", "task"):
        deny(tool, "*")
    return rules


def render(rules):
    seen, out = set(), []
    for rule in rules:
        if rule not in seen:
            seen.add(rule)
            out.append(rule)
    return "permissions:\n" + "".join("  - action: %s\n    resource: %s\n    effect: %s\n" % (json.dumps(a), json.dumps(r), e) for a, r, e in out)


def rewrite(path, subagent):
    text = open(path).read()
    front, body = text[4:].split("\n---\n", 1)
    head = front.split("permissions:")[0] if "permissions:" in front else front
    head = re.sub(r"(?m)^name:.*\n", "", head)
    head = re.sub(r"(?m)^permission:\n(?:[ ]+.*\n)*", "", head + "\n").rstrip("\n") + "\n"
    new = "---\n" + head.rstrip("\n") + "\n" + render(build(subagent)) + "---\n" + body
    return text, new


def main():
    check = "--check" in sys.argv
    bad = False
    for name, sub in (("chief-of-staff.md", False), ("cos-subagent.md", True)):
        path = os.path.join(AGENTS, name)
        old, new = rewrite(path, sub)
        if check and old != new:
            bad = True
            print("%s differs from the generator" % name)
        elif not check:
            open(path, "w").write(new)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
