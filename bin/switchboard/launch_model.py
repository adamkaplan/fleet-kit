# ---- #1642: `launch --model PROVIDER/MODEL --reasoning LEVEL`. The choice is checked against ONE read of the LOCAL service's model list before any session
# exists (no call to a model provider); the level is the model's own variant. The choice is a dict of the record keys it adds (`model`, `reasoning`): empty
# when nothing was asked, so a launch with neither option is what it was.

LAUNCH_MODEL_RE = re.compile(r"[A-Za-z0-9._:+/-]{1,120}")
LAUNCH_LEVEL_RE = re.compile(r"[a-z0-9_-]{1,24}")
LAUNCH_MODEL_LIST_MAX = 2000   # models read from the list; the reply itself is already bounded by the transport


def launch_model_syntax(model, level):
    """The two validated texts as a choice dict, or a LaunchError. The raw text is never echoed (it may hold anything)."""
    if model is not None:
        provider, _, name = model.partition("/") if isinstance(model, str) else ("", "", "")
        if not (isinstance(model, str) and LAUNCH_MODEL_RE.fullmatch(model) and provider and name):
            raise LaunchError("--model must be PROVIDER/MODEL (at most 120 letters, digits and ._-:+/): pass it as shown by the local service's model list")
    if level is not None and not (isinstance(level, str) and LAUNCH_LEVEL_RE.fullmatch(level)):
        raise LaunchError("--reasoning must be one of the model's own levels (at most 24 lower-case letters, digits, _ and -): pass one the model lists")
    choice = {}
    if model is not None:
        choice["model"] = model
    if level is not None:
        choice["reasoning"] = level
    return choice


def launch_model_choice(args, engine):
    """The choice of this launch ({} when none was asked). Refused, before any session or pane, with one line naming the fix: a resume, a level without
    a model, a malformed value, a model or level the local service does not list, a list that cannot be read. ONE read of the local list, only when asked."""
    model, level = getattr(args, "model", None), getattr(args, "reasoning", None)
    if model is None and level is None:
        return {}
    if args.resume:
        raise LaunchError("--model and --reasoning cannot be used with --resume: a resume keeps its session's model; drop the option")
    if model is None:
        raise LaunchError("--reasoning needs --model PROVIDER/MODEL: the level is one of that model's own levels; add --model or drop --reasoning")
    choice = launch_model_syntax(model, level)
    try:
        listed = engine.v2.model_list()
        found = [m for m in listed[:LAUNCH_MODEL_LIST_MAX] if isinstance(m, dict)] if isinstance(listed, list) else None
    except Exception as err:
        found, why = None, _one_line(str(err), 100)
    else:
        why = "the service's answer was not a model list"
    if found is None:
        raise LaunchError("--model %s could not be verified: the local OpenCode service's model list could not be read (%s); start the service, or launch without --model" % (model, why))
    provider, _, name = model.partition("/")
    entry = next((m for m in found if m.get("providerID") == provider and m.get("id") == name), None)
    if entry is None:
        raise LaunchError("--model %s: the local service lists no such model; use a listed PROVIDER/MODEL (a model added after OpenCode started is listed after the service reloads)" % model)
    if level is not None:
        levels = sorted({v.get("id") for v in entry.get("variants") or [] if isinstance(v, dict) and isinstance(v.get("id"), str)})
        if level not in levels:
            raise LaunchError("--reasoning %s: %s lists %s; pick one of them%s" % (level, model, ", ".join(levels) or "no levels", "" if levels else " or drop --reasoning"))
    return choice


def launch_model_kwargs(choice):
    """The extra argument of `session_create`: nothing when no model was chosen, so the call is the one it was; else the model body, the level as its variant."""
    if not choice:
        return {}
    provider, _, name = choice["model"].partition("/")
    body = {"providerID": provider, "id": name}
    if choice.get("reasoning"):
        body["variant"] = choice["reasoning"]
    return {"model": body}


def launch_model_note(fleet):
    """The text a worker's status row gains: its model and level, or nothing."""
    fleet = fleet if isinstance(fleet, dict) else {}
    model, level = fleet.get("model"), fleet.get("reasoning")
    return "  model %s%s" % (model, " reasoning %s" % level if level else "") if isinstance(model, str) else ""


def launch_model_cloud_refusal(args):
    """Until the cloud route carries the options: a cloud launch given one is refused, never silently run on another model."""
    if getattr(args, "cloud", False) and (getattr(args, "model", None) or getattr(args, "reasoning", None)):
        raise LaunchError("--model and --reasoning are not carried by --cloud yet: launch locally, or drop them")
