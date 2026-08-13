#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# ///
# ruff: noqa: UP007, UP045
"""
Destructive-operation and Gmail-send matcher for the gws block-destructive hook.

Reads a Claude Code Bash tool JSON payload from stdin.
Prints "ALLOW" when the command is safe.
Prints one of the DENY_* verdicts plus context when a gws command is blocked.
Exits non-zero only on unrecoverable errors so the hook can fail closed.

Python 3.9.6 compatibility: this script must run on the system Python that
ships with macOS Xcode Command Line Tools (3.9.6). Keep ``from __future__
import annotations`` at the top so PEP 585 / PEP 604 hints stay stringified,
and avoid 3.10+ syntax (``match`` statements, runtime ``X | Y`` unions,
``except*``). The CI guard lives in ``tests/plugins/gws/test_python39_compat.py``.
Smoke check locally::

    uv run --python 3.9 python -c "import ast; ast.parse(open('plugins/gws/scripts/check-destructive.py').read())"
"""

from __future__ import annotations

import json
import re
import sys

# stdlib (email.utils.getaddresses): no pip install needed, available since Python 3.0
from email.utils import getaddresses
from typing import Any, Optional

# Shared shell-parsing layer. Imported by path (both hooks run
# ``python3 <scripts>/check-destructive.py``, so the scripts dir is on sys.path).
from _gws_shell import (
    COMMAND_SEPARATORS,
    COMMAND_SUBSTITUTION_MARKER,
    COMMAND_SUBSTITUTION_MARKER_UNQUOTED,
    FALSE_FLAG_VALUES,
    TRUE_FLAG_VALUES,
    VALUE_TAKING_FLAGS,
    ShellParseError,
    is_gmail_service,
    iter_gws_invocations,
    substitution_wildcards,
)

UPSTART_DOMAIN = "upstart.com"
DESTRUCTIVE_VERBS = {"delete", "trash", "remove", "clear"}

