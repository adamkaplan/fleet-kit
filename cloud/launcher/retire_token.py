"""retire_token: the fresh repository token the launcher hands to the end hook at retire time (never stored, never printed).

`configured(env)` says whether this launcher can mint at all (the mint command or the vault/App settings are present); a
launcher with none behaves as before the change. The mint itself is cloud_worker.run_git_token_cmd, the ONE mint call the launch and the renewal use; this module holds
only the settings test, the step names and the recovery text.
"""
MINT_SETTINGS = ("CW_GIT_TOKEN_CMD", "CW_VAULT_NAME", "CW_APP_ID_CMD")

# The step names a retire failure message uses, and the read-only route out of a kept pod (docs: three files carry it).
STEP_MINT, STEP_PUSH = "mint", "push"
RECOVERY = ("the pod and its work are kept; copy the work out read-only by running "
            "`git -C /work/repo bundle create - --all` in the pod (git bundle; the docs' retire-path section has the full route), "
            "then `retire` again or `delete --force` once it is safe")


def configured(env):
    return any((env.get(k) or "").strip() for k in MINT_SETTINGS)
