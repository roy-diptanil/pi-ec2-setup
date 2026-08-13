#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# ///
# ruff: noqa: UP007, UP045
# This script must run on Python 3.9.6 as that's what is guaranteed on
# Upstart laptops. This makes Optional[...] required over PEP 604 (X | None).
"""
Domain validator for the gws check-domain PreToolUse hook.

Reads a Claude Code Bash tool JSON payload from stdin.
Scans actual gws invocations for user-identifier arguments (--userId=, userId=,
or bare email-looking tokens). If any email address uses a domain other than
upstart.com, prints "DENY\n<offending_address>" and exits. External recipient
addresses are allowed for Gmail draft/dry-run helpers because no message is
delivered by Claude. Prints "ALLOW" when all identifiers are @upstart.com or
the literal "me", and "ERROR" when the shell payload cannot be inspected.

The Google People API identifier format is an email address or "me".
This check is best-effort defence-in-depth; it does not replace OAuth
scope enforcement or the Workspace-layer controls (ITENG-694).

Python 3.9.6 compatibility: this script must run on the system Python that
ships with macOS Xcode Command Line Tools (3.9.6). Keep ``from __future__
import annotations`` at the top so PEP 585 / PEP 604 hints stay stringified,
and avoid 3.10+ syntax (``match`` statements, runtime ``X | Y`` unions,
``except*``). The CI guard lives in ``tests/plugins/gws/test_python39_compat.py``.
Smoke check locally::

    uv run --python 3.9 python -c "import ast; ast.parse(open('plugins/gws/scripts/check-domain.py').read())"
"""

from __future__ import annotations

import json
import re
import sys
from typing import Optional

# Shared shell-parsing layer. Imported by path (both hooks run
# ``python3 <scripts>/check-domain.py``, so the scripts dir is on sys.path).
from _gws_shell import (
    COMMAND_SUBSTITUTION_MARKER,
    COMMAND_SUBSTITUTION_MARKER_UNQUOTED,
    TRUE_FLAG_VALUES,
    VALUE_TAKING_FLAGS,
    ShellParseError,
    is_gmail_service,
    iter_gws_invocations,
    substitution_wildcards,
)

ALLOWED_DOMAIN = "upstart.com"

# email.utils.parseaddr() handles header-format addresses (display names, angle brackets) but not
# raw shell command strings — regex is the correct tool for this input.