# Destructive roots matched against the camelCase / ``-`` / ``_`` SEGMENTS of a
# method name, not just the whole token. Exact-set matching against
# DESTRUCTIVE_VERBS missed every compound Google API method:
# ``drive files emptyTrash`` permanently deletes all trashed files (no arguments,
# unrecoverable), ``gmail users messages batchDelete`` deletes messages, and
# ``sheets spreadsheets values batchClear`` / ``batchClearByDataFilter`` wipe
# ranges. These need no shell trickery at all — ``gws --help`` prints them and
# this plugin's own skills document them, so a well-behaved agent reaches them.
DESTRUCTIVE_METHOD_ROOTS = {"delete", "trash", "remove", "clear", "purge", "empty"}
# Methods whose names contain a destructive root but RESTORE data; never blocked.
RESTORATIVE_METHODS = {"untrash", "undelete", "unhide", "unarchive"}
# Methods that REPLACE a whole resource, destroying whatever was there, without
# any destructive-sounding word in the name. Apps Script
# ``projects.updateContent`` and the ``script +push`` helper both overwrite every
# file in a project, so an omitted file is deleted.
WHOLESALE_REPLACEMENT_METHODS = {"updatecontent"}
# ``+push`` is destructive only in its HELPER form. A bare word ``push`` is an
# ordinary value (a cell value, a branch name), so matching it unconditionally
# denied things like `sheets +append --values push`.
WHOLESALE_REPLACEMENT_HELPERS = {"push"}
# Split on non-alphanumerics and camelCase humps (``emptyTrash`` -> empty, Trash;
# ``batchClearByDataFilter`` -> batch, Clear, By, Data, Filter).
_METHOD_SEGMENT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# Request-body keys that perform a destructive write even though the METHOD name
# is benign. ``batchUpdate`` is precisely how gws-sheets/docs/slides/forms skills
# tell Claude to write, so only the body reveals a ``deleteSheet`` intent. Kept
# narrower than DESTRUCTIVE_METHOD_ROOTS on purpose: ``remove``/``clear`` would
# also match benign bodies such as ``removeLabelIds`` (un-labelling a message).
BODY_DESTRUCTIVE_ROOTS = {"delete", "trash", "purge", "empty"}
# Body keys that contain a destructive root but destroy nothing. Verified by
# sweeping every query parameter of all 424 methods in the real CLI's command
# tree: ``includeSpamTrash`` is the ONLY benign collision in the whole API
# surface (a read-only Gmail list filter). The ``delete*`` entries are Docs/Sheets
# requests that remove FORMATTING, not content, and are common in normal use.
BODY_BENIGN_KEYS = {
    "includespamtrash",
    "deleteparagraphbullets",
    "deleteconditionalformatrule",
    "deletebanding",
    "deletefilterview",
}
# Applying either label is a trash/spam move, i.e. destructive in effect.
TRASHING_LABEL_IDS = {"TRASH", "SPAM"}
# Keys whose enum VALUE (not name) carries the destructive intent.
VALUE_ENUM_KEYS = {"disposition", "expungeBehavior"}
# Flags whose value is a JSON request body.
BODY_FLAGS = ("--json", "--params")
GMAIL_SEND_HELPERS = {"+send", "+forward", "+reply", "+reply-all"}
# Only helpers whose recipients are fully specified by command-line flags can be statically
# inspected for @upstart.com membership. `+reply` and `+reply-all` always inherit recipients
# from the original Gmail thread (which the hook cannot read), and their `--to/--cc/--bcc`
# flags ADD recipients on top of the inherited set — so even if every literal flag value is
# @upstart.com, the inherited thread participants may still be external. Non-draft replies
# are therefore permanently blocked; engineers should use `--draft` and review/send from the
# Gmail UI, or prefix with `!` at the Claude Code prompt to bypass the hook intentionally.
INSPECTABLE_RECIPIENT_HELPERS = {"+send", "+forward"}
RECIPIENT_FLAGS = {"--to", "--cc", "--bcc"}
# The canonical boolean value sets are imported from _gws_shell so this hook
# and the tokenizer cannot disagree about pflag/Cobra boolean semantics.


def _flag_state(args: list[str], flag: str) -> tuple[bool, Optional[str]]:
    """Scan ALL args (pflag-style) and report ``flag``'s LAST-occurrence state.

    pflag/Cobra (gws's CLI library) uses last-occurrence-wins: when a flag is
    repeated, only the final occurrence determines its value. So
    ``--draft --draft=false`` is NOT a draft (the trailing ``=false`` wins) and
    ``--draft=false --draft`` IS a draft (the trailing bare flag wins). This
    function therefore walks every token and keeps the most recent state instead
    of returning on the first match -- returning on the first occurrence let an
    external, non-draft send slip through.

    A bare ``--flag`` (or ``--flag=<truthy>``) counts as enabled only when it
    appears in *flag position* -- i.e. it is not being consumed as the value
    for a preceding value-taking flag such as ``--body`` or ``--subject``.
    Without this guard, a payload like
    ``gws gmail +send --to ext@example.com --body --draft ...`` would let the
    literal token ``--draft`` (which is actually the value of ``--body``)
    enable draft mode and bypass the external-recipient check.

    An explicit false value (``--draft=false``) behaves exactly like omitting
    the flag (pflag semantics), so an all-internal send is not over-blocked;
    only genuinely unsupported values (``--draft=maybe``) produce an error so
    ambiguous input still fails closed.

    Returns ``(False, None)`` when ``flag`` never appears in flag position.
    """
    state: tuple[bool, Optional[str]] = (False, None)
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            # pflag end-of-options marker: every later token is positional, so
            # a trailing `-- --draft` must not be read as enabling draft mode
            # (the real CLI would not treat it as a flag — fail-open otherwise).
            break
        # If this token is itself a value-taking flag, the *next* token is its
        # value and must be skipped, not interpreted as a flag.
        if arg in VALUE_TAKING_FLAGS:
            index += 2
            continue
        if arg == flag:
            state = (True, None)
        elif arg.startswith(f"{flag}="):
            value = arg.split("=", 1)[1].strip().lower()
            if value in TRUE_FLAG_VALUES:
                state = (True, None)
            elif value in FALSE_FLAG_VALUES:
                state = (False, None)
            else:
                state = (False, f"{flag} has unsupported value: {value or '<empty>'}")
        index += 1
    return state


