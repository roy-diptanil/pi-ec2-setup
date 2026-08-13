# ruff: noqa: UP007, UP045
"""Shared shell-parsing layer for the gws PreToolUse hook scripts.

``check-destructive.py`` and ``check-domain.py`` both need to take a Claude Code
Bash payload and locate every ``gws`` invocation inside it — peeling apart shell
operators, command substitutions, and ``bash -c`` / ``eval`` wrappers — before
applying their own (different) policy check to each invocation's argument list.
That tokenization layer is identical for both hooks, so it lives here once
instead of being copy-pasted (and drifting) between the two scripts.

Python 3.9.6 compatibility: this module is imported by scripts that must run on
the system Python shipped with macOS Xcode Command Line Tools (3.9.6). Keep
``from __future__ import annotations`` so PEP 585 / PEP 604 hints stay
stringified, and avoid 3.10+ syntax (``match`` statements, runtime ``X | Y``
unions, ``except*``). The CI guard lives in
``tests/plugins/gws/test_python39_compat.py``.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Iterator
from typing import Optional, Union

# Shell control operators that terminate one command's argument list. ``;;`` is
# included so a `case` clause terminator ends the preceding command instead of
# letting its tokens leak into the next branch. Newlines are normalized to ``;``
# before tokenization (see ``normalize_shell_operators``) so they never need a
# separate entry here.
PIPE_OPERATOR = "|"
COMMAND_SEPARATORS = {";", ";;", "&&", "||", PIPE_OPERATOR, "&"}

# ``shlex`` removes quote/escape provenance, so a quoted or backslash-escaped
# argument whose value is exactly ``;``/``&&``/etc. is otherwise
# indistinguishable from real shell syntax. Protect those characters while
# tokenizing, then decode them only when the word is passed as argv. Private-use
# characters avoid collisions with ordinary shell text while keeping the
# Python 3.9-compatible token representation as plain strings.
_QUOTED_OPERATOR_MARKERS = {";": "\ue100", "&": "\ue101", "|": "\ue102"}
_QUOTED_OPERATOR_TRANSLATION = str.maketrans(
    {marker: operator for operator, marker in _QUOTED_OPERATOR_MARKERS.items()}
)

# Reserved words and grouping operators after which Bash starts a NEW simple
# command, so the following token is in command-word position (``if true; then
# gws ...`` runs gws right after ``then``). Used alongside COMMAND_SEPARATORS so
# a command word built from an expansion is still inspected as a possible gws
# invocation even when it follows a keyword rather than a separator.
COMMAND_WORD_INTRODUCERS = {
    "then",
    "do",
    "else",
    "elif",
    "if",
    "while",
    "until",
    "!",
    "{",
    "(",
    ")",
    "time",
    "coproc",
}

# Commands that execute the command named by a later word. A marker-bearing
# word after one of these is still in command position (``command $(get-tool)``
# or ``env $(get-tool)`` can execute ``gws``), just like a word after
# assignments or redirections.
COMMAND_EXEC_WRAPPERS = {
    "arch",
    "builtin",
    "caffeinate",
    "chrt",
    "command",
    "env",
    "exec",
    "ionice",
    "nice",
    "nohup",
    "script",
    "setsid",
    "stdbuf",
    "sudo",
    "time",
    "timeout",
    "watch",
}
SHELL_EXECUTABLES = frozenset({"bash", "dash", "sh", "zsh"})
# Wrappers that run a named command and APPEND further arguments to it at runtime
# from stdin. The literal tokens are only a prefix of the real argv, so
# ``echo '--cc ext@evil.com' | xargs gws gmail +send --to ok@upstart.com ...``
# looked like a compliant internal send while Bash delivered externally. Any gws
# invocation reached under one of these has its argv marked unverifiable.
ARG_APPENDING_WRAPPERS = {"xargs", "parallel"}
# ``find`` embeds a command after each of these expression actions. The command
# runs later for matching paths, so its operands need the same recursive scan as
# a shell ``-c`` command string.
FIND_COMMAND_ACTIONS = {"-exec", "-execdir", "-ok", "-okdir"}
END_OF_OPTIONS = "--"
HEREDOC_OPERATOR = "<<"
WATCH_EXEC_OPTION = "--exec"
PARALLEL_LOAD_OPTION = "-l"
# Keep ordinary nested shell constructs inspectable, but fail closed before
# Python's own recursion limit when a payload is too deeply nested to analyze.
MAX_SHELL_RECURSION_DEPTH = 32

# Assignment words may prefix a simple command without consuming its command
# position (``FOO=bar gws ...``). Keep this intentionally limited to Bash
# identifier assignments; an expansion that merely *produces* ``FOO=bar`` is
# not parsed as an assignment by Bash and remains a possible command word.
ASSIGNMENT_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")

# Marker for a substitution that appeared inside double quotes: Bash expands it
# to exactly ONE word (part of the surrounding quoted string), so it can act as
# a flag VALUE but can never spill extra argv tokens.
COMMAND_SUBSTITUTION_MARKER = "__gws_command_substitution_value__"
# Marker for an UNQUOTED substitution: Bash word-splits its output, so it can
# expand into any number of argv tokens (flags, recipients, verbs) anywhere in
# the command. Policy checks must fail closed wherever this marker appears in a
# gws argument stream.
COMMAND_SUBSTITUTION_MARKER_UNQUOTED = "__gws_unquoted_command_substitution__"

# Redirection operators, emitted as their own tokens by
# ``normalize_shell_operators``. A redirection TARGET is consumed by the shell
# and never passed to gws, so :func:`_collect_invocation_args` drops each
# operator together with its target before any policy scan runs — otherwise a
# target literally named ``--draft`` or ``delete`` would be misread as gws argv
# (fail-open). The compound forms (``&>``/``&>>`` merge stdout+stderr, ``>|``
# force-clobbers, ``>&``/``<&`` duplicate a descriptor) embed ``&``/``|`` —
# characters that would otherwise read as command separators — so they are
# recognized as atomic operators (see :data:`_REDIRECTION_OPERATOR_PATTERN`)
# rather than terminating the invocation at the ``&``/``|``. The here-doc /
# here-string forms (``<<``, ``<<-``, ``<<<``) and read-write (``<>``) also
# consume a target word (the delimiter or here-string), so they belong here
# too: ``gws gmail +send --to ext@x <<< --draft`` feeds ``--draft`` to stdin,
# not to gws, and must not be read as enabling draft mode.
# ``<<-`` is absent because ``normalize_shell_operators`` collapses it to ``<<``
# (its trailing ``-`` is not a shlex punctuation char), so no token ever equals
# ``<<-`` by the time this set is consulted.
REDIRECTION_OPERATORS = {"<", ">", ">>", "&>", "&>>", ">|", ">&", "<&", "<<", "<<<", "<>"}

# gws command word-boundary check.
GWS_CMD = re.compile(r"(?<![A-Za-z0-9_-])gws(?![A-Za-z0-9_-])", re.IGNORECASE)


def mentions_gws(command: str) -> bool:
    """True when ``command`` could run ``gws``, judged on the DEQUOTED text too.

    Bash dequotes the command word before execution, so ``g'w's``, ``g\\ws`` and
    ``gw"s"`` all run gws while the raw string never contains a contiguous
    ``gws``. Any fail-closed gate keyed on the raw text alone is therefore
    bypassable by quoting the command word (that was a real fail-open: an
    obfuscated command word plus a tokenizer error returned ALLOW). Checking a
    quote-stripped copy as well keeps those gates honest. Over-matching here only
    costs a deny on a command that could not be parsed anyway.
    """
    if GWS_CMD.search(command):
        return True
    dequoted = command.replace("'", "").replace('"', "").replace("\\", "")
    return bool(GWS_CMD.search(dequoted))


def _decode_quoted_operators(token: str) -> str:
    """Restore operator characters protected while they were shell-quoted."""
    return token.translate(_QUOTED_OPERATOR_TRANSLATION)


def _preserve_quoted_separator(token: str) -> str:
    """Keep an exact quoted separator distinct; restore it inside other words."""
    decoded = _decode_quoted_operators(token)
    if decoded in COMMAND_SEPARATORS and decoded != token:
        return token
    return decoded


# A simple (non-braced) parameter expansion: ``$name``, ``$1``, or a special
# parameter (``$@``, ``$*``, ``$#``, ``$?``, ``$$``, ``$!``, ``$-``). Bash
# expands these before word splitting, so an unquoted one can inject arbitrary
# argv tokens (``$FLAGS`` -> ``--draft=false``) and a quoted one can BE any
# single flag/verb. ``${...}`` is read separately (it can nest). ``$(``/``$'``/
# ``$"`` are deliberately NOT matched here — they are command substitution /
# ANSI-C / locale quoting, handled elsewhere.
_PARAM_EXPANSION_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]|[-@*#?!$])")

# Values accepted by Go's ``strconv.ParseBool``, normalized to lowercase. The
# gws CLI's pflag/Cobra boolean flags use that parser.
TRUE_FLAG_VALUES = {"1", "t", "true"}
FALSE_FLAG_VALUES = {"0", "f", "false"}

# Flags in the gws CLI that consume the NEXT argv token as their value
# (pflag-style). This single canonical set is shared by both hook scripts so the
# two can never disagree about which tokens are values vs. flags/positionals.
#
# Correctness here is security-critical in BOTH directions:
#   * Under-inclusion is fail-open for draft detection: if a value-taking flag
#     is missing, an attacker can write ``--missingflag --draft`` so the real
#     gws CLI reads ``--draft`` as the flag's value (a live send) while the hook
#     reads it as an enabled boolean (thinks it is a draft) and ALLOWs.
#   * Over-inclusion is fail-open for the destructive scan: a genuinely boolean
#     flag listed here would swallow a following positional ``delete`` method
#     token (``--boolflag delete``), hiding it from the destructive-verb scan.
# Every entry below is value-taking in the gws CLI; boolean flags such as
# ``--draft``/``--dry-run``/``--html``/``--verbose`` are deliberately absent.
# The Gmail send/forward/reply helpers' flags are pinned against the CLI's own
# ``--help`` output in tests/plugins/gws/test_value_taking_flags.py, so a gws
# upgrade that adds a value-taking flag fails CI instead of silently opening the
# ``--<flag> --draft`` bypass.
VALUE_TAKING_FLAGS = frozenset(
    {
        "--to",
        "--cc",
        "--bcc",
        "--from",
        "--subject",
        "--body",
        "--query",
        "--params",
        "--json",
        "--sanitize",
        "--format",
        "--attach",
        "-a",  # short form of --attach; takes a <PATH>. Missing this opened a
        # `-a --draft` bypass (real CLI attaches a file named "--draft" and
        # sends live, while the hook read --draft as draft mode and allowed it).
        "--attachment",
        "--attachments",
        "--remove",  # +reply-all: excludes recipients (takes <EMAILS>); NOT the
        # destructive `remove` verb, which is a bare positional, not a flag.
        "--label",
        # NOTE: ``--labels`` is deliberately absent — it is a BOOLEAN flag on the
        # gws helpers (verified against `--help`). Listing it made it swallow the
        # following positional, which hid a `delete` method from the scan.
        "--folder",
        # Value-taking flags on the non-Gmail helpers (+read/+append/+push/...),
        # enumerated from every helper's own `--help`. These were missing, so
        # their VALUES were scanned as positional method tokens: a spreadsheet
        # range or cell value like `DeleteMe` read as a destructive method and
        # denied an ordinary read. ./tests/plugins/gws/test_value_taking_flags.py
        # now pins this against every helper, not just the Gmail send family.
        "--spreadsheet",
        "--range",
        "--values",
        "--json-values",
        "--script",
        "--dir",
        "--document",
        "--presentation",
        "--calendar",
        "--attendee",
        "--summary",
        "--description",
        "--location",
        "--timezone",
        "--start",
        "--end",
        "--days",
        "--text",
        "--label-ids",
        "--msg-format",
        "--max-messages",
        "--output-dir",
        "--parent",
        "--project",
        "--topic",
        "--subscription",
        "--poll-interval",
        "--output",
        "-o",
        "--upload",
        "--template",
        "--page-limit",
        "--page-delay",
        "--reply-to",
        "--in-reply-to",
        "--inReplyTo",
        "--reference",
        "--references",
        "--thread",
        "--thread-id",
        "--threadId",
        "--message-id",
        "--messageId",
        "--calendarId",
        "--calendar-id",
        "--userId",
        "--user-id",
        "--max",
        "--max-results",
        "--maxResults",
        "--page-token",
        "--pageToken",
        "--name",
        "--title",
        "--file",
        "--input",
        "--id",
    }
)

# Shell control operators matched longest-first so multi-character operators
# (``&&``, ``||``, ``;;``) are recognized before their single-character
# counterparts.
_SHELL_OPERATOR_PATTERN = re.compile(r"(&&|\|\||;;|;|\||&)")

# Redirection operators, matched (longest-first) BEFORE the command-separator
# pattern in ``normalize_shell_operators``. This is what keeps the ``&`` in
# ``&>``/``&>>``/``>&`` and the ``|`` in ``>|`` from being mis-read as a command
# separator: Bash consumes only a redirection's TARGET and still passes every
# later word as argv, so terminating the gws argument list at the operator
# would drop trailing recipients/methods (``... &> /tmp/out --cc ext@x`` or
# ``... &> /tmp/out delete``) and fail open. Recognizing them as their own
# non-separator token leaves the redirect + target in the argument stream,
# where the surrounding scans skip them harmlessly while still seeing the real
# trailing arguments. Order matters (regex alternation is first-match): longer
# operators precede the single-character forms they contain (``<<<``/``<<-``
# before ``<<`` before ``<``, ``&>>`` before ``&>``, ``>>``/``>|``/``>&`` before
# ``>``).
_REDIRECTION_OPERATOR_PATTERN = re.compile(r"&>>|&>|<<<|<<-|<<|<>|>>|>\||>&|<&|>|<")

_ANSI_C_SIMPLE_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "?": "?",
}

_HEX_DIGITS = "0123456789abcdefABCDEF"
_OCTAL_DIGITS = "01234567"


def _take_digits(command: str, start: int, max_len: int, allowed: str) -> str:
    end = start
    while end < len(command) and end - start < max_len and command[end] in allowed:
        end += 1
    return command[start:end]


def _read_ansi_c_quoted(command: str, body_start: int) -> tuple[str, int]:
    """Read and decode a Bash ANSI-C ``$'...'`` span (starting after ``$'``).

    Bash decodes escape sequences here BEFORE execution, so ``$'del\\x65te'``
    runs ``delete`` — the hook must scan the decoded text, not the raw escape.
    Returns ``(decoded_text, index_after_closing_quote)``. Raises ValueError
    for unterminated spans or escapes we cannot decode faithfully, which
    split_command converts into a fail-closed parse error for gws-mentioning
    commands.
    """
    out: list[str] = []
    truncated = False
    i = body_start
    n = len(command)
    while i < n:
        ch = command[i]
        if ch == "'":
            return "".join(out), i + 1
        if ch != "\\":
            if not truncated:
                out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            break
        esc = command[i + 1]
        if esc in _ANSI_C_SIMPLE_ESCAPES:
            if not truncated:
                out.append(_ANSI_C_SIMPLE_ESCAPES[esc])
            i += 2
            continue
        if esc == "x":
            digits = _take_digits(command, i + 2, 2, _HEX_DIGITS)
            if digits:
                decoded = chr(int(digits, 16))
                if decoded == "\0":
                    truncated = True
                elif not truncated:
                    out.append(decoded)
                i += 2 + len(digits)
            else:
                if not truncated:
                    out.append("\\x")  # bash leaves \x without digits literal
                i += 2
            continue
        if esc in ("u", "U"):
            digits = _take_digits(command, i + 2, 4 if esc == "u" else 8, _HEX_DIGITS)
            if digits:
                code_point = int(digits, 16)
                if code_point > 0x10FFFF:
                    raise ValueError(f"out-of-range \\{esc} escape in $'...' quoting")
                decoded = chr(code_point)
                if decoded == "\0":
                    truncated = True
                elif not truncated:
                    out.append(decoded)
                i += 2 + len(digits)
            else:
                if not truncated:
                    out.append("\\" + esc)
                i += 2
            continue
        if esc in _OCTAL_DIGITS:
            digits = _take_digits(command, i + 1, 3, _OCTAL_DIGITS)
            decoded = chr(int(digits, 8) & 0xFF)
            if decoded == "\0":
                truncated = True
            elif not truncated:
                out.append(decoded)
            i += 1 + len(digits)
            continue
        if esc == "c":
            # ``\cX`` is a control character. Bash computes it as
            # ``toupper(X) & 0x1F`` (verified against bash 5.3 and 3.2), with
            # ``\c?`` as the documented DEL special case — NOT ``ord(X) ^ 0x40``,
            # which agrees only for letters and diverges for ``\c$``/``\c%``/
            # ``\c4``. Decode rather than raise: raising made an ordinary Bash
            # construct unparseable, and a parse error used to return ALLOW.
            operand = command[i + 2] if i + 2 < n else ""
            if operand == "\\" and i + 3 < n and command[i + 3] == "\\":
                # ``\c\\`` — the operand is an ESCAPED backslash, so Bash consumes
                # all four characters. Consuming only three left a stray ``\``
                # that then escaped the span's closing quote and produced a
                # spurious "unterminated" error on a valid command.
                if not truncated:
                    out.append(chr(ord("\\") & 0x1F))
                i += 4
                continue
            if operand in ("", "'"):
                # ``\c`` with no operand (end of input, or the span's closing
                # quote) is literal ``\c`` in Bash, and the quote still closes.
                # Consuming the quote here left the span unterminated, which
                # turned into a spurious parse error.
                if not truncated:
                    out.append("\\c")
                i += 2
                continue
            if not truncated:
                if operand == "?":
                    out.append(chr(0x7F))
                elif operand.isascii():
                    out.append(chr(ord(operand.upper()) & 0x1F))
                else:
                    # Bash applies ``\cX`` byte-wise. Preserve a non-ASCII form
                    # literally instead of letting ``upper()`` expand to multiple
                    # characters (for example, ``ß`` -> ``SS``) and crash ``ord``.
                    out.append("\\c" + operand)
            i += 3
            continue
        # Unknown escape: bash keeps the backslash and character literally.
        if not truncated:
            out.append("\\")
            out.append(esc)
        i += 2
    raise ValueError("unterminated $'...' quote")


def decode_ansi_c_quoting(command: str) -> str:
    """Rewrite every unquoted ANSI-C ``$'...'`` span as a plain single-quoted word.

    This runs FIRST, before any other pass, and exists because ``$'...'`` uses
    different quoting rules from ``'...'``: a backslash-escaped quote (``$'it\\'s'``)
    is a literal apostrophe that does NOT close the span. Passes that treat the
    span as ordinary single quotes read that inner ``'`` as a close and the
    trailing ``'`` as an open, so they run the rest of the command believing they
    are inside a quoted string — and then emit no markers for the ``$VAR``,
    ``$(...)``, glob or brace expansions that follow. That silently disabled the
    whole fail-closed foundation (an external ``--cc`` in a variable sailed
    through), so the decode must happen once, up front, rather than in each pass.

    Emitting a plain ``'...'`` word means every later pass sees only simple
    quoting and needs no ``$'...'`` knowledge at all. Raises ValueError for an
    unterminated span; callers turn that into a fail-closed parse error.
    """
    out: list[str] = []
    i = 0
    n = len(command)
    quote: Optional[str] = None
    while i < n:
        ch = command[i]
        if quote is not None:
            out.append(ch)
            if ch == "\\" and i + 1 < n and quote == '"':
                out.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            # An escaped ``$`` is a literal dollar, not an ANSI-C opener.
            out.append(ch)
            out.append(command[i + 1])
            i += 2
            continue
        if ch == "$" and i + 1 < n and command[i + 1] == "'":
            decoded, next_index = _read_ansi_c_quoted(command, i + 2)
            out.append("'" + decoded.replace("'", "'\\''") + "'")
            i = next_index
            continue
        if ch in ('"', "'"):
            quote = ch
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


class ShellParseError(Exception):
    """Raised when a gws-containing command cannot be tokenized.

    Callers must treat this as fail-closed (deny), since an un-parseable gws
    command is exactly the case where the policy check cannot be trusted.
    """


def basename(token: str) -> str:
    """Return the case-folded executable basename used for command dispatch.

    The plugin primarily runs on default macOS filesystems, where ``BASH`` and
    ``/bin/BASH`` resolve to the same executable as their lowercase spellings.
    """
    return os.path.basename(token).lower()


def _is_command_wrapper_option(token: str) -> bool:
    """Return whether ``token`` is an executing `command` option cluster.

    `-p` may be repeated/clustered and still executes the following word.
    Clusters containing `-v`/`-V` only query command metadata, so they are not
    prefixes for a later executable command word.
    """
    return token.startswith("-") and len(token) > 1 and set(token[1:]) <= {"p"}


def _exec_wrapper_option(token: str) -> tuple[bool, bool]:
    """Return ``(valid_option_cluster, consumes_next_value)`` for `exec`.

    Bash clusters `-c`/`-l`; `-a` consumes the rest of its cluster as argv[0]
    or, when last, consumes the next word (``-ca NAME``). Parsing this exactly
    keeps the following expanded command word in command position.
    """
    if not token.startswith("-") or len(token) <= 1:
        return False, False
    cluster = token[1:]
    for index, option in enumerate(cluster):
        if option == "a":
            return True, index == len(cluster) - 1
        if option not in {"c", "l"}:
            return False, False
    return True, False


def _sudo_wrapper_option(argument: str) -> tuple[bool, bool]:
    """Return ``(is_option, consumes_next_value)`` for `sudo`.

    Short options may carry their value in the same token (``-uroot``), while
    long options use either a following word or ``=value``. Unknown options are
    still prefixes: sudo will reject them rather than execute them as a command.
    """
    if not argument.startswith("-") or argument == "-":
        return False, False
    if argument.startswith("--"):
        name, separator, _ = argument.partition("=")
        takes_value = name in {
            "--chdir",
            "--chroot",
            "--command-timeout",
            "--group",
            "--host",
            "--prompt",
            "--role",
            "--type",
            "--user",
        }
        return True, takes_value and not separator
    cluster = argument[1:]
    for index, option in enumerate(cluster):
        if option in {"C", "D", "g", "h", "p", "R", "r", "t", "T", "u"}:
            return True, index == len(cluster) - 1
    return True, False


def _nice_wrapper_option(argument: str) -> tuple[bool, bool]:
    """Return ``(is_option, consumes_next_value)`` for `nice`."""
    if re.fullmatch(r"-\d+", argument):
        return True, False
    if argument == "-n":
        return True, True
    if argument.startswith("-n") and len(argument) > 2:
        return True, False
    if argument == "--adjustment":
        return True, True
    if argument.startswith("--adjustment="):
        return True, False
    return argument.startswith("-") and argument != "-", False


def _ionice_wrapper_option(argument: str) -> tuple[bool, bool, bool]:
    """Return option, next-value, and non-executing-mode states for `ionice`."""
    if not argument.startswith("-") or argument == "-":
        return False, False, False
    if argument.startswith("--"):
        name, separator, _ = argument.partition("=")
        if name in {"--help", "--version"}:
            return True, False, True
        takes_value = name in {"--class", "--classdata", "--pid", "--pgid", "--uid"}
        valid = takes_value or name == "--ignore"
        nonexecuting = name in {"--pid", "--pgid", "--uid"}
        return valid, takes_value and not separator, nonexecuting

    cluster = argument[1:]
    for index, option in enumerate(cluster):
        if option in {"h", "V"}:
            return True, False, True
        if option in {"c", "n", "p", "P", "u"}:
            nonexecuting = option in {"p", "P", "u"}
            return True, index == len(cluster) - 1, nonexecuting
        if option != "t":
            return False, False, False
    return True, False, False


def _chrt_wrapper_option(argument: str) -> tuple[bool, bool, bool]:
    """Return option, next-value, and non-executing-mode states for `chrt`."""
    if not argument.startswith("-") or argument == "-":
        return False, False, False
    if argument.startswith("--"):
        name, separator, _ = argument.partition("=")
        if name in {"--help", "--max", "--pid", "--version"}:
            return True, False, True
        takes_value = name in {"--sched-deadline", "--sched-period", "--sched-runtime"}
        no_value = name in {
            "--all-tasks",
            "--batch",
            "--deadline",
            "--ext",
            "--fifo",
            "--idle",
            "--other",
            "--reset-on-fork",
            "--rr",
            "--verbose",
        }
        return takes_value or no_value, takes_value and not separator, False

    cluster = argument[1:]
    for index, option in enumerate(cluster):
        if option in {"h", "m", "p", "V"}:
            return True, False, True
        if option in {"D", "P", "T"}:
            return True, index == len(cluster) - 1, False
        if option not in {"R", "a", "b", "d", "e", "f", "i", "o", "r", "v"}:
            return False, False, False
    return True, False, False


def _arch_wrapper_option(argument: str) -> tuple[bool, bool, bool]:
    """Return option, next-value, and non-executing states for macOS `arch`."""
    if argument == "-h":
        return True, False, True
    if argument in {"-32", "-64", "-c"}:
        return True, False, False
    if argument in {"-arch", "-d", "-e"}:
        return True, True, False
    if argument in {"-arm64", "-arm64e", "-i386", "-x86_64", "-x86_64h"}:
        return True, False, False
    return False, False, False


def _caffeinate_wrapper_option(argument: str) -> tuple[bool, bool]:
    """Return ``(is_option, consumes_next_value)`` for macOS `caffeinate`."""
    if not argument.startswith("-") or argument == "-" or argument.startswith("--"):
        return False, False
    cluster = argument[1:]
    for index, option in enumerate(cluster):
        if option in {"t", "w"}:
            return True, index == len(cluster) - 1
        if option not in {"d", "i", "m", "s", "u"}:
            return False, False
    return True, False


def _nohup_wrapper_option(argument: str) -> bool:
    """Return whether ``argument`` is a `nohup` option that still runs a command."""
    # macOS/BSD nohup supports -p. GNU's --help/--version and any unknown option
    # exit without running a following word, so they must end command position.
    return argument == "-p"


def _script_wrapper_option(argument: str) -> tuple[bool, bool, bool]:
    """Return option, next-value, and non-executing states for macOS `script`."""
    if not argument.startswith("-") or argument == "-":
        return False, False, False
    if argument.startswith("--"):
        # macOS script has no long options, so these exit without executing a
        # following command.
        return True, False, True

    cluster = argument[1:]
    for index, option in enumerate(cluster):
        if option in {"d", "p", "T"}:
            # These select playback mode, whose operands are recordings rather
            # than commands.
            return True, option == "T" and index == len(cluster) - 1, True
        if option == "t":
            return True, index == len(cluster) - 1, False
        if option not in {"F", "a", "e", "k", "q", "r"}:
            # Unknown macOS options make script print usage and exit.
            return True, False, True
    return True, False, False


def _setsid_wrapper_option(argument: str) -> bool:
    """Return whether ``argument`` is a `setsid` option that still runs a command."""
    if argument.startswith("--"):
        return argument in {"--ctty", "--fork", "--wait"}
    if not argument.startswith("-") or argument == "-":
        return False
    return all(option in {"c", "f", "w"} for option in argument[1:])


def _stdbuf_wrapper_option(argument: str) -> tuple[bool, bool]:
    """Return ``(is_option, consumes_next_value)`` for GNU `stdbuf`."""
    if argument.startswith("--"):
        name, separator, _ = argument.partition("=")
        takes_value = name in {"--error", "--input", "--output"}
        return takes_value, takes_value and not separator
    if len(argument) < 2 or argument[0] != "-" or argument[1] not in {"e", "i", "o"}:
        return False, False
    return True, len(argument) == 2


def _xargs_wrapper_option(argument: str) -> tuple[bool, bool, bool, Optional[str]]:
    """Return option, next-value, replacement-mode, and attached replacement states for `xargs`."""
    if not argument.startswith("-") or argument == "-":
        return False, False, False, None
    if argument.startswith("--"):
        name, separator, value = argument.partition("=")
        if name in {"--help", "--version"}:
            return False, False, False, None
        if name == "--replace":
            replacement = value if separator else "{}"
            return True, False, True, replacement or None
        takes_value = name in {
            "--arg-file",
            "--delimiter",
            "--max-args",
            "--max-chars",
            "--max-procs",
            "--process-slot-var",
        }
        no_value = name in {
            "--eof",
            "--exit",
            "--interactive",
            "--max-lines",
            "--no-run-if-empty",
            "--null",
            "--open-tty",
            "--show-limits",
            "--verbose",
        }
        return takes_value or no_value, takes_value and not separator, False, None
    cluster = argument[1:]
    for index, option in enumerate(cluster):
        if option in {"I", "J"}:
            replacement = cluster[index + 1 :]
            return True, not replacement, True, replacement or None
        if option == "i":
            # GNU's legacy optional replacement defaults to ``{}`` and only
            # consumes an attached suffix.
            return True, False, True, cluster[index + 1 :] or "{}"
        if option in {"a", "d", "E", "L", "n", "P", "R", "S", "s"}:
            return True, index == len(cluster) - 1, False, None
        if option in {"e", "l"}:
            # GNU's legacy optional-argument forms consume only an attached
            # suffix. A bare -e/-l does not consume the next word.
            return True, False, False, None
        if option not in {"0", "o", "p", "r", "t", "x"}:
            return False, False, False, None
    return True, False, False, None


def _parallel_wrapper_option(argument: str) -> tuple[bool, bool]:
    """Return ``(is_option, consumes_next_value)`` for GNU `parallel`."""
    if not argument.startswith("-") or argument == "-":
        return False, False
    if argument.startswith("--"):
        name, separator, _ = argument.partition("=")
        if name in {"--help", "--version"}:
            return False, False
        takes_value = name in {
            "--arg-file",
            "--arg-file-sep",
            "--arg-sep",
            "--basefile",
            "--block",
            "--block-timeout",
            "--colsep",
            "--compress-program",
            "--delay",
            "--delimiter",
            "--env",
            "--filter",
            "--group-by",
            "--halt",
            "--header",
            "--hostgroups",
            "--joblog",
            "--jobs",
            "--load",
            "--max-args",
            "--max-chars",
            "--max-lines",
            "--max-procs",
            "--memfree",
            "--nice",
            "--results",
            "--retries",
            "--return",
            "--rpl",
            "--rsync-opts",
            "--shell",
            "--ssh",
            "--ssh-delay",
            "--sshlogin",
            "--sshloginfile",
            "--tagstring",
            "--timeout",
            "--tmpdir",
            "--total-jobs",
            "--transferfile",
            "--trim",
            "--workdir",
            "--wd",
        }
        # Treat every other option as flag-like. Unknown options make parallel
        # exit, while future flag options must not hide the following command.
        return True, takes_value and not separator
    cluster = argument[1:]
    for index, option in enumerate(cluster):
        if option in {"a", "C", "E", "I", "J", "L", "N", "P", "S", "d", "j", "n", "s"}:
            return True, index == len(cluster) - 1
    return True, False


def _time_wrapper_option(argument: str) -> tuple[bool, bool]:
    """Return ``(is_option, consumes_next_value)`` for shell/GNU/BSD `time`."""
    if not argument.startswith("-") or argument == "-":
        return False, False
    if argument.startswith("--"):
        name, separator, _ = argument.partition("=")
        if name in {"--help", "--version"}:
            return False, False
        takes_value = name in {"--format", "--output"}
        no_value = name in {"--append", "--portability", "--quiet", "--verbose"}
        return takes_value or no_value, takes_value and not separator
    cluster = argument[1:]
    for index, option in enumerate(cluster):
        if option in {"f", "o"}:
            return True, index == len(cluster) - 1
        if option not in {"a", "l", "p", "q", "v"}:
            return False, False
    return True, False


def _timeout_wrapper_option(argument: str) -> tuple[bool, bool]:
    """Return ``(is_option, consumes_next_value)`` for GNU `timeout`."""
    if not argument.startswith("-") or argument == "-":
        return False, False
    if argument.startswith("--"):
        name, separator, _ = argument.partition("=")
        takes_value = name in {"--kill-after", "--signal"}
        no_value = name in {"--foreground", "--preserve-status", "--verbose"}
        return takes_value or no_value, takes_value and not separator
    cluster = argument[1:]
    for index, option in enumerate(cluster):
        if option in {"k", "s"}:
            return True, index == len(cluster) - 1
        if option != "v":
            return False, False
    return True, False


def _watch_wrapper_option(argument: str) -> tuple[bool, bool]:
    """Return ``(is_option, consumes_next_value)`` for procps `watch`."""
    if not argument.startswith("-") or argument == "-":
        return False, False
    if argument.startswith("--"):
        name, separator, _ = argument.partition("=")
        if name in {"--help", "--version"}:
            return False, False
        takes_value = name in {"--equexit", "--interval", "--shotsdir"}
        no_value = name in {
            "--beep",
            "--color",
            "--differences",
            "--errexit",
            "--exec",
            "--chgexit",
            "--no-color",
            "--no-rerun",
            "--no-title",
            "--no-wrap",
            "--precise",
        }
        return takes_value or no_value, takes_value and not separator
    cluster = argument[1:]
    for index, option in enumerate(cluster):
        if option in {"n", "q", "s"}:
            return True, index == len(cluster) - 1
        if option == "d":
            # -d's optional cumulative mode must be attached; a following word
            # is the command, not the option value.
            return True, False
        if option not in {"b", "c", "C", "e", "g", "p", "r", "t", "w", "x"}:
            return False, False
    return True, False


def _env_wrapper_option(argument: str) -> tuple[bool, bool]:
    """Return ``(is_option, consumes_next_value)`` for `env`.

    GNU and BSD/macOS ``env`` accept options before their assignment operands
    and command. Value-taking short options consume the rest of their cluster,
    or the next word when no attached value remains. Unknown option spellings
    are still options to ``env`` (and may make it exit); they cannot be the
    command word unless they follow ``--``.
    """
    if argument == "-":
        return True, False
    if argument.startswith("--"):
        name, separator, _ = argument.partition("=")
        takes_value = name in {"--chdir", "--split-string", "--unset"}
        return True, takes_value and not separator
    if not argument.startswith("-") or len(argument) <= 1:
        return False, False
    cluster = argument[1:]
    for index, option in enumerate(cluster):
        if option in {"C", "P", "S", "u"}:
            return True, index == len(cluster) - 1
    return True, False


def _env_split_string_option(argument: str) -> tuple[bool, Optional[str]]:
    """Return whether an ``env`` option is split-string and any attached value."""
    if argument.startswith("--"):
        name, separator, value = argument.partition("=")
        return name == "--split-string", value if separator else None
    if not argument.startswith("-") or len(argument) <= 1:
        return False, None
    cluster = argument[1:]
    for index, option in enumerate(cluster):
        if option == "S":
            return True, cluster[index + 1 :] or None
        if option in {"C", "P", "u"}:
            # These options consume the rest of the cluster as their own value.
            return False, None
    return False, None


def strip_substitution_markers(token: str) -> str:
    """Return ``token`` with substitution markers removed.

    This approximates the Bash word when every substitution expands empty —
    the fail-closed reading for detection purposes (``gws$(true)`` must still
    be recognized as a gws invocation).
    """
    return token.replace(COMMAND_SUBSTITUTION_MARKER_UNQUOTED, "").replace(COMMAND_SUBSTITUTION_MARKER, "")


def has_substitution_marker(token: str) -> bool:
    """True when ``token`` carries any command-substitution / expansion marker."""
    return COMMAND_SUBSTITUTION_MARKER_UNQUOTED in token or COMMAND_SUBSTITUTION_MARKER in token


def is_gws_token(token: str) -> bool:
    # The plugin's primary macOS platform uses a case-insensitive filesystem by
    # default, so ``GWS`` and ``/opt/homebrew/bin/GWS`` can execute the installed
    # lowercase binary. Match the executable name with the same semantics.
    return basename(strip_substitution_markers(token)).lower() == "gws"


def is_gmail_service(token: str) -> bool:
    return token.split(":", 1)[0].lower() == "gmail"


def _read_dollar_paren_body(command: str, body_start: int) -> tuple[Optional[str], int]:
    """Read the body of an unquoted ``$(...)`` command substitution."""
    depth = 1
    i = body_start
    n = len(command)
    quote: Optional[str] = None
    while i < n:
        ch = command[i]
        if quote is not None:
            if ch == "\\" and i + 1 < n and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if command.startswith("$(", i):
            depth += 1
            i += 2
            continue
        if ch == ")":
            depth -= 1
            if depth == 0:
                return command[body_start:i], i + 1
        i += 1
    return None, body_start


def _read_arithmetic_body(command: str, body_start: int) -> tuple[Optional[str], int]:
    """Read a ``$((...))`` body starting after the opening parentheses."""
    depth = 0
    i = body_start
    n = len(command)
    quote: Optional[str] = None
    while i < n:
        ch = command[i]
        if quote is not None:
            if ch == "\\" and i + 1 < n and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if command.startswith("$(", i):
            _, next_index = _read_dollar_paren_body(command, i + 2)
            i = next_index if next_index > i else i + 2
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            if depth:
                depth -= 1
                i += 1
                continue
            if i + 1 < n and command[i + 1] == ")":
                return command[body_start:i], i + 2
        i += 1
    return None, body_start


_ARITHMETIC_NUMERIC_LITERAL = re.compile(r"(?<![A-Za-z0-9_])(?:0[xX][0-9A-Fa-f]+|[0-9]+#[0-9A-Za-z@_]+|[0-9]+)")
_ARITHMETIC_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _arithmetic_identifiers(expression: str) -> set[str]:
    """Return variable names that Bash may recursively evaluate."""
    without_literals = _ARITHMETIC_NUMERIC_LITERAL.sub("", expression)
    return set(_ARITHMETIC_IDENTIFIER.findall(without_literals))


def _arithmetic_assignment_graph(command: str) -> tuple[set[str], dict[str, set[str]]]:
    """Return gws-bearing assignments and their arithmetic dependencies."""
    tokens = split_command(command)
    if isinstance(tokens, tuple):
        return set(), {}

    gws_names: set[str] = set()
    dependencies: dict[str, set[str]] = {}
    at_command_word = True
    for token in tokens:
        if token in COMMAND_SEPARATORS or token in COMMAND_WORD_INTRODUCERS:
            at_command_word = True
            continue
        assignment = ASSIGNMENT_WORD.match(token) if at_command_word else None
        if assignment is not None:
            raw_name, value = token.split("=", 1)
            name = raw_name.removesuffix("+")
            references = _arithmetic_identifiers(value)
            # Preserve every observed edge rather than only the final assignment:
            # an earlier arithmetic context may run before a later overwrite.
            dependencies[name] = dependencies.get(name, set()) | references
            if mentions_gws(value):
                gws_names.add(name)
            continue
        at_command_word = False
    return gws_names, dependencies


def _parameter_array_subscript_identifiers(body: str) -> set[str]:
    """Return identifiers from an indexed-array parameter subscript."""
    match = re.match(r"^(?:[!#])?[A-Za-z_][A-Za-z0-9_]*\[", body)
    if match is None:
        return set()

    depth = 1
    i = match.end()
    start = i
    quote: Optional[str] = None
    while i < len(body):
        ch = body[i]
        if quote is not None:
            if ch == "\\" and i + 1 < len(body) and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < len(body):
            i += 2
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return _arithmetic_identifiers(body[start:i])
        i += 1
    return set()


def _array_parameter_identifiers(command: str) -> set[str]:
    """Return names evaluated as indexed-array parameter subscripts."""
    identifiers: set[str] = set()
    i = 0
    quote: Optional[str] = None
    while i < len(command):
        ch = command[i]
        if quote == "'":
            if ch == quote:
                quote = None
            i += 1
            continue
        if command.startswith("${", i):
            body, next_index = _read_brace_param(command, i + 2)
            if body is not None:
                identifiers.update(_parameter_array_subscript_identifiers(body))
                i = next_index
                continue
        if quote == '"':
            if ch == "\\" and i + 1 < len(command):
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < len(command):
            i += 2
            continue
        i += 1
    return identifiers


_READ_OPTIONS_WITH_VALUES = frozenset("adinNptu")
_DECLARATION_BUILTINS = frozenset({"declare", "local", "typeset"})
_DECLARATION_INSPECTION_FLAGS = frozenset("fFp")
_VARIABLE_EXISTENCE_BUILTINS = frozenset({"test", "[", "[["})


def _builtin_options_and_operands(args: list[str]) -> tuple[list[str], list[str]]:
    """Split simple builtin options from operands without evaluating either."""
    options: list[str] = []
    operands: list[str] = []
    options_ended = False
    for arg in args:
        if not options_ended and arg == "--":
            options_ended = True
            continue
        if not options_ended and len(arg) > 1 and arg[0] in "-+":
            options.append(arg)
            continue
        options_ended = True
        operands.append(arg)
    return options, operands


def _evaluated_builtin_array_identifiers(command: str) -> set[str]:
    """Return names evaluated by indexed-array operands passed to builtins.

    Bash evaluates indexed-array subscripts supplied to ``printf -v``,
    positional ``read`` destinations, declaration assignments, variable
    targets passed to ``unset``, and ``-v`` variable-existence tests. A stored
    arithmetic expression in that subscript can therefore execute a command
    substitution before the builtin changes anything. Inspect direct calls and
    the ``builtin`` / safe ``command`` prefixes that still dispatch to the
    shell builtins.
    """
    tokens = split_command(command)
    if isinstance(tokens, tuple):
        return set()

    identifiers: set[str] = set()
    segments: list[list[str]] = [[]]
    in_double_bracket = False
    for word in tokens:
        if word == "[[":
            in_double_bracket = True
        is_command_boundary = word in COMMAND_SEPARATORS or word in COMMAND_WORD_INTRODUCERS
        if not in_double_bracket and is_command_boundary and not (word == "!" and segments[-1]):
            segments.append([])
        else:
            segments[-1].append(word)
        if word == "]]":
            in_double_bracket = False

    for segment in segments:
        words: list[str] = []
        index = 0
        while index < len(segment):
            if segment[index] in REDIRECTION_OPERATORS:
                index += 2
                continue
            words.append(segment[index])
            index += 1

        index = 0
        while index < len(words) and ASSIGNMENT_WORD.match(words[index]) is not None:
            index += 1
        while index < len(words) and basename(words[index]) in {"builtin", "command"}:
            wrapper = basename(words[index])
            index += 1
            while index < len(words) and words[index].startswith("-"):
                option = words[index]
                index += 1
                if option == "--":
                    break
                if wrapper == "builtin" or (wrapper == "command" and not set(option[1:]) <= {"p"}):
                    index = len(words)
                    break
        if index >= len(words):
            continue

        builtin = basename(words[index])
        args = words[index + 1 :]
        if builtin == "printf":
            arg_index = 0
            while arg_index < len(args):
                arg = args[arg_index]
                if arg == "-v" and arg_index + 1 < len(args):
                    identifiers.update(_parameter_array_subscript_identifiers(args[arg_index + 1]))
                    arg_index += 2
                    continue
                if arg.startswith("-v") and len(arg) > 2:
                    identifiers.update(_parameter_array_subscript_identifiers(arg[2:]))
                    arg_index += 1
                    continue
                break
        elif builtin == "read":
            arg_index = 0
            options_ended = False
            while arg_index < len(args):
                arg = args[arg_index]
                if not options_ended and arg == "--":
                    options_ended = True
                    arg_index += 1
                    continue
                if not options_ended and arg.startswith("-") and arg != "-":
                    option_chars = arg[1:]
                    value_offset = next(
                        (offset for offset, char in enumerate(option_chars) if char in _READ_OPTIONS_WITH_VALUES),
                        None,
                    )
                    if value_offset is not None and value_offset == len(option_chars) - 1:
                        arg_index += 2
                    else:
                        arg_index += 1
                    continue
                options_ended = True
                identifiers.update(_parameter_array_subscript_identifiers(arg))
                arg_index += 1
        elif builtin in _DECLARATION_BUILTINS:
            options, operands = _builtin_options_and_operands(args)
            inspection_only = any(_DECLARATION_INSPECTION_FLAGS.intersection(option[1:]) for option in options)
            if not inspection_only:
                for operand in operands:
                    if "=" in operand:
                        target = operand.split("=", 1)[0]
                        identifiers.update(_parameter_array_subscript_identifiers(target))
        elif builtin == "unset":
            options, operands = _builtin_options_and_operands(args)
            variable_targets = all(option.startswith("-") and set(option[1:]) <= {"v"} for option in options)
            if variable_targets:
                for operand in operands:
                    identifiers.update(_parameter_array_subscript_identifiers(operand))
        elif builtin in _VARIABLE_EXISTENCE_BUILTINS:
            operands = args[:-1] if builtin in {"[", "[["} and args and args[-1] in {"]", "]]"} else args
            for arg_index, arg in enumerate(operands[:-1]):
                if arg == "-v":
                    identifiers.update(_parameter_array_subscript_identifiers(operands[arg_index + 1]))
    return identifiers


def _reaches_gws_assignment(evaluated_names: set[str], gws_names: set[str], dependencies: dict[str, set[str]]) -> bool:
    """True when an evaluated name transitively reaches a gws assignment."""
    pending = list(evaluated_names)
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        if name in gws_names:
            return True
        pending.extend(dependencies.get(name, set()) - seen)
    return False


def _let_arithmetic_identifiers(command: str) -> set[str]:
    """Return identifiers evaluated by a live Bash ``let`` builtin."""
    tokens = split_command(command)
    if isinstance(tokens, tuple):
        return set()

    identifiers: set[str] = set()
    at_command_word = True
    in_let = False
    for token in tokens:
        if token in COMMAND_SEPARATORS or token in COMMAND_WORD_INTRODUCERS:
            at_command_word = True
            in_let = False
            continue
        if in_let:
            identifiers.update(_arithmetic_identifiers(token))
            continue
        if at_command_word and ASSIGNMENT_WORD.match(token) is not None:
            continue
        if at_command_word and basename(token) == "let":
            at_command_word = False
            in_let = True
            continue
        at_command_word = False
    return identifiers


def has_indirect_arithmetic_expansion(command: str) -> bool:
    """True when an arithmetic context evaluates a gws-bearing assignment."""
    evaluated_names = (
        _let_arithmetic_identifiers(command)
        | _array_parameter_identifiers(command)
        | _evaluated_builtin_array_identifiers(command)
    )
    i = 0
    n = len(command)
    quote: Optional[str] = None
    while i < n:
        ch = command[i]
        if quote is not None:
            if quote == '"' and command.startswith("$((", i):
                body, next_index = _read_arithmetic_body(command, i + 3)
                if body is not None:
                    evaluated_names.update(_arithmetic_identifiers(body))
                    i = next_index
                    continue
            if ch == "\\" and i + 1 < n and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if command.startswith("$((", i):
            body, next_index = _read_arithmetic_body(command, i + 3)
            if body is not None:
                evaluated_names.update(_arithmetic_identifiers(body))
                i = next_index
                continue
        if command.startswith("((", i):
            body, next_index = _read_arithmetic_body(command, i + 2)
            if body is not None:
                evaluated_names.update(_arithmetic_identifiers(body))
                i = next_index
                continue
        i += 1
    gws_names, dependencies = _arithmetic_assignment_graph(command)
    return _reaches_gws_assignment(evaluated_names, gws_names, dependencies)


def _read_backtick_body(command: str, body_start: int) -> tuple[Optional[str], int]:
    i = body_start
    while i < len(command):
        ch = command[i]
        if ch == "\\" and i + 1 < len(command):
            i += 2
            continue
        if ch == "`":
            return command[body_start:i], i + 1
        i += 1
    return None, body_start


def _read_paren_body(command: str, body_start: int) -> tuple[Optional[str], int]:
    """Read a balanced-parenthesis body starting after a ``<(`` / ``>(`` opener.

    Counts every ``(``/``)`` (not just ``$(``) so nested process substitutions
    and subshells inside the body are consumed, and respects quotes. Returns
    ``(body, index_after_closing_paren)`` or ``(None, body_start)`` if
    unbalanced.
    """
    depth = 1
    i = body_start
    n = len(command)
    quote: Optional[str] = None
    while i < n:
        ch = command[i]
        if quote is not None:
            if ch == "\\" and i + 1 < n and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth -= 1
            if depth == 0:
                return command[body_start:i], i + 1
            i += 1
            continue
        i += 1
    return None, body_start


def _read_brace_param(command: str, body_start: int) -> tuple[Optional[str], int]:
    """Read a ``${...}`` parameter-expansion body starting after the ``{``.

    Tracks ``{`` nesting (``${x:-${y}}``) and quotes, and skips nested ``$(...)``
    and backtick spans so a ``}`` inside them does not close the expansion
    early. Returns ``(body, index_after_closing_brace)`` or ``(None, body_start)``
    if unbalanced.
    """
    depth = 1
    i = body_start
    n = len(command)
    quote: Optional[str] = None
    while i < n:
        ch = command[i]
        if quote is not None:
            if ch == "\\" and i + 1 < n and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if command.startswith("$(", i):
            _, next_index = _read_dollar_paren_body(command, i + 2)
            i = next_index if next_index > i else i + 2
            continue
        if ch == "`":
            _, next_index = _read_backtick_body(command, i + 1)
            i = next_index if next_index > i else i + 1
            continue
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return command[body_start:i], i + 1
            i += 1
            continue
        i += 1
    return None, body_start


def mask_command_substitutions(command: str) -> tuple[str, list[str]]:
    """Mask substitution bodies for outer parsing and return them separately.

    Each ``$(...)`` / backtick body is replaced by a marker in the outer
    command (glued into the surrounding word, mirroring Bash concatenation)
    and appended to the returned list so callers can inspect the bodies
    recursively. The marker only records WHERE an expansion occurs — an
    unquoted substitution can still word-split into arbitrary argv tokens at
    runtime, which is why policy checks must treat markers as unverifiable
    wildcards (see :func:`substitution_wildcards`) rather than trusting the
    surrounding parse.
    """
    out: list[str] = []
    substitutions: list[str] = []
    i = 0
    n = len(command)
    quote: Optional[str] = None
    while i < n:
        ch = command[i]
        if quote is not None:
            if quote == '"' and command.startswith("$(", i):
                body, next_index = _read_dollar_paren_body(command, i + 2)
                if body is not None:
                    substitutions.append(body)
                    out.append(COMMAND_SUBSTITUTION_MARKER)
                    i = next_index
                    continue
            if quote == '"' and ch == "`":
                body, next_index = _read_backtick_body(command, i + 1)
                if body is not None:
                    substitutions.append(body)
                    out.append(COMMAND_SUBSTITUTION_MARKER)
                    i = next_index
                    continue
            if quote == '"' and command.startswith("${", i):
                # Parameter expansion inside double quotes stays a single word,
                # but that word could BE any flag/verb (``"${x:---draft=false}"``),
                # so mask it as the single-word marker. Recurse the body so a
                # command substitution in a default/alternate value
                # (``"${x:-$(gws ...)}"``) is still surfaced.
                body, next_index = _read_brace_param(command, i + 2)
                if body is not None:
                    _, inner = mask_command_substitutions(body)
                    substitutions.extend(inner)
                    # ``"${arr[@]}"`` is the exception to "quoted means one word":
                    # the ``[@]`` subscript forms expand to ONE WORD PER ELEMENT
                    # even inside quotes, so they can spill past a value slot
                    # (``--subject "${arr[@]}"`` -> ``--subject harmless --cc
                    # ext@evil.com``). ``[*]`` really is one word, so it keeps the
                    # single-word marker.
                    out.append(COMMAND_SUBSTITUTION_MARKER_UNQUOTED if "[@]" in body else COMMAND_SUBSTITUTION_MARKER)
                    i = next_index
                    continue
            if quote == '"' and ch == "$":
                param = _PARAM_EXPANSION_RE.match(command, i)
                if param:
                    # ``"$@"`` splits per positional parameter; ``"$*"`` does not.
                    out.append(
                        COMMAND_SUBSTITUTION_MARKER_UNQUOTED if param.group(0) == "$@" else COMMAND_SUBSTITUTION_MARKER
                    )
                    i = param.end()
                    continue
            out.append(ch)
            if ch == "\\" and i + 1 < n and quote == '"':
                out.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(ch)
            out.append(command[i + 1])
            i += 2
            continue
        if command.startswith("<(", i) or command.startswith(">(", i):
            # Process substitution: Bash runs the body as a separate process and
            # passes gws a single ``/dev/fd/N`` path — the body words (which may
            # include ``--draft`` or a ``delete`` method) are NOT gws argv. Mask
            # it as the single-word (quoted-style) marker so it can sit as a flag
            # value without being read as those tokens, and recurse into the body
            # so a gws call hidden inside ``<(gws ...)`` is still surfaced. Placed
            # before ``normalize_shell_operators`` runs, so its redirection
            # matcher never sees the ``<``/``>`` of the ``<(``/``>(`` opener.
            body, next_index = _read_paren_body(command, i + 2)
            if body is not None:
                substitutions.append(body)
                out.append(COMMAND_SUBSTITUTION_MARKER)
                i = next_index
                continue
        if command.startswith("$(", i):
            body, next_index = _read_dollar_paren_body(command, i + 2)
            if body is not None:
                substitutions.append(body)
                # Glue the marker into the surrounding word — Bash concatenates
                # an adjacent substitution into the SAME argument, so splitting
                # it off (`--to=alice@upstart.com$(x)` -> two tokens) would let
                # policy checks verify only the literal prefix and miss the
                # substituted suffix.
                out.append(COMMAND_SUBSTITUTION_MARKER_UNQUOTED)
                i = next_index
                continue
        if ch == "`":
            body, next_index = _read_backtick_body(command, i + 1)
            if body is not None:
                substitutions.append(body)
                out.append(COMMAND_SUBSTITUTION_MARKER_UNQUOTED)
                i = next_index
                continue
        if command.startswith("${", i):
            # Unquoted parameter expansion word-splits, so it can inject any
            # number of argv tokens (``${FLAGS}`` -> ``--draft=false``). Mask it
            # as the unquoted wildcard and recurse the body for a nested command
            # substitution in a default/alternate value.
            body, next_index = _read_brace_param(command, i + 2)
            if body is not None:
                _, inner = mask_command_substitutions(body)
                substitutions.extend(inner)
                out.append(COMMAND_SUBSTITUTION_MARKER_UNQUOTED)
                i = next_index
                continue
        if ch == "$":
            param = _PARAM_EXPANSION_RE.match(command, i)
            if param:
                out.append(COMMAND_SUBSTITUTION_MARKER_UNQUOTED)
                i = param.end()
                continue
        out.append(ch)
        i += 1
    return "".join(out), substitutions


def _read_brace_group(command: str, body_start: int) -> tuple[Optional[str], int]:
    """Read a ``{...}`` group starting after the opening brace (quote/nest-aware)."""
    depth = 1
    i = body_start
    n = len(command)
    quote: Optional[str] = None
    while i < n:
        ch = command[i]
        if quote is not None:
            if ch == "\\" and i + 1 < n and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return command[body_start:i], i + 1
            i += 1
            continue
        i += 1
    return None, body_start


def _brace_group_expands(body: str) -> bool:
    """True when Bash would brace-expand ``{body}`` into multiple words.

    Expansion is triggered only by a top-level comma (``{a,b}``) or a ``..``
    range (``{1..5}``). A group with neither (``{}``, ``{foo}``) is left literal
    by Bash, so it need not be masked. Nested braces and quoted commas do not
    count toward the top-level test.
    """
    depth = 0
    i = 0
    n = len(body)
    quote: Optional[str] = None
    has_comma = False
    while i < n:
        ch = body[i]
        if quote is not None:
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif depth == 0 and ch == ",":
            has_comma = True
        elif depth == 0 and ch == "." and i + 1 < n and body[i + 1] == ".":
            return True
        i += 1
    return has_comma


def mask_unquoted_expansions(command: str) -> str:
    """Replace unquoted brace expansions and glob patterns with a wildcard marker.

    Two more Bash word expansions happen on unquoted text before pflag parsing,
    each producing argv the static scan cannot predict:

    * Brace expansion (``{a,b}`` -> ``a b``, ``{1..3}`` -> ``1 2 3``): so
      ``gws drive files {dele,}te`` runs the ``delete`` method even though the
      literal token is ``{dele,}te``. Only groups that actually expand (a
      top-level comma or ``..`` range) are masked; ``{}``/``{foo}`` are left
      literal, as Bash leaves them.
    * Pathname (glob) expansion (``*``, ``?``, ``[...]``): ``--draft *`` can
      expand to a file named ``--draft=false`` and flip the last-occurrence draft
      state to a live send. The filesystem is invisible here, so any unquoted
      glob metacharacter is unverifiable.

    Both are replaced with the unquoted command-substitution wildcard marker,
    which the policy scans already treat as unverifiable and fail closed on.
    Quoted text (Bash performs neither expansion inside quotes) and ``${VAR}``
    parameter expansions are left untouched.

    Runs after :func:`mask_command_substitutions`, so ``$(...)`` / ``<(...)`` /
    ``${...}`` bodies are already markers and out of scope here.
    """
    out: list[str] = []
    i = 0
    n = len(command)
    quote: Optional[str] = None
    while i < n:
        ch = command[i]
        if quote is not None:
            out.append(ch)
            if ch == "\\" and i + 1 < n and quote == '"':
                out.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(ch)
            out.append(command[i + 1])
            i += 2
            continue
        if ch == "{" and not (out and out[-1] == "$"):
            # ``${VAR}`` is parameter expansion, not brace expansion — the `$`
            # guard skips it. A bare word-start or mid-word `{` may expand.
            body, next_index = _read_brace_group(command, i + 1)
            if body is not None and _brace_group_expands(body):
                out.append(COMMAND_SUBSTITUTION_MARKER_UNQUOTED)
                i = next_index
                continue
        if ch in "@!+" and i + 1 < n and command[i + 1] == "(":
            # extglob openers ``@(`` ``!(`` ``+(`` are pathname expansion just like
            # ``*``. ``?(`` and ``*(`` were already covered incidentally by their
            # leading metacharacter; these three were not, so an extglob pattern
            # expanded to a file named ``--cc=ext@evil.com`` unnoticed. Mask the
            # whole group so the token fails closed.
            body, next_index = _read_paren_body(command, i + 2)
            if body is not None:
                out.append(COMMAND_SUBSTITUTION_MARKER_UNQUOTED)
                i = next_index
                continue
        if ch in "*?[":
            # Unquoted glob metacharacter: pathname expansion can turn this word
            # into any file name(s) in the CWD (or leave it literal if none
            # match). Unverifiable — mask it so the whole token fails closed.
            out.append(COMMAND_SUBSTITUTION_MARKER_UNQUOTED)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def remove_line_continuations(command: str) -> str:
    """Collapse Bash backslash-newline line continuations before tokenizing.

    Bash removes a ``\\``-newline pair before word splitting, joining the
    surrounding text — so ``del\\<newline>ete`` executes the ``delete`` method.
    The scanners must see the joined word, not the split one. Line continuation
    works in unquoted text and inside double quotes, but NOT inside single
    quotes (where the pair is literal), so this pass is quote-aware.
    """
    out: list[str] = []
    i = 0
    n = len(command)
    quote: Optional[str] = None
    while i < n:
        ch = command[i]
        if quote == "'":
            out.append(ch)
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n and command[i + 1] in "\n\r":
            # Drop the backslash + the newline (consume a trailing \n of a \r\n).
            i += 2
            if command[i - 1] == "\r" and i < n and command[i] == "\n":
                i += 1
            continue
        if quote == '"':
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(command[i + 1])
                i += 2
                continue
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(ch)
            out.append(command[i + 1])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def normalize_shell_operators(command: str) -> str:
    """Insert spaces around shell control operators and turn newlines into ``;``.

    Defense-in-depth: although ``shlex.shlex(..., punctuation_chars=True)``
    already separates operators, this explicit pre-pass guarantees
    operator-adjacent verbs (``delete&&echo``) cannot fuse into a single token,
    and — critically — converts unquoted, unescaped newlines into a ``;``
    separator token. Without the newline conversion, a multiline Bash payload
    such as ``gws gmail +send --to ext@example.com --body Hi\\necho --draft``
    would let a ``--draft`` on a *later* line leak into the send command's
    argument stream (shlex treats a bare newline as ordinary whitespace, not a
    command separator).

    This is an escape-aware char-by-char state machine: it preserves single- and
    double-quoted spans verbatim (so operator characters and newlines inside a
    quoted value are untouched) and understands backslash escapes, including a
    backslash-newline line continuation (which is preserved, not converted to a
    separator). Operators are matched longest-first via
    :data:`_SHELL_OPERATOR_PATTERN` so ``&&``/``||``/``;;`` win over their
    single-character counterparts.

    Command substitutions are masked before this function runs and inspected
    recursively, so substitution bodies cannot leak into the outer stream.
    """
    out: list[str] = []
    i = 0
    n = len(command)
    quote: Optional[str] = None
    while i < n:
        ch = command[i]
        if quote is not None:
            if ch == "\\" and i + 1 < n and quote == '"':
                # In double quotes a backslash can escape the next char.
                out.append(ch)
                out.append(command[i + 1])
                i += 2
                continue
            if ch in _QUOTED_OPERATOR_MARKERS:
                out.append(_QUOTED_OPERATOR_MARKERS[ch])
                i += 1
                continue
            out.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "$" and i + 1 < n and command[i + 1] == "'":
            # Bash ANSI-C quoting: the span is dequoted AND its escape
            # sequences are decoded before execution, so ``$'gws'`` runs
            # ``gws`` and ``$'del\x65te'`` runs ``delete``. Decode it the same
            # way and re-emit as an ordinary single-quoted word so shlex (and
            # every downstream scan) sees what Bash will execute. Undecodable
            # or unterminated spans raise ValueError, which split_command
            # converts into a fail-closed parse error for gws commands.
            decoded, next_index = _read_ansi_c_quoted(command, i + 2)
            decoded = "".join(_QUOTED_OPERATOR_MARKERS.get(char, char) for char in decoded)
            out.append("'" + decoded.replace("'", "'\\''") + "'")
            i = next_index
            continue
        if ch == "$" and i + 1 < n and command[i + 1] == '"':
            # Bash locale quoting ($"..."): the ``$`` is dropped and the span
            # behaves as ordinary double quotes — mirror that so ``$"gws"``
            # is still recognized as the gws command word.
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            # Preserve backslash escapes (e.g. ``\&`` and backslash-newline line
            # continuations) without splitting them.
            if command[i + 1] in _QUOTED_OPERATOR_MARKERS:
                out.append(_QUOTED_OPERATOR_MARKERS[command[i + 1]])
                i += 2
                continue
            out.append(ch)
            out.append(command[i + 1])
            i += 2
            continue
        if ch in "\n\r":
            # An unquoted, unescaped newline separates two commands. Emit an
            # explicit ``;`` token so the downstream arg-list scan stops here
            # instead of absorbing the next line's tokens.
            if out and not out[-1].isspace():
                out.append(" ")
            out.append(";")
            i += 1
            if i < n and not command[i].isspace():
                out.append(" ")
            continue
        if ch == "#" and (not out or out[-1].isspace()):
            # Bash starts a comment only when ``#`` BEGINS a word; a glued
            # ``foo#`` is part of the word. Skip to end-of-line here (the
            # newline branch above then emits the ``;`` separator) and disable
            # shlex's own commenter in split_command — shlex would drop a
            # glued ``#`` AND everything after it, hiding later arguments
            # (``--to alice@upstart.com# --cc evil@example.com``) that Bash
            # still passes to gws.
            while i < n and command[i] not in "\n\r":
                i += 1
            continue
        redirection = _REDIRECTION_OPERATOR_PATTERN.match(command, i)
        if redirection:
            # A redirection operator is NOT a command separator: Bash consumes
            # only its target word and keeps passing later words to gws. Emit it
            # as its own spaced token (so an adjacent verb like ``delete&>x``
            # cannot fuse into one word), but because it is absent from
            # COMMAND_SEPARATORS the gws argument scan continues past it and
            # still sees the trailing flags/methods. Matched before the
            # command-separator pattern so a compound operator is not mis-split
            # at its embedded ``&``/``|`` (``&>`` -> ``& >``), which would
            # otherwise terminate the invocation early and fail open.
            matched = redirection.group(0)
            # An adjacent decimal IO number or dynamic fd allocation is shell
            # redirection syntax, not argv (``2>file`` / ``{fd}>file``). Remove
            # it from the normalized token stream so a leading fd redirect
            # cannot consume the command position before an expanded command
            # word. Spaced forms remain untouched because Bash treats their
            # prefix as a real word.
            io_number_start = i
            while io_number_start > 0 and command[io_number_start - 1].isdigit():
                io_number_start -= 1
            # In ``>&1>file`` / ``<&1>file``, the adjacent digits are the
            # TARGET of the preceding fd-duplication redirect, not an IO number
            # prefix for the following redirect. Keep them so each operator
            # consumes its own target in the normalized token stream.
            follows_fd_duplication = (
                io_number_start >= 2 and command[io_number_start - 1] == "&" and command[io_number_start - 2] in "<>"
            )
            if (
                io_number_start < i
                and not follows_fd_duplication
                and (
                    io_number_start == 0
                    or command[io_number_start - 1].isspace()
                    or command[io_number_start - 1] in ";&|("
                )
            ):
                del out[-(i - io_number_start) :]
            else:
                dynamic_fd = re.search(r"(?:^|[\s;&|(])(\{[A-Za-z_][A-Za-z0-9_]*\})$", command[:i])
                if dynamic_fd:
                    del out[-len(dynamic_fd.group(1)) :]
            # ``<<-`` (tab-stripping heredoc) behaves exactly like ``<<`` for
            # argv purposes, and its trailing ``-`` is not a shlex punctuation
            # char — shlex would split it back off (``<<`` + ``-``), leaving the
            # real delimiter as a separate surviving token. Collapse it to the
            # canonical ``<<`` so the delimiter is the immediately following
            # token and gets dropped with the operator.
            op = "<<" if matched == "<<-" else matched
            if out and not out[-1].isspace():
                out.append(" ")
            out.append(op)
            i += len(matched)
            if i < n and not command[i].isspace():
                out.append(" ")
            continue
        match = _SHELL_OPERATOR_PATTERN.match(command, i)
        if match:
            op = match.group(0)
            if out and not out[-1].isspace():
                out.append(" ")
            out.append(op)
            i += len(op)
            if i < n and not command[i].isspace():
                out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _heredoc_specs(command_line: str) -> list[tuple[str, bool]]:
    """Return literal heredoc delimiters and whether their receiver is a shell."""
    try:
        lexer = shlex.shlex(normalize_shell_operators(command_line), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = [_preserve_quoted_separator(token) for token in lexer]
    except ValueError:
        # Keep the original text when the header itself is malformed. The
        # normal parser will then fail closed if it could contain gws.
        return []

    specs: list[tuple[str, bool]] = []
    segment_start = 0
    for index, token in enumerate(tokens):
        if token in COMMAND_SEPARATORS:
            segment_start = index + 1
            continue
        if token != HEREDOC_OPERATOR or index + 1 >= len(tokens):
            continue

        segment: list[str] = []
        expecting_target = False
        for candidate in tokens[segment_start:index]:
            if expecting_target:
                expecting_target = False
                continue
            if candidate in REDIRECTION_OPERATORS:
                expecting_target = True
                continue
            segment.append(candidate)

        command_start = 0
        for position, candidate in enumerate(segment):
            # Reserved words and grouping syntax introduce a new simple command.
            # Keep only the final command leading into the heredoc.
            if candidate in COMMAND_WORD_INTRODUCERS:
                command_start = position + 1
        segment = segment[command_start:]

        while segment and ASSIGNMENT_WORD.match(segment[0]) is not None:
            segment.pop(0)
        receiver_is_shell = False
        if segment:
            first = basename(segment[0])
            if first in SHELL_EXECUTABLES:
                receiver_is_shell = True
            elif first in COMMAND_EXEC_WRAPPERS | ARG_APPENDING_WRAPPERS:
                receiver_is_shell = any(basename(candidate) in SHELL_EXECUTABLES for candidate in segment[1:])
        specs.append((_decode_quoted_operators(tokens[index + 1]), receiver_is_shell))
    return specs


def _strip_nonexecuted_heredoc_bodies(command: str) -> str:
    """Remove heredoc data unless the receiving command executes shell stdin."""
    out: list[str] = []
    pending: list[tuple[str, bool]] = []
    for line in command.splitlines(keepends=True):
        if pending:
            delimiter, preserve = pending[0]
            body_line = line.rstrip("\r\n")
            if preserve:
                out.append(line)
            else:
                # Preserve line boundaries so surrounding commands cannot fuse.
                out.append(line[len(body_line) :])
            if body_line.lstrip("\t") == delimiter:
                pending.pop(0)
            continue

        out.append(line)
        pending.extend(_heredoc_specs(line))
    return "".join(out)


def split_command(command: str) -> Union[list[str], tuple[str, str]]:
    """Tokenize ``command`` after operator/newline normalization.

    Returns the token list on success, or an ``("ERROR", message)`` TUPLE when
    the command mentions ``gws`` but cannot be parsed (so the caller fails
    closed). Callers must distinguish the two by type — a list is always a
    successful parse, even one whose first token happens to be ``ERROR``.
    A parse failure on a command that does *not* mention gws returns ``[]``.
    """
    try:
        normalized = normalize_shell_operators(command)
        lexer = shlex.shlex(normalized, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        # Comments are stripped Bash-correctly (word-start ``#`` only) by
        # normalize_shell_operators; shlex's commenter would also discard a
        # glued mid-word ``#`` and every argument after it.
        lexer.commenters = ""
        return [_preserve_quoted_separator(token) for token in lexer]
    except ValueError as exc:
        # Also fail closed when the text carries an expansion marker: the command
        # word itself may be BUILT by that expansion (``T=$(printf %s%s gw s);
        # $T drive files delete``, or a plain ``$TOOL ...``), so the literal
        # ``gws`` never appears and the mention probe cannot see it. Paired with a
        # tokenizer error — e.g. an apostrophe inside a heredoc body, which Bash
        # accepts and shlex rejects — that combination returned ALLOW for a real
        # external send and a real delete. ``split_command`` receives the masked
        # text, so the marker is visible here.
        if mentions_gws(command) or has_substitution_marker(command):
            return ("ERROR", str(exc))
        return []


# Shell invocation options that consume the NEXT token as their argument.
# Without these, `bash -o pipefail -c "gws ..."` would stop scanning at
# `pipefail` and never find the -c command string.
_SHELL_STARTUP_FILE_OPTIONS = {"--rcfile", "--init-file"}
_SHELL_OPTIONS_WITH_ARGS = {"-o", "+o", "-O", "+O"} | _SHELL_STARTUP_FILE_OPTIONS


def shell_execution_operand(tokens: list[str], shell_index: int) -> tuple[Optional[str], bool]:
    """Return a shell's command/script operand and whether it belongs to ``-c``."""
    index = shell_index + 1
    while index < len(tokens):
        shell_arg = tokens[index]
        if shell_arg == "--":
            return (tokens[index + 1], False) if index + 1 < len(tokens) else (None, False)
        if shell_arg in _SHELL_OPTIONS_WITH_ARGS:
            if (
                shell_arg in _SHELL_STARTUP_FILE_OPTIONS
                and index + 1 < len(tokens)
                and has_substitution_marker(tokens[index + 1])
            ):
                raise ShellParseError("shell startup file is built by an expansion")
            index += 2
            continue
        if shell_arg.startswith("--"):
            option, separator, value = shell_arg.partition("=")
            if option in _SHELL_STARTUP_FILE_OPTIONS and separator and has_substitution_marker(value):
                raise ShellParseError("shell startup file is built by an expansion")
            # Long option (--norc, --posix, --rcfile=FILE): never a -c
            # cluster, so the `c` characters inside the option NAME must not
            # be misread as -c (`bash --norc -c "..."` would otherwise treat
            # the literal token `-c` as the command string).
            index += 1
            continue
        if shell_arg.startswith("+") and len(shell_arg) > 1:
            # `+O histexpand` style toggles (argument forms handled above).
            index += 1
            continue
        if shell_arg.startswith("-") and len(shell_arg) > 1:
            if "c" in shell_arg[1:]:
                return (tokens[index + 1], True) if index + 1 < len(tokens) else (None, True)
            index += 1
            continue
        if shell_arg in REDIRECTION_OPERATORS:
            # A redirection operator is NOT a script operand. Returning it as one
            # made ``bash <<< 'gws ... delete'`` look like it ran a script called
            # ``<<<``, so the real program text was dropped as a redirect target
            # and the gws call inside was never inspected.
            return None, False
        return shell_arg, False
    return None, False


def substitution_wildcards(args: list[str], strict_quoted_value_flags: frozenset[str] = frozenset()) -> list[str]:
    """Return the argument tokens whose runtime value a substitution makes unverifiable.

    A substitution marker is a *wildcard*: the hook cannot know what Bash will
    expand it to. Where that matters depends on quoting and position:

    * An UNQUOTED substitution word-splits, so it can expand into any number of
      argv tokens (``$(x)`` may become ``--cc evil@ext.com``). It is
      unverifiable in EVERY position, including as a value-taking flag's value
      (the expansion can spill past the value slot).
    * A QUOTED substitution stays a single word. Consumed as a value-taking
      flag's value it is normally a plain value. Callers can name flags whose
      opaque values are still security-sensitive in ``strict_quoted_value_flags``.
      In flag/positional position that single word could BE any flag, verb, or method
      (``gws drive files "$(x)" abc`` may run ``delete``), so it is
      unverifiable there.
    * A redirection target is consumed by the shell, never by gws, so
      substitutions there are ignored.

    Callers must fail closed (deny) when this returns a non-empty list.
    """
    found: list[str] = []
    index = 0
    end_of_options = False
    while index < len(args):
        arg = args[index]
        if arg in REDIRECTION_OPERATORS:
            index += 2
            continue
        if not end_of_options:
            if arg == "--":
                end_of_options = True
                index += 1
                continue
            if arg in VALUE_TAKING_FLAGS:
                value = args[index + 1] if index + 1 < len(args) else None
                quoted_value_is_strict = arg in strict_quoted_value_flags and value is not None
                if value is not None and (
                    COMMAND_SUBSTITUTION_MARKER_UNQUOTED in value
                    or (quoted_value_is_strict and COMMAND_SUBSTITUTION_MARKER in value)
                ):
                    found.append(value)
                index += 2
                continue
            if arg.startswith("-") and len(arg) > 1:
                name, separator, value = arg.partition("=")
                quoted_value_is_strict = name in strict_quoted_value_flags and bool(separator)
                if (
                    COMMAND_SUBSTITUTION_MARKER_UNQUOTED in arg
                    or COMMAND_SUBSTITUTION_MARKER in name
                    or (quoted_value_is_strict and COMMAND_SUBSTITUTION_MARKER in value)
                ):
                    found.append(arg)
                index += 1
                continue
        if COMMAND_SUBSTITUTION_MARKER_UNQUOTED in arg or COMMAND_SUBSTITUTION_MARKER in arg:
            found.append(arg)
        index += 1
    return found


def _iter_find_commands(tokens: list[str], find_index: int) -> Iterator[list[str]]:
    """Yield command argv embedded in a ``find`` expression.

    ``-exec``/``-execdir``/``-ok``/``-okdir`` consume a command through ``;``.
    The batched ``-exec ... {} +`` form instead ends at the ``+`` immediately
    following ``{}``. Continue after each terminator because one expression can
    contain multiple command actions.
    """
    index = find_index + 1
    while index < len(tokens):
        argument = tokens[index]
        if argument in COMMAND_SEPARATORS:
            return
        if argument not in FIND_COMMAND_ACTIONS:
            index += 1
            continue

        command: list[str] = []
        index += 1
        while index < len(tokens):
            argument = tokens[index]
            if _decode_quoted_operators(argument) == ";" or (argument == "+" and command and command[-1] == "{}"):
                break
            if argument in COMMAND_SEPARATORS:
                break
            command.append(argument)
            index += 1
        if command:
            if any("{}" in argument for argument in command):
                # find substitutes matching paths into these operands at runtime.
                # Mark the eventual gws argv as unverifiable, just as xargs does
                # for argv appended from stdin.
                command.append(COMMAND_SUBSTITUTION_MARKER_UNQUOTED)
            yield command
        if index >= len(tokens) or (argument in COMMAND_SEPARATORS and argument != ";"):
            return
        index += 1


def iter_gws_invocations(
    command: str,
    depth: int = 0,
    _aliases: Optional[dict[str, str]] = None,
) -> Iterator[list[str]]:
    """Yield the argument list of every ``gws`` invocation reachable in ``command``.

    Recurses into command substitutions, shell ``-c`` wrappers, ``eval``, alias
    expansions, and commands embedded in ``find`` actions so a gws call hidden
    inside any of them is still surfaced. Each yielded list contains the tokens
    after ``gws`` up to (but not including) the next command separator.

    Raises :class:`ShellParseError` if a gws-containing command cannot be
    tokenized or the nesting depth is exceeded, so callers fail closed.
    """
    # Decode ANSI-C ``$'...'`` spans FIRST so every later pass sees only plain
    # quoting. ``$'...'`` escapes its own closing quote (``$'it\'s'``), which the
    # other passes would misread as a close+reopen and then spend the rest of the
    # command believing they are inside quotes — emitting no expansion markers at
    # all. An unterminated span is unparseable, so fail closed.
    try:
        command = decode_ansi_c_quoting(command)
    except ValueError as exc:
        # Fail closed unconditionally: the gws-mention probe cannot be trusted
        # here because the command word may itself be BUILT by the span we just
        # failed to decode (``gw$'\x73'`` is ``gws``), so the pre-decode text
        # need not contain ``gws`` at all. An undecodable ``$'...'`` is an
        # unterminated span, which Bash rejects too, so denying costs nothing.
        raise ShellParseError(str(exc)) from exc

    # Bash strips backslash-newline line continuations before tokenizing, so
    # collapse them first (``del\<newline>ete`` -> ``delete``) — otherwise a
    # verb/method split across lines escapes the token scans.
    command = remove_line_continuations(command)

    # Bash recursively evaluates variable values in arithmetic expressions. A
    # stored array index can therefore execute a command substitution that is
    # invisible in the live expression. Pair the evaluated variable with its
    # gws-bearing assignment so unrelated arithmetic remains inspectable.
    if has_indirect_arithmetic_expansion(command):
        raise ShellParseError("gws command hidden behind recursive arithmetic variable evaluation")

    if depth > MAX_SHELL_RECURSION_DEPTH:
        # The command word itself can be built by an expansion, so absence of a
        # literal ``gws`` token does not prove this subtree is irrelevant.
        raise ShellParseError("nested shell command depth exceeded")

    aliases = {} if _aliases is None else _aliases

    masked_command, substitutions = mask_command_substitutions(command)
    for substitution in substitutions:
        yield from iter_gws_invocations(substitution, depth + 1, aliases)

    masked_command = mask_unquoted_expansions(masked_command)
    masked_command = _strip_nonexecuted_heredoc_bodies(masked_command)
    tokens = split_command(masked_command)
    if isinstance(tokens, tuple):
        raise ShellParseError(tokens[1])

    at_command_word = True
    command_wrapper: Optional[str] = None
    expecting_redirection_target = False
    expecting_wrapper_option_value: Optional[str] = None
    wrapper_options_ended = False
    wrapper_operand_pending = False
    wrapper_nonexecuting = False
    watch_exec_direct = False
    arg_appending = False
    parallel_command_list = False
    xargs_replacement: Optional[str] = None
    stdin_from_pipe = False
    expansion_built_bash_env = False
    for index, token in enumerate(tokens):
        if token in COMMAND_SEPARATORS:
            at_command_word = True
            command_wrapper = None
            expecting_redirection_target = False
            expecting_wrapper_option_value = None
            wrapper_options_ended = False
            wrapper_operand_pending = False
            wrapper_nonexecuting = False
            watch_exec_direct = False
            arg_appending = False
            parallel_command_list = False
            xargs_replacement = None
            stdin_from_pipe = token == PIPE_OPERATOR
            expansion_built_bash_env = False
            continue
        if expecting_redirection_target:
            expecting_redirection_target = False
            continue
        if token in REDIRECTION_OPERATORS:
            # Redirections can appear before the command word; both the
            # operator and its target are shell syntax, not the command.
            expecting_redirection_target = True
            continue
        if parallel_command_list:
            # moreutils parallel treats every operand after ``--`` as a
            # complete shell command when no command precedes the marker.
            yield from iter_gws_invocations(token, depth + 1, aliases)
        command_word = at_command_word
        command_prefix = False
        if command_word:
            base = basename(token)
            exec_option, exec_option_takes_value = _exec_wrapper_option(token)
            env_option, env_option_takes_value = _env_wrapper_option(token)
            env_split_string, env_split_string_value = _env_split_string_option(token)
            sudo_option, sudo_option_takes_value = _sudo_wrapper_option(token)
            ionice_option, ionice_option_takes_value, ionice_nonexecuting = _ionice_wrapper_option(token)
            chrt_option, chrt_option_takes_value, chrt_nonexecuting = _chrt_wrapper_option(token)
            arch_option, arch_option_takes_value, arch_nonexecuting = _arch_wrapper_option(token)
            caffeinate_option, caffeinate_option_takes_value = _caffeinate_wrapper_option(token)
            nice_option, nice_option_takes_value = _nice_wrapper_option(token)
            nohup_option = _nohup_wrapper_option(token)
            script_option, script_option_takes_value, script_nonexecuting = _script_wrapper_option(token)
            setsid_option = _setsid_wrapper_option(token)
            stdbuf_option, stdbuf_option_takes_value = _stdbuf_wrapper_option(token)
            (
                xargs_option,
                xargs_option_takes_value,
                xargs_replacement_option,
                xargs_replacement_value,
            ) = _xargs_wrapper_option(token)
            parallel_option, parallel_option_takes_value = _parallel_wrapper_option(token)
            time_option, time_option_takes_value = _time_wrapper_option(token)
            timeout_option, timeout_option_takes_value = _timeout_wrapper_option(token)
            watch_option, watch_option_takes_value = _watch_wrapper_option(token)
            if expecting_wrapper_option_value:
                if expecting_wrapper_option_value == "env-split-string":
                    split_command_text = token
                    trailing = _collect_invocation_args(tokens[index + 1 :])
                    if trailing:
                        split_command_text = f"{split_command_text} {shlex.join(trailing)}"
                    yield from iter_gws_invocations(split_command_text, depth + 1, aliases)
                elif expecting_wrapper_option_value == "xargs-replacement":
                    xargs_replacement = token or None
                # ``exec -a NAME command``: NAME is argv[0], not the command.
                expecting_wrapper_option_value = None
                command_prefix = True
            elif command_wrapper in {"builtin", "command"} and not wrapper_options_ended and token == END_OF_OPTIONS:
                command_prefix = True
                wrapper_options_ended = True
            elif command_wrapper == "command" and not wrapper_options_ended and _is_command_wrapper_option(token):
                command_prefix = True
            elif command_wrapper == "exec" and not wrapper_options_ended and token == END_OF_OPTIONS:
                command_prefix = True
                wrapper_options_ended = True
            elif command_wrapper == "exec" and not wrapper_options_ended and exec_option:
                command_prefix = True
                expecting_wrapper_option_value = "wrapper-option" if exec_option_takes_value else None
            elif command_wrapper == "env" and not wrapper_options_ended and token == END_OF_OPTIONS:
                command_prefix = True
                wrapper_options_ended = True
            elif command_wrapper == "env" and not wrapper_options_ended and env_option:
                command_prefix = True
                if env_split_string and env_split_string_value is not None:
                    split_command_text = env_split_string_value
                    trailing = _collect_invocation_args(tokens[index + 1 :])
                    if trailing:
                        split_command_text = f"{split_command_text} {shlex.join(trailing)}"
                    yield from iter_gws_invocations(split_command_text, depth + 1, aliases)
                elif env_split_string:
                    expecting_wrapper_option_value = "env-split-string"
                else:
                    expecting_wrapper_option_value = "wrapper-option" if env_option_takes_value else None
            elif command_wrapper == "sudo" and not wrapper_options_ended and token == END_OF_OPTIONS:
                command_prefix = True
                wrapper_options_ended = True
            elif command_wrapper == "sudo" and not wrapper_options_ended and sudo_option:
                command_prefix = True
                expecting_wrapper_option_value = "wrapper-option" if sudo_option_takes_value else None
            elif command_wrapper == "ionice" and not wrapper_options_ended and token == END_OF_OPTIONS:
                command_prefix = True
                wrapper_options_ended = True
            elif command_wrapper == "ionice" and not wrapper_options_ended and ionice_option:
                if ionice_nonexecuting:
                    command_wrapper = None
                    expecting_wrapper_option_value = None
                    wrapper_nonexecuting = True
                else:
                    command_prefix = True
                    expecting_wrapper_option_value = "wrapper-option" if ionice_option_takes_value else None
            elif command_wrapper == "arch" and not wrapper_options_ended and arch_option:
                if arch_nonexecuting:
                    command_wrapper = None
                    expecting_wrapper_option_value = None
                    wrapper_nonexecuting = True
                else:
                    command_prefix = True
                    expecting_wrapper_option_value = "wrapper-option" if arch_option_takes_value else None
            elif command_wrapper == "caffeinate" and not wrapper_options_ended and caffeinate_option:
                command_prefix = True
                expecting_wrapper_option_value = "wrapper-option" if caffeinate_option_takes_value else None
            elif command_wrapper == "chrt" and not wrapper_options_ended and token == END_OF_OPTIONS:
                command_prefix = True
                wrapper_options_ended = True
            elif command_wrapper == "chrt" and not wrapper_options_ended and chrt_option:
                if chrt_nonexecuting:
                    command_wrapper = None
                    expecting_wrapper_option_value = None
                    wrapper_operand_pending = False
                    wrapper_nonexecuting = True
                else:
                    command_prefix = True
                    expecting_wrapper_option_value = "wrapper-option" if chrt_option_takes_value else None
            elif command_wrapper == "chrt" and wrapper_operand_pending and re.fullmatch(r"[+-]?\d+", token):
                # chrt's first non-option operand is a numeric priority when
                # present. Some policies allow it to be omitted, in which case
                # a non-numeric word is the command and must stay inspectable.
                command_prefix = True
                wrapper_operand_pending = False
            elif command_wrapper == "nice" and not wrapper_options_ended and token == END_OF_OPTIONS:
                command_prefix = True
                wrapper_options_ended = True
            elif command_wrapper == "nice" and not wrapper_options_ended and nice_option:
                command_prefix = True
                expecting_wrapper_option_value = "wrapper-option" if nice_option_takes_value else None
            elif command_wrapper == "nohup" and not wrapper_options_ended and token == END_OF_OPTIONS:
                command_prefix = True
                wrapper_options_ended = True
            elif command_wrapper == "nohup" and not wrapper_options_ended and nohup_option:
                command_prefix = True
            elif command_wrapper == "script" and not wrapper_options_ended and token == END_OF_OPTIONS:
                command_prefix = True
                wrapper_options_ended = True
            elif command_wrapper == "script" and not wrapper_options_ended and script_option:
                if script_nonexecuting:
                    command_wrapper = None
                    expecting_wrapper_option_value = None
                    wrapper_operand_pending = False
                    wrapper_nonexecuting = True
                else:
                    command_prefix = True
                    expecting_wrapper_option_value = "wrapper-option" if script_option_takes_value else None
            elif command_wrapper == "script" and wrapper_operand_pending:
                if has_substitution_marker(token):
                    raise ShellParseError("script output file and command are built by an expansion")
                # macOS script's first operand is the typescript output file;
                # an optional command and its argv begin at the following word.
                command_prefix = True
                wrapper_operand_pending = False
            elif command_wrapper == "setsid" and not wrapper_options_ended and token == END_OF_OPTIONS:
                command_prefix = True
                wrapper_options_ended = True
            elif command_wrapper == "setsid" and not wrapper_options_ended and setsid_option:
                command_prefix = True
            elif command_wrapper == "stdbuf" and not wrapper_options_ended and token == END_OF_OPTIONS:
                command_prefix = True
                wrapper_options_ended = True
            elif command_wrapper == "stdbuf" and not wrapper_options_ended and stdbuf_option:
                command_prefix = True
                expecting_wrapper_option_value = "wrapper-option" if stdbuf_option_takes_value else None
            elif command_wrapper == "xargs" and not wrapper_options_ended and token == END_OF_OPTIONS:
                command_prefix = True
                wrapper_options_ended = True
            elif command_wrapper == "xargs" and not wrapper_options_ended and xargs_option:
                command_prefix = True
                if xargs_replacement_option:
                    xargs_replacement = xargs_replacement_value
                    expecting_wrapper_option_value = (
                        "xargs-replacement" if xargs_option_takes_value and xargs_replacement_value is None else None
                    )
                else:
                    expecting_wrapper_option_value = "wrapper-option" if xargs_option_takes_value else None
            elif command_wrapper == "parallel" and token in {"--help", "--version"}:
                command_wrapper = None
            elif command_wrapper == "parallel" and not wrapper_options_ended and token == END_OF_OPTIONS:
                command_prefix = True
                wrapper_options_ended = True
                parallel_command_list = True
            elif command_wrapper == "parallel" and not wrapper_options_ended and parallel_option:
                command_prefix = True
                if token == PARALLEL_LOAD_OPTION:
                    # ``-l`` has an optional attached value in GNU parallel,
                    # but consumes the next word in moreutils parallel. Inspect
                    # both interpretations so neither executable can hide gws.
                    moreutils_command = _collect_invocation_args(tokens[index + 2 :])
                    if moreutils_command:
                        moreutils_command.append(COMMAND_SUBSTITUTION_MARKER_UNQUOTED)
                        yield from iter_gws_invocations(shlex.join(moreutils_command), depth + 1, aliases)
                expecting_wrapper_option_value = "wrapper-option" if parallel_option_takes_value else None
            elif command_wrapper == "time" and not wrapper_options_ended and token == END_OF_OPTIONS:
                command_prefix = True
                wrapper_options_ended = True
            elif command_wrapper == "time" and not wrapper_options_ended and time_option:
                command_prefix = True
                expecting_wrapper_option_value = "wrapper-option" if time_option_takes_value else None
            elif command_wrapper == "timeout" and token in {"--help", "--version"}:
                wrapper_operand_pending = False
            elif command_wrapper == "timeout" and not wrapper_options_ended and token == END_OF_OPTIONS:
                command_prefix = True
                wrapper_options_ended = True
            elif command_wrapper == "timeout" and not wrapper_options_ended and timeout_option:
                command_prefix = True
                expecting_wrapper_option_value = "wrapper-option" if timeout_option_takes_value else None
            elif command_wrapper == "timeout" and wrapper_operand_pending:
                # timeout's first non-option operand is the duration; the command
                # starts at the following word.
                command_prefix = True
                wrapper_operand_pending = False
            elif command_wrapper == "watch" and token in {"--help", "--version"}:
                command_wrapper = None
            elif command_wrapper == "watch" and not wrapper_options_ended and token == END_OF_OPTIONS:
                command_prefix = True
                wrapper_options_ended = True
            elif command_wrapper == "watch" and not wrapper_options_ended and watch_option:
                command_prefix = True
                expecting_wrapper_option_value = "wrapper-option" if watch_option_takes_value else None
                watch_exec_direct = (
                    watch_exec_direct
                    or token == WATCH_EXEC_OPTION
                    or (token.startswith("-") and not token.startswith("--") and "x" in token[1:])
                )
            elif base in COMMAND_EXEC_WRAPPERS:
                # Wrappers can nest (``command exec -a name command ...``).
                command_wrapper = base
                command_prefix = True
                wrapper_options_ended = False
                wrapper_operand_pending = base in {"chrt", "script", "timeout"}
                watch_exec_direct = False
            elif base in ARG_APPENDING_WRAPPERS:
                # The command it names gets extra argv appended from stdin.
                arg_appending = True
                command_wrapper = base
                wrapper_options_ended = False
                command_prefix = True
                if base == "xargs":
                    xargs_replacement = None
            elif command_wrapper in {None, "env", "sudo"} and ASSIGNMENT_WORD.match(token) is not None:
                command_prefix = True
                raw_name, value = token.split("=", 1)
                if raw_name.removesuffix("+") == "BASH_ENV" and has_substitution_marker(value):
                    expansion_built_bash_env = True
                if command_wrapper == "env":
                    wrapper_options_ended = True

        actual_command_word = command_word and not command_prefix
        if actual_command_word and xargs_replacement and xargs_replacement in token:
            raise ShellParseError("xargs replaces its command word from stdin")
        if actual_command_word and command_wrapper == "watch" and not watch_exec_direct:
            watch_command = " ".join(_collect_invocation_args(tokens[index:]))
            if watch_command:
                yield from iter_gws_invocations(watch_command, depth + 1, aliases)
        if actual_command_word and base in SHELL_EXECUTABLES:
            if base == "bash" and expansion_built_bash_env:
                raise ShellParseError("BASH_ENV startup file is built by an expansion")
            operand, is_command_string = shell_execution_operand(tokens, index)
            if is_command_string and operand and xargs_replacement and xargs_replacement in operand:
                raise ShellParseError("xargs replaces a shell command string from stdin")
            if operand is not None and has_substitution_marker(operand):
                kind = "shell -c command string" if is_command_string else "shell script operand"
                raise ShellParseError(f"{kind} is built by an expansion")
            if is_command_string and operand:
                yield from iter_gws_invocations(operand, depth + 1, aliases)

            later = index + 1
            while later < len(tokens) and tokens[later] not in COMMAND_SEPARATORS:
                shell_arg = tokens[later]
                if shell_arg in ("<<<", "<<"):
                    program = tokens[later + 1] if later + 1 < len(tokens) else None
                    if shell_arg == "<<<" and program and has_substitution_marker(program):
                        raise ShellParseError("shell here-string program is built by an expansion")
                    if program:
                        yield from iter_gws_invocations(program, depth + 1, aliases)
                    break
                if shell_arg == "<":
                    program = tokens[later + 1] if later + 1 < len(tokens) else None
                    if program and has_substitution_marker(program):
                        raise ShellParseError("shell stdin redirection target is built by an expansion")
                    break
                if shell_arg == "--":
                    break
                if shell_arg in _SHELL_OPTIONS_WITH_ARGS:
                    later += 2
                    continue
                if shell_arg.startswith("-") and not shell_arg.startswith("--"):
                    if "s" in shell_arg[1:]:
                        raise ShellParseError("shell reads its program from stdin, which cannot be inspected")
                    later += 1
                    continue
                if shell_arg.startswith("+"):
                    later += 1
                    continue
                break

            if stdin_from_pipe:
                # A producer can construct protected text without a literal
                # ``gws`` token (for example, printf format substitution). The
                # hook cannot recover arbitrary command output, so every shell
                # program supplied through a pipe must fail closed, including
                # when command wrappers appear between the pipe and the shell.
                raise ShellParseError("shell is piped a program whose output cannot be inspected")

        if actual_command_word and base in {"source", "."}:
            source_args = _collect_invocation_args(tokens[index + 1 :])
            if source_args and source_args[0] == "--":
                source_args = source_args[1:]
            if source_args and has_substitution_marker(source_args[0]):
                raise ShellParseError("sourced shell script is built by an expansion")

        if actual_command_word and base == "eval":
            eval_args = _collect_invocation_args(tokens[index + 1 :])
            if eval_args and eval_args[0] == END_OF_OPTIONS:
                eval_args = eval_args[1:]
            if eval_args:
                # eval concatenates its already-dequoted argv with spaces and
                # reparses that text as shell input.
                eval_arg = " ".join(eval_args)
                if has_substitution_marker(eval_arg):
                    raise ShellParseError("eval argument is built by an expansion")
                yield from iter_gws_invocations(eval_arg, depth + 1, aliases)

        if actual_command_word and base == "alias":
            for alias_arg in _collect_invocation_args(tokens[index + 1 :]):
                if alias_arg.startswith("-") or "=" not in alias_arg:
                    continue
                alias_name, alias_value = alias_arg.split("=", 1)
                if not alias_name:
                    continue
                if has_substitution_marker(alias_value):
                    raise ShellParseError("alias value is built by an expansion")
                aliases[alias_name] = alias_value
                yield from iter_gws_invocations(alias_value, depth + 1, aliases)

        if actual_command_word and base == "unalias":
            unalias_args = _collect_invocation_args(tokens[index + 1 :])
            if "-a" in unalias_args:
                aliases.clear()
            else:
                for alias_name in unalias_args:
                    if not alias_name.startswith("-"):
                        aliases.pop(alias_name, None)

        if actual_command_word and base == "trap":
            trap_args = _collect_invocation_args(tokens[index + 1 :])
            if trap_args and trap_args[0] in {"-l", "-p"}:
                trap_args = []
            if trap_args and trap_args[0] == "--":
                trap_args = trap_args[1:]
            if trap_args:
                trap_arg = trap_args[0]
                if has_substitution_marker(trap_arg):
                    raise ShellParseError("trap argument is built by an expansion")
                yield from iter_gws_invocations(trap_arg, depth + 1, aliases)

        if actual_command_word and base == "find":
            for find_command in _iter_find_commands(tokens, index):
                yield from iter_gws_invocations(shlex.join(find_command), depth + 1, aliases)

        if actual_command_word and token in aliases:
            alias_command = aliases[token]
            alias_args = _collect_invocation_args(tokens[index + 1 :])
            if alias_args:
                # Bash appends the invocation's words to the alias replacement
                # before parsing it. Preserve argv boundaries while rebuilding
                # that command so a bare ``alias x=gws`` cannot hide a later
                # destructive method or external Gmail recipient.
                alias_command = f"{alias_command} {shlex.join(alias_args)}"
            yield from iter_gws_invocations(alias_command, depth + 1, aliases)

        if not command_prefix:
            command_wrapper = None
            expecting_wrapper_option_value = None
            wrapper_options_ended = False
            wrapper_operand_pending = False
            watch_exec_direct = False
            xargs_replacement = None
        # The NEXT token is in command-word position when this one introduces a
        # new command (a reserved word / grouping operator); otherwise this token
        # is a command word and the ones after it are its arguments.
        at_command_word = (command_prefix or token in COMMAND_WORD_INTRODUCERS) and not wrapper_nonexecuting
        if actual_command_word and is_gws_token(token):
            invocation_args = _collect_invocation_args(tokens[index + 1 :])
            if arg_appending:
                # The literal tokens are only a PREFIX of the real argv — stdin
                # supplies the rest — so mark the stream unverifiable and let the
                # policy scans fail closed. Without this, an empty or benign
                # literal prefix read as a compliant command.
                invocation_args = invocation_args + [COMMAND_SUBSTITUTION_MARKER_UNQUOTED]
            yield invocation_args
        elif command_word and not command_prefix and has_substitution_marker(token):
            # A command WORD built from an expansion could itself BE `gws`
            # (``$(printf gws) gmail +send ...`` runs a real external send, and
            # ``${TOOL} drive files delete`` a real delete). Its runtime value is
            # unknown, so inspect the trailing tokens as a possible gws
            # invocation and let the policy scan fail closed on a send/
            # destructive shape.
            #
            # It could also be a shell executable. Reparse a visible ``-c``
            # operand before treating the trailing tokens as possible gws argv;
            # otherwise ``"$shell" -c 'gws drive files delete FILE'`` hides the
            # protected invocation inside one opaque argument.
            operand, is_command_string = shell_execution_operand(tokens, index)
            if is_command_string and operand is not None and has_substitution_marker(operand):
                raise ShellParseError("shell -c command string is built by an expansion")
            if is_command_string and operand:
                yield from iter_gws_invocations(operand, depth + 1, aliases)
            args = _collect_invocation_args(tokens[index + 1 :])
            if not args and COMMAND_SUBSTITUTION_MARKER_UNQUOTED in token:
                # An unquoted expansion may supply not only `gws`, but its
                # complete argv. Keep the wildcard when there are no literal
                # trailing arguments for either policy scanner to inspect.
                args.insert(0, token)
            yield args


def _collect_invocation_args(rest: list[str]) -> list[str]:
    """Collect one gws invocation's argv from the tokens following ``gws``.

    Stops at the first command separator, and drops each redirection operator
    together with its target word. Bash consumes a redirection target itself
    and never passes it to gws, so it must not remain in the argv the policy
    scans inspect: a target literally named ``--draft`` (``gws ... >--draft``)
    would otherwise be read as enabling draft mode — fail-OPEN for an external
    live send — and a target named ``delete`` as a destructive method. Only the
    target is skipped; the invocation itself continues past the redirection
    (that is what keeps a trailing ``--cc ext@example.com`` after ``&>`` in the
    scan). A malformed redirection with no target (end of the run, or a command
    separator next) drops only the operator.
    """
    args: list[str] = []
    index = 0
    while index < len(rest):
        arg = rest[index]
        if arg in COMMAND_SEPARATORS:
            break
        if arg in REDIRECTION_OPERATORS:
            index += 1
            if index < len(rest) and rest[index] not in COMMAND_SEPARATORS:
                index += 1
            continue
        args.append(_decode_quoted_operators(arg))
        index += 1
    return args