# Matches --userId=value, --user-id=value, userId=value, userId: value
USERID_PARAM = re.compile(
    r"""
    (?:--?user[-_]?id   # --userId / --user-id / -userId
    |"userId"           # JSON key "userId"
    |userId)            # bare key
    [=:\s]+             # separator
    ["']?               # optional opening quote on the value (e.g. JSON "userId": "addr")
    ([^\s,;'">\]]+)     # capture a single address-like token (quotes/delimiters end it)
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Matches any bare token that looks like an email address
# The local part is a SPECIALS-EXCLUDING class, not an alphanumeric whitelist:
# RFC-5322 atext allows ``! = ~ '`` etc., quoted local parts exist, and
# SMTPUTF8/EAI allows non-ASCII (``延伸@evil.com``). A narrow class produced NO
# match for those, and since this regex is both the JSON-leaf scan and the
# raw-text fallback, an external address slipped past both. Excluding ``:`` and
# whitespace keeps ``from:alice@upstart.com is:unread`` resolving to
# ``upstart.com``, and requiring a dotted domain still ignores a bare ``@alice``.
BARE_EMAIL = re.compile(r"[^\s<>,;:\"()\[\]@\\]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")
# An ``@`` preceded by a non-space and followed by a dotted token that the regex
# above could NOT parse (an IP literal, a bracketed domain, a non-ASCII domain)
# is an address this hook cannot verify — fail closed instead of falling through.
UNVERIFIABLE_AT = re.compile(r"\S@[^\s@]*\.[^\s@]")

GMAIL_DRAFT_HELPERS = {"+send", "+forward", "+reply", "+reply-all"}
GMAIL_MESSAGE_CONTENT_FLAGS = frozenset({"--subject", "--body"})
# These generic payload flags can carry user identifiers inside JSON. Even a
# quoted substitution keeps their runtime value opaque to the domain hook, so
# unlike ordinary value flags it must fail closed.
DOMAIN_BEARING_VALUE_FLAGS = frozenset({"--json", "--params"})
BODY_FLAGS = ("--json", "--params")


def _parse_pflags(args: list[str]) -> tuple[dict[str, Optional[str]], list[str]]:
    """Parse args in pflag style.

    Returns (flags, positionals) where:
      - flags maps each flag name (e.g. "--draft") to its value or None for
        bare boolean flags. Only tokens that appear in flag position are
        recorded; tokens consumed as values for preceding value-taking flags
        are excluded.
      - positionals is the ordered list of tokens that appear in positional
        position (i.e. neither flag nor value-of-flag).
    """
    flags: dict[str, Optional[str]] = {}
    positionals: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            # pflag end-of-options marker: every later token is positional, so
            # a trailing `-- --draft` must not be read as enabling draft mode.
            positionals.extend(args[i + 1 :])
            break
        if arg.startswith("-") and len(arg) > 1:
            if "=" in arg:
                name, value = arg.split("=", 1)
                flags[name] = value
                i += 1
                continue
            if arg in VALUE_TAKING_FLAGS:
                # Consume next token as the value, even if it looks like a flag.
                if i + 1 < len(args):
                    flags[arg] = args[i + 1]
                    i += 2
                else:
                    flags[arg] = None
                    i += 1
                continue
            # Bare boolean flag.
            flags[arg] = None
            i += 1
            continue
        positionals.append(arg)
        i += 1
    return flags, positionals


def _flag_enabled(flags: dict[str, Optional[str]], flag: str) -> bool:
    if flag not in flags:
        return False
    value = flags[flag]
    if value is None:
        return True
    return value.strip().lower() in TRUE_FLAG_VALUES


def _gmail_helper(positionals: list[str]) -> Optional[str]:
    gmail_index = None
    for index, token in enumerate(positionals):
        if is_gmail_service(token):
            gmail_index = index
            break
    if gmail_index is None:
        return None
    # Only the first positional token after the gmail service can be a helper.
    # Later path tokens like `gws gmail messages list +send` are not helpers.
    helper_index = gmail_index + 1
    if helper_index >= len(positionals):
        return None
    helper = positionals[helper_index].lower()
    return helper if helper in GMAIL_DRAFT_HELPERS else None


def _allows_external_gmail_draft(args: list[str]) -> bool:
    # Invariant: _inspect_gws_args fails closed on substitution_wildcards()
    # before calling this helper, so by the time we are asked about the draft
    # exemption no substitution can cancel the draft flag.
    flags, positionals = _parse_pflags(args)
    return _gmail_helper(positionals) is not None and (
        _flag_enabled(flags, "--draft") or _flag_enabled(flags, "--dry-run")
    )


def _bare_address_scan_args(args: list[str]) -> list[str]:
    """Remove Gmail helper message content from the bare-address scan.

    Subject and body text may legitimately mention external addresses. Strip
    only those flags' values, and only for recognized Gmail helpers; recipient,
    JSON, generic command, and end-of-options arguments remain inspectable.
    """
    _, positionals = _parse_pflags(args)
    if _gmail_helper(positionals) is None:
        return args

    filtered: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            filtered.extend(args[index:])
            break
        if arg.startswith("-") and len(arg) > 1:
            if "=" in arg:
                name, _ = arg.split("=", 1)
                if name not in GMAIL_MESSAGE_CONTENT_FLAGS:
                    filtered.append(arg)
                index += 1
                continue
            if arg in VALUE_TAKING_FLAGS:
                value = args[index + 1] if index + 1 < len(args) else None
                if arg not in GMAIL_MESSAGE_CONTENT_FLAGS:
                    filtered.append(arg)
                    if value is not None:
                        filtered.append(value)
                index += 2 if value is not None else 1
                continue
        filtered.append(arg)
        index += 1
    return filtered


def _bad_address(value: str) -> Optional[str]:
    """Return the address if it uses a non-Upstart domain, else None."""
    value = value.strip("'\"")
    if COMMAND_SUBSTITUTION_MARKER in value or COMMAND_SUBSTITUTION_MARKER_UNQUOTED in value:
        # The identifier was assembled by a $(...)/backtick command
        # substitution, so its runtime value is invisible to this hook (the
        # substitution body is inspected separately, but it need not mention
        # gws). Fail closed rather than allow an unverifiable identifier.
        return "unverifiable user identifier built from a command substitution"
    if value.lower() in ("me", ""):
        return None
    m = re.search(r"@(.+)$", value)
    if m and m.group(1).lower() != ALLOWED_DOMAIN:
        return value
    return None


def _bad_address_in_text(text: str, context: str) -> Optional[str]:
    """Return an external or unverifiable address-shaped token in ``text``."""
    residue: list[str] = []
    cursor = 0
    for match in BARE_EMAIL.finditer(text):
        if match.group(1).lower() != ALLOWED_DOMAIN:
            return match.group(0)
        residue.append(text[cursor : match.start()])
        cursor = match.end()
    residue.append(text[cursor:])
    if any(UNVERIFIABLE_AT.search(part) for part in residue):
        return f"unverifiable address in {context}: {text[:60]}"
    return None


def _walk_json_addresses(node: object) -> Optional[str]:
    """Check every string leaf of a parsed request body for a non-Upstart address."""
    if isinstance(node, dict):
        for value in node.values():
            found = _walk_json_addresses(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _walk_json_addresses(item)
            if found:
                return found
    elif isinstance(node, str):
        # Extract address-shaped tokens rather than judging the whole string.
        # BARE_EMAIL ignores free-text @mentions; the residue check catches
        # dotted address forms whose domains cannot be verified.
        return _bad_address_in_text(node, "request body")
    return None


def _bad_json_body(value: str) -> Optional[str]:
    """Return an offending address inside a ``--json`` / ``--params`` body.

    The raw-text scans below only see the literal characters, so a JSON escape
    hides the address from them entirely: ``"ext\\u0040evil\\u002ecom"`` contains
    no ``@``, yet gws decodes it and Google delivers to ``ext@evil.com`` (and the
    same trick retargets ``userId`` to another mailbox, which is exactly what
    this hook exists to prevent). Parsing the body and judging its decoded string
    leaves closes that hole.

    A body that will not parse is NOT denied here: the real CLI validates it and
    refuses the call (HTTP 400, nothing sent), so no address can hide behind a
    syntax error — and denying it made an ordinary JSON typo look like a policy
    violation. The raw-text scans below still see any literal address anyway.
    """
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    if not text.startswith("{") and not text.startswith("["):
        return None
    try:
        body = json.loads(text)
    except ValueError:
        return None
    return _walk_json_addresses(body)


def _body_values(args: list[str]) -> list[str]:
    """Collect every ``--json`` / ``--params`` value without swallowing repeats."""
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


def _inspect_gws_args(args: list[str]) -> Optional[str]:
    # A substitution that can expand into the gws argument stream could inject
    # identifier flags this hook cannot see (e.g. a marker-only positional
    # `$(x)` expanding to `--userId=attacker@evil.com`). Fail closed; quoted
    # substitutions consumed as flag values are still allowed.
    if substitution_wildcards(args, DOMAIN_BEARING_VALUE_FLAGS):
        return "unverifiable argument built from a command substitution"

    bare_address_args = _bare_address_scan_args(args)
    bare_address_text = " ".join(bare_address_args)

    # userId identifies the acting Workspace user; keep this restricted even
    # when a Gmail helper is only creating a draft.
    for match in USERID_PARAM.finditer(bare_address_text):
        bad = _bad_address(match.group(1))
        if bad:
            return bad

    # JSON request bodies are checked by DECODING them, before any raw-text scan,
    # because a `\uXXXX` escape hides the address from a literal search.
    for value in _body_values(args):
        bad = _bad_json_body(value)
        if bad:
            return bad

    if _allows_external_gmail_draft(args):
        return None

    # An address glued to a substitution (`alice@upstart.com$(x)`) concatenates
    # into one Bash word whose full domain is invisible here — the bare-email
    # regex would stop at the literal prefix. Fail closed on any token that
    # mixes an ``@`` with a substitution marker.
    for arg in bare_address_args:
        has_marker = COMMAND_SUBSTITUTION_MARKER in arg or COMMAND_SUBSTITUTION_MARKER_UNQUOTED in arg
        if has_marker and "@" in arg:
            return "unverifiable address assembled from a command substitution"

    bad = _bad_address_in_text(bare_address_text, "arguments")
    if bad:
        return bad

    return None


def _inspect_command(command: str) -> tuple[str, Optional[str]]:
    """Return an ALLOW, DENY, or ERROR verdict for the shell command.

    Delegates tokenization and recursion (substitutions, ``bash -c``, ``eval``)
    to :func:`iter_gws_invocations`; a :class:`ShellParseError` fails closed by
    returning a distinct error verdict for the shell hook.
    """
    try:
        for args in iter_gws_invocations(command):
            bad = _inspect_gws_args(args)
            if bad:
                return "DENY", bad
    except ShellParseError as exc:
        return "ERROR", str(exc)
    return "ALLOW", None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        cmd = payload.get("tool_input", {}).get("command", "")
    except Exception as exc:
        print(f"failed to parse Bash tool payload: {exc}", file=sys.stderr)
        sys.exit(1)

    # No raw-text `gws` pre-filter here: Bash dequotes the command word before
    # execution, so `g\ws` or `g'w's` run gws even though the raw string never
    # contains a contiguous `gws`. The tokenizer sees the dequoted word, so
    # every command must go through it.
    if not cmd:
        print("ALLOW")
        return

    verdict, detail = _inspect_command(cmd)
    print(verdict)
    if detail:
        print(detail)


if __name__ == "__main__":
    main()