def _is_recipient_flag_token(token: str) -> bool:
    """Return True when ``token`` is a recipient flag in bare or inline form.

    Matches ``--to``/``--cc``/``--bcc`` and their inline ``--to=``/``--cc=``/
    ``--bcc=`` variants. Used so a generic value-taking flag never consumes a
    recipient flag as its value (which would skip external-recipient
    validation).
    """
    return any(token == flag or token.startswith(f"{flag}=") for flag in RECIPIENT_FLAGS)


def _collect_recipient_values(args: list[str]) -> tuple[list[str], list[str]]:
    """Walk args sequentially (pflag-style) and collect recipient flag values.

    Each value-taking flag in :data:`VALUE_TAKING_FLAGS` consumes the next
    token so the value is not mistakenly treated as a fresh flag in a later
    scan. Recipient flag values that are missing or flag-shaped are recorded
    as ``missing`` so the caller fails closed. Flag-shaped tokens remain
    available for the next iteration because pflag treats them as the next
    flag.

    A recipient flag whose value-slot is missing (end of args) or whose
    inline ``--to=`` value is empty/flag-shaped is recorded as ``missing`` so
    the caller can fail closed.
    """
    values: list[str] = []
    missing: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            # pflag end-of-options marker: later --to/--cc/--bcc tokens are
            # positionals to the real CLI, not recipient flags. Stop collecting
            # so post-`--` "recipients" never count as verified literals.
            break
        # Handle inline ``--flag=value`` form for recipient flags first.
        handled_recipient = False
        for flag in RECIPIENT_FLAGS:
            if arg == flag:
                if index + 1 >= len(args) or args[index + 1].startswith("--"):
                    missing.append(f"{flag} is missing a value")
                    index += 1
                else:
                    values.append(args[index + 1])
                    index += 2
                handled_recipient = True
                break
            if arg.startswith(f"{flag}="):
                value = arg.split("=", 1)[1]
                if not value or value.startswith("--"):
                    missing.append(f"{flag} is missing a value")
                else:
                    values.append(value)
                index += 1
                handled_recipient = True
                break
        if handled_recipient:
            continue

        # Any other value-taking flag consumes the next token so we do not
        # re-scan that value as a flag (e.g. ``--body --to ...``).
        if arg in VALUE_TAKING_FLAGS:
            # Defense-in-depth: never let a generic value-taking flag swallow a
            # recipient flag as its value, or the recipient would skip external
            # validation (e.g. ``--subject --cc ext@example.com``). Advance by 1 so
            # the recipient flag is processed on the next iteration. Non-recipient
            # tokens (incl. ``--draft``/``--dry-run``) are still consumed as values,
            # preserving the ``--body --draft`` anti-bypass.
            if index + 1 < len(args) and _is_recipient_flag_token(args[index + 1]):
                index += 1
                continue
            index += 2
            continue

        index += 1
    return values, missing


def _parse_recipients(values: list[str]) -> tuple[list[str], list[str]]:
    emails = []
    unknown = []
    for value in values:
        if not value.strip():
            continue
        parsed = getaddresses([value])
        if not parsed:
            unknown.append(value)
            continue
        for display_name, address in parsed:
            address = address.strip()
            if not address or "@" not in address:
                unknown.append(display_name or value)
                continue
            emails.append(address)
    return emails, unknown


