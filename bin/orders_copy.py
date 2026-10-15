"""`orders copy`: a charter's standing orders copied to another charter, word for word (ask #347, slice 3).

When an ask moves lanes, the new lane's charter needs the sibling's orders. Done by hand that was one `orders add` at a time, each
re-dated, and one add printed no result. This copies every order bullet of the source charter's `## Standing orders` section to the
target with its original wording and date (the bullet is carried verbatim, `(ordered by <name>, <date>)` included), skips one the
target already holds (so running it again copies only what is missing), writes and reads back one order at a time with the existing
orders writer, and ALWAYS prints a result: one line per order, one line per failure, one summary line. A failed order is named and the
rest are still copied; any failure exits 1. Only the Chief of Staff copies (the rule of `orders add`); a refusal is audited. Nothing in
the source charter is changed. The legacy one-line `Standing authority:` is not a bullet: it is not copied, and the output says so.

The logic is here; `bin/fleet-switchboard` registers the subcommand and hands this module a namespace `sb` of its own helpers (`NEEDS`).
"""

import re

SENTINEL = "orders-copy-sentinel-7f3a91"
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
SUFFIX_ALLOWANCE = 90   # room for the `(ordered by <name up to 60>, <date>)` the writer adds beyond ORDER_MAX_CHARS of text
SENTINEL_BY, SENTINEL_DATE = "x", "2000-01-01"

NEEDS = ("PROG", "EXIT_OK", "EXIT_ERROR", "EXIT_USAGE", "OrdersError", "IntentError", "NoticeError", "section_orders", "add_order_to_body",
         "edit_charter_orders", "ORDER_MAX_CHARS", "_charter_body", "_legacy_authority", "record_order_notice", "comment_as", "_one_line", "issue_tag")


def insert_bullet(sb, body, bullet):
    """`body` with `bullet` (an order's whole text, as `section_orders` gives it) added to the section, verbatim. The placement, the
    new section and the limit of 20 are the existing writer's: it writes a placeholder order, which is then replaced by `bullet`."""
    marked = sb.add_order_to_body(body, SENTINEL, SENTINEL_BY, SENTINEL_DATE)
    line = "- %s (ordered by %s, %s)" % (SENTINEL, SENTINEL_BY, SENTINEL_DATE)
    return marked.replace(line, "- " + bullet, 1)


def shown(sb, bullet):
    """An order's text as one safe line for output: it came from an issue body (outside text), so control characters never reach a terminal."""
    return sb._one_line(CONTROL.sub(" ", bullet), 300)


def unfit(sb, bullet):
    """Why this bullet is not copied, or None. The writer limits what `orders add` takes; a bullet read from outside text gets the same limits
    and no control characters, and is refused, never reworded."""
    if CONTROL.search(bullet):
        return "it contains control characters"
    if len(bullet) > sb.ORDER_MAX_CHARS + SUFFIX_ALLOWANCE:
        return "it is %d characters, over the limit of an order" % len(bullet)
    return None


def run_copy(sb, repo, source, target, caller, role, out, err):
    """Copy `repo#source`'s orders to `repo#target`. Exit code: 0 every order is there; 1 an order failed to copy (named); 2 refused
    (not the Chief of Staff, or a charter that cannot be used): nothing changed."""
    engine = sb.engine
    reader = engine.intents
    subject = "charter:%s#%d" % (repo, target)
    if role != "chief-of-staff":
        engine.audit.append(engine.actor, "orders.refused", subject,
                            {"caller": caller, "role": role, "action": "copy", "repo": repo, "source": source})
        err("%s: only the chief-of-staff copies standing orders; %s is %s" % (
            sb.PROG, caller, "a %s" % role if role else "not given a role"))
        return sb.EXIT_USAGE
    if source == target:
        err("%s: orders copy: the source and the target are the same charter; nothing was changed" % sb.PROG)
        return sb.EXIT_USAGE
    try:
        source_body = sb._charter_body(reader, repo, source)
        target_body = sb._charter_body(reader, repo, target)
    except (sb.OrdersError, sb.IntentError) as problem:
        err("%s: orders copy: %s; nothing was changed" % (sb.PROG, sb._one_line(str(problem), 300)))
        return sb.EXIT_USAGE
    orders = sb.section_orders(source_body)
    held = {}
    for text in sb.section_orders(target_body):
        held[text] = held.get(text, 0) + 1
    legacy = sb._legacy_authority(source_body)
    if legacy:
        out("note: charter #%d has a legacy `Standing authority:` line, which is not an order bullet and is NOT copied: add it by hand" % source)
    copied, skipped, failed = 0, 0, []
    for number, bullet in enumerate(orders, 1):
        if held.get(bullet, 0) > 0:
            held[bullet] -= 1
            skipped += 1
            out("order %d/%d already on #%d: %s" % (number, len(orders), target, shown(sb, bullet)))
            continue
        why = unfit(sb, bullet)
        if why:
            failed.append(number)
            out("order %d/%d FAILED: %s (not copied: %s)" % (number, len(orders), shown(sb, bullet), why))
            continue
        before = sb.section_orders(sb._charter_body(reader, repo, target) if copied or failed else target_body).count(bullet)
        detail = {"caller": caller, "source": source, "number": number, "text": shown(sb, bullet)}
        try:
            sb.edit_charter_orders(engine, repo, target, "orders.copy", detail,
                                   lambda body, b=bullet: insert_bullet(sb, body, b),
                                   lambda body, b=bullet, n=before: sb.section_orders(body).count(b) > n)
        except (sb.OrdersError, sb.IntentError) as problem:
            failed.append(number)
            out("order %d/%d FAILED: %s (%s)" % (number, len(orders), shown(sb, bullet), sb._one_line(str(problem), 200)))
            continue
        copied += 1
        out("order %d/%d copied to #%d: %s" % (number, len(orders), target, shown(sb, bullet)))
        notify(sb, engine, repo, target, caller, bullet)
    out("orders copy %s#%d -> #%d: %d copied, %d already there, %d failed (of %d)" % (repo, source, target, copied, skipped, len(failed), len(orders)))
    if copied:
        summary = "Standing orders copied from charter #%d by `%s` (chief-of-staff), %d of %d, wording and dates unchanged. If the owner did not give them, remove them." % (
            source, caller, copied, len(orders))
        try:
            sb.comment_as(engine, getattr(engine, "gh_runner", None) or reader.runner, repo, target, summary, caller)
        except sb.IntentError as problem:
            err("%s: the orders stand, but the comment on the charter failed (%s): tell the owner what was copied" % (sb.PROG, sb._one_line(str(problem), 200)))
            return sb.EXIT_ERROR
    if failed:
        err("%s: orders copy: %d of %d orders did not copy (%s); the rest did. Run it again to copy only what is missing" % (
            sb.PROG, len(failed), len(orders), ", ".join(str(n) for n in failed)))
        return sb.EXIT_ERROR
    return sb.EXIT_OK


def notify(sb, engine, repo, target, caller, bullet):
    """The owner's heads-up for a copied order, as for an added one (a burst groups in the panel). Best effort: the order stands either way."""
    try:
        if engine.state_dir is not None:
            sb.record_order_notice(engine.state_dir, repo, target, "add", shown(sb, bullet), caller, None, engine.now())
    except (OSError, sb.NoticeError):
        pass