def _non_upstart_recipients(emails: list[str]) -> list[str]:
    external = []
    for email in emails:
        domain = email.rsplit("@", 1)[1].lower()
        if domain != UPSTART_DOMAIN:
            external.append(email)
    return sorted(set(external), key=str.lower)


def _inspect_gmail_send_helper(args: list[str], helper_index: int, command: str) -> tuple[str, ...]:
    helper = args[helper_index].lower()
    draft_enabled, draft_error = _flag_state(args, "--draft")
    dry_run_enabled, dry_run_error = _flag_state(args, "--dry-run")
    flag_errors = [error for error in (draft_error, dry_run_error) if error]
    if flag_errors:
        return ("DENY_EMAIL_UNVERIFIED", command, *flag_errors)
    if draft_enabled or dry_run_enabled:
        return ("ALLOW",)
    if helper not in INSPECTABLE_RECIPIENT_HELPERS:
        return (
            "DENY_EMAIL_UNVERIFIED",
            command,
            f"gws gmail {args[helper_index]} infers recipients from an existing message",
        )

    values, missing = _collect_recipient_values(args)
    emails, unknown = _parse_recipients(values)
    external = _non_upstart_recipients(emails)
    if external:
        return ("DENY_EMAIL_EXTERNAL", command, *external)
    if missing or unknown or not emails:
        details = [*missing, *unknown]
        if not emails:
            details.append("no literal recipients found in --to/--cc/--bcc")
        return ("DENY_EMAIL_UNVERIFIED", command, *details)
    return ("ALLOW",)


def _command_path_tokens(args: list[str]) -> list[str]:
    """Collect positional path tokens (service/resource/method) with pflag awareness.

    Only KNOWN value-taking flags (:data:`VALUE_TAKING_FLAGS`) consume the next
    token as their value. Boolean/unknown flags (e.g. ``--verbose``, ``--html``)
    do NOT swallow the following token, because for a boolean flag the next token
    is a positional argument -- often the destructive method itself
    (``--verbose delete``) or a raw ``send`` method. Treating every flag as
    value-taking would let such a positional escape the destructive-verb and
    raw-send scans, wrongly ALLOWing the command. Scanning stops at the first
    COMMAND_SEPARATOR. This mirrors the pflag parsing used elsewhere in the file.
    """
    path = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in COMMAND_SEPARATORS:
            break
        if arg == "--":
            # pflag end-of-options marker: every later token (up to a command
            # separator) is a positional, even when it starts with `-`. Keep
            # scanning them as path tokens so `gws drive files -- delete` still
            # surfaces its destructive method (fail-safe direction).
            for rest in args[index + 1 :]:
                if rest in COMMAND_SEPARATORS:
                    break
                path.append(rest)
            break
        if arg in VALUE_TAKING_FLAGS:
            # Bare value-taking flag (inline ``--flag=value`` forms never appear
            # in VALUE_TAKING_FLAGS, so membership already implies no ``=``): the
            # next token is its value (not a positional), so skip both -- unless
            # the next token is a command separator, in which case stop here.
            if index + 1 < len(args) and args[index + 1] not in COMMAND_SEPARATORS:
                index += 2
                continue
            index += 1
            continue
        if arg.startswith("-"):
            # Inline ``--flag=value`` or a boolean/unknown flag: skip only the
            # flag. Do NOT consume the following positional token.
            index += 1
            continue
        path.append(arg)
        index += 1
    return path


def _method_segments(name: str) -> set:
    """Return the lowercased camelCase / punctuation-delimited segments of ``name``."""
    return {segment.lower() for segment in _METHOD_SEGMENT.split(name) if segment}


def _is_destructive_method(token: str) -> bool:
    """True when a positional method token destroys data.

    Matches on SEGMENTS so compound API names (``emptyTrash``, ``batchDelete``,
    ``batchClearByDataFilter``) are caught, not only the bare verbs. Restorative
    methods (``untrash``, ``undelete``) contain a destructive root but add data
    back, so they are excluded by whole-name match first.
    """
    raw = token.strip()
    name = raw.lstrip("+")
    if not name or name.lower() in RESTORATIVE_METHODS:
        return False
    if name.lower() in WHOLESALE_REPLACEMENT_HELPERS:
        return raw.startswith("+")
    # Google API method names are bare camelCase identifiers (`emptyTrash`,
    # `batchDelete`) — never punctuated. A token carrying punctuation is a
    # resource path, id, or file name, so segment matching must not apply to it:
    # otherwise a folder called `delete-me-folder` reads as a destructive method.
    if not name.isalnum():
        return False
    if name.lower() in WHOLESALE_REPLACEMENT_METHODS:
        return True
    return bool(_method_segments(name) & DESTRUCTIVE_METHOD_ROOTS)


def _body_values(args: list[str]) -> list[str]:
    """Collect the values of ``--json`` / ``--params`` (bare and inline forms)."""
    values: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            break
        handled = False
        for flag in BODY_FLAGS:
            if arg == flag:
                next_is_body_flag = index + 1 < len(args) and any(
                    args[index + 1] == candidate or args[index + 1].startswith(f"{candidate}=")
                    for candidate in BODY_FLAGS
                )
                if index + 1 < len(args) and not next_is_body_flag:
                    values.append(args[index + 1])
                    index += 2
                else:
                    index += 1
                handled = True
                break
            if arg.startswith(f"{flag}="):
                values.append(arg.split("=", 1)[1])
                index += 1
                handled = True
                break
        if handled:
            continue
        if arg in VALUE_TAKING_FLAGS:
            next_is_body_flag = index + 1 < len(args) and any(
                args[index + 1] == flag or args[index + 1].startswith(f"{flag}=") for flag in BODY_FLAGS
            )
            index += 1 if next_is_body_flag else 2
            continue
        index += 1
    return values


def _destructive_update_request(key: str, request: dict) -> Optional[str]:
    """Flag ``update``-named requests whose PARAMETERS make them destructive.

    Google's write APIs express "wipe this" as an update, so the request name
    alone is not enough:

    * ``updateCells`` with a ``fields`` mask of ``*`` and no ``rows`` clears the
      whole range (with ``rows`` it is an ordinary write).
    * ``replaceAllText`` with an empty ``replaceText`` deletes every occurrence of
      the matched text (with a replacement it is an ordinary edit).
    """
    # An UpdateCellsRequest with no `rows` clears whatever `fields` names over the
    # whole range — ANY mask, not just `*` (`fields: userEnteredValue` wipes every
    # value and leaves only formatting). No rows always means a clear, so testing
    # the mask was both unnecessary and a bypass.
    # "No rows" must be judged on whether any row actually carries CELL DATA, not
    # on the list being non-empty: ``rows: [{}]`` and ``rows: [{"values": []}]``
    # are truthy but supply zero cells, so the range is still cleared.
    if key == "updateCells":
        rows = request.get("rows")
        carries_data = isinstance(rows, list) and any(isinstance(row, dict) and row.get("values") for row in rows)
        if not carries_data:
            return "request body clears a cell range (updateCells with no cell data)"
    # A whitespace-only replacement shreds the text just as thoroughly as an
    # empty one, so compare on the stripped value.
    if key == "replaceAllText" and not str(request.get("replaceText", "")).strip():
        return "request body deletes matched text (replaceAllText with a blank replaceText)"
    return None


def _walk_body_for_destruction(node: Any) -> Optional[str]:
    """Recursively look for a destructive intent inside a parsed request body."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str):
                if key.lower() not in BODY_BENIGN_KEYS and _method_segments(key) & BODY_DESTRUCTIVE_ROOTS:
                    return f"request body performs a destructive write: {key}"
                # `{"trashed": true}` on drive.files.update trashes the file.
                if key == "trashed" and value not in (False, None, 0, "false", "0", ""):
                    return "request body sets trashed=true"
                # Applying the TRASH/SPAM label is a trash move.
                if key == "addLabelIds" and isinstance(value, list):
                    applied = {str(item).upper() for item in value}
                    hit = applied & TRASHING_LABEL_IDS
                    if hit:
                        return f"request body applies the {'/'.join(sorted(hit))} label"
                # Some settings encode the destruction in an enum VALUE under a
                # benign key name, so the key scan alone cannot see it. A sweep of
                # every cached discovery document found exactly these keys:
                # AutoForwarding/PopSettings ``disposition`` ("trash") and
                # ImapSettings ``expungeBehavior`` ("trash", "deleteForever" —
                # irreversible, and with autoExpunge every IMAP delete becomes a
                # permanent one). Match on the value's segments so a future enum
                # member of the same shape is caught too.
                if key in VALUE_ENUM_KEYS and _method_segments(str(value)) & BODY_DESTRUCTIVE_ROOTS:
                    return f"request body sets {key}={value}"
                # Requests that destroy content while being named like an update.
                if isinstance(value, dict):
                    reason = _destructive_update_request(key, value)
                    if reason:
                        return reason
            found = _walk_body_for_destruction(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _walk_body_for_destruction(item)
            if found:
                return found
    return None


def _destructive_body(value: str) -> Optional[str]:
    """Return a reason when a ``--json`` / ``--params`` body is a destructive write.

    A body that will not parse is deliberately NOT denied: the real CLI validates
    it first and refuses the call outright (``Invalid --json body: trailing comma
    ...``, HTTP 400, nothing sent), so an unparseable body cannot hide a
    destructive intent — it only stops the command from doing anything. Denying it
    turned an ordinary JSON typo in a read-only command into a confusing
    "no-delete policy" violation. Non-JSON-shaped values are not request bodies.
    """
    text = value.strip()
    if not text.startswith("{") and not text.startswith("["):
        return None
    try:
        body = json.loads(text)
    except ValueError:
        return None
    return _walk_body_for_destruction(body)


def _display_substitution(token: str) -> str:
    """Render a marker-bearing token readably in deny details."""
    return token.replace(COMMAND_SUBSTITUTION_MARKER_UNQUOTED, "$(...)").replace(
        COMMAND_SUBSTITUTION_MARKER, '"$(...)"'
    )


def _inspect_gws_args(args: list[str], command: str) -> tuple[str, ...]:
    if not args:
        return ("ALLOW",)

    # Command substitutions whose expansion could alter the argument stream
    # (inject flags/recipients, or BE the verb/method) are unverifiable — fail
    # closed before any other inspection. Quoted substitutions consumed as flag
    # values are fine and are validated by the downstream checks instead.
    wildcards = substitution_wildcards(args)
    if wildcards:
        details: list[str] = []
        for token in wildcards:
            display = _display_substitution(token)
            if display not in details:
                details.append(display)
        return ("DENY_SUBSTITUTION", command, *details)

    # Locate the `gmail` service token in POSITIONAL position (pflag-aware):
    # skip value-taking flags and their values, and inline `--flag=value` /
    # other flags, so a flag VALUE that equals `gmail` (e.g. `--query gmail`)
    # is not misread as the Gmail service. Without this, a non-Gmail command
    # like `gws admin-reports activities list --query gmail ... send` would
    # spuriously DENY_EMAIL_RAW on its later positional `send`.
    gmail_index = None
    scan = 0
    saw_end_of_options = False
    while scan < len(args):
        arg = args[scan]
        if not saw_end_of_options:
            if arg == "--":
                saw_end_of_options = True
                scan += 1
                continue
            if arg in VALUE_TAKING_FLAGS:
                scan += 2
                continue
            if arg.startswith("-") and len(arg) > 1:
                scan += 1
                continue
        if is_gmail_service(arg):
            gmail_index = scan
            break
        scan += 1

    if gmail_index is not None:
        # Only treat a token as a send helper when it is in POSITIONAL position
        # (not a flag, not the value of a value-taking flag). This mirrors
        # check-domain.py's `_allows_external_gmail_draft`, so a flag VALUE like
        # ``--query +send`` is never misread as a helper. Walk args after `gmail`
        # with pflag awareness and stop at a command separator.
        index = gmail_index + 1
        end_of_options = False
        while index < len(args):
            arg = args[index]
            if arg in COMMAND_SEPARATORS:
                break
            if not end_of_options:
                if arg == "--":
                    # pflag end-of-options marker: everything after is
                    # positional, so keep scanning for helpers but stop
                    # treating `-`-prefixed tokens as flags.
                    end_of_options = True
                    index += 1
                    continue
                if arg in VALUE_TAKING_FLAGS:
                    # Skip the flag and its value token.
                    index += 2
                    continue
                if arg.startswith("-"):
                    # Inline ``--flag=value`` or any other flag: skip the flag only.
                    index += 1
                    continue
            # Positional token.
            if arg.lower() in GMAIL_SEND_HELPERS:
                return _inspect_gmail_send_helper(args, index, command)
            index += 1

        # Raw Gmail send methods hide recipients in MIME/JSON payloads, so this
        # hook cannot prove every recipient is an Upstart address. Only inspect
        # positional path tokens after `gmail` (which skip value-taking flags
        # and their values and stop at command separators) so a flag VALUE like
        # `--query send` is not misread as the `send` method.
        method_tokens = [token.lower().lstrip("+") for token in _command_path_tokens(args[gmail_index + 1 :])]
        if "send" in method_tokens:
            return ("DENY_EMAIL_RAW", command)

    for token in _command_path_tokens(args):
        if _is_destructive_method(token):
            return ("DENY_DESTRUCTIVE", command)

    # A benign-looking method can still carry a destructive request body
    # (`batchUpdate` with a `deleteSheet` request, `update` with `trashed:true`).
    # The method-name scan above cannot see that — only the body can.
    for value in _body_values(args):
        reason = _destructive_body(value)
        if reason:
            return ("DENY_DESTRUCTIVE", command, reason)

    return ("ALLOW",)


def _inspect_command(command: str) -> tuple[str, ...]:
    """Return the first non-ALLOW verdict across every gws invocation in ``command``.

    Delegates tokenization and recursion (substitutions, ``bash -c``, ``eval``)
    to :func:`iter_gws_invocations`; a :class:`ShellParseError` fails closed as
    an ``ERROR`` verdict, which the hook surfaces as a deny.
    """
    try:
        for args in iter_gws_invocations(command):
            result = _inspect_gws_args(args, command)
            if result[0] != "ALLOW":
                return result
    except ShellParseError as exc:
        return ("ERROR", str(exc))
    return ("ALLOW",)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        cmd = payload.get("tool_input", {}).get("command", "")
    except Exception as exc:
        print(f"failed to parse Bash tool payload: {exc}", file=sys.stderr)
        sys.exit(1)

    if not cmd:
        print("ALLOW")
        return

    result = _inspect_command(cmd)
    verdict = result[0]
    if verdict in ("ALLOW", "ERROR"):
        print(verdict)
        for line in result[1:]:
            print(line)
        return

    # DENY_* verdicts carry (verdict, command, *details). The command may span
    # multiple lines (a multiline Bash payload), while every detail line is a
    # single line. Emit the detail-line count on line 2 so the hook can split
    # the (variable-length) details from the multiline command unambiguously --
    # a positional split would otherwise interleave command and detail lines in
    # the rendered deny message. Layout:
    #   line 1        verdict
    #   line 2        <k> = number of detail lines
    #   lines 3..2+k  detail lines
    #   lines 3+k..   the command (verbatim, possibly multiline)
    command = result[1] if len(result) > 1 else ""
    details = result[2:]
    print(verdict)
    print(len(details))
    for detail in details:
        print(detail)
    print(command)


if __name__ == "__main__":
    main()
