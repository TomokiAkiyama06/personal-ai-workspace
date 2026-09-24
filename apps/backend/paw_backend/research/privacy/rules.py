"""The pure text functions of the Research Privacy Filter (PAW-053).

The docstrings are the specification (the tests check them). The implementation
is the Claude reference implementation: see "実装の由来" in the backend README.
"""

import hashlib
import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit

from paw_backend.research.privacy.contract import (
    MIN_HEX_HASH_CHARS,
    MIN_ID_DIGITS,
    MIN_OPAQUE_TOKEN_CHARS,
    PRIVATE_HOST_SUFFIXES,
)
from paw_backend.tools.credentials import REDACTED, redact_text


def _collapse(text):
    return " ".join(text.split())


def normalize_text(text):
    r"""The normal form of a text: compatibility-folded, printable, collapsed.

    In this order:

    1. Apply Unicode NFKC normalisation.
    2. Turn every character for which ``str.isspace()`` is true into a space.
    3. Drop every other character whose Unicode category is ``Cc`` (control) or
       ``Cf`` (format, for example zero-width characters and the BOM).
    4. Collapse and strip the spaces.

    Case is kept. The function is idempotent.

    ``"  Hello \n\t World  "`` -> ``"Hello World"``;
    ``"pass\u200bword"`` -> ``"password"``;
    ``"a\x00b"`` -> ``"ab"``;
    ``"ｆｕｌｌ\u3000ｗｉｄｔｈ"`` -> ``"full width"``;
    ``"\ufeff"`` -> ``""``; ``"ﬁle"`` -> ``"file"``.
    (The examples are Python string literals.)
    """
    text = unicodedata.normalize("NFKC", text)
    out = []
    for ch in text:
        if ch.isspace():
            out.append(" ")
        elif unicodedata.category(ch) in ("Cc", "Cf"):
            continue
        else:
            out.append(ch)
    return _collapse("".join(out))


def fold_for_match(text):
    r"""A fully case-folded copy of ``text``: ``ch.casefold()`` for every character.

    This is full Unicode case folding, not lower-casing, so text that differs only
    in case (or in the spelling of a case pair) folds to the same string. A
    character can EXPAND: ``"ß"`` and ``"ẞ"`` (U+1E9E) become ``"ss"``, ``"İ"``
    (U+0130) becomes ``"i"`` and a combining dot above (U+0307). Every character
    is folded on its own (this is what ``str.casefold`` does, so the result equals
    ``text.casefold()``); there is no context: both ``"σ"`` and a word-final
    ``"ς"`` fold to ``"σ"``. The result is never shorter than ``text``, and equals
    it in length exactly when no character expands. ``find_copied_spans`` relies on
    the one-character-at-a-time property to trace every folded character back to
    the original character it came from.

    ``"ABC def"`` -> ``"abc def"``; ``"ẞ"`` -> ``"ss"``; ``"Straße"`` ->
    ``"strasse"``; ``"ΟΔΟΣ"`` and ``"οδος"`` -> ``"οδοσ"``; ``"İ"`` ->
    ``"i\u0307"``; ``"ǅ"`` -> ``"ǆ"``; ``""`` -> ``""``. (The examples are Python
    string literals.)
    """
    return text.casefold()


def _fold_with_origin(text):
    """``(folded, origin)``: ``fold_for_match(text)`` and where it came from.

    ``origin[k]`` is the index in ``text`` of the character that folded character
    ``k`` comes from (an expanding character owns several folded characters).
    ``origin`` is ``None`` when nothing expanded: then the two indices are equal.
    """
    folded = text.casefold()
    if len(folded) == len(text):  # a fold never shortens a character to nothing
        return folded, None
    origin = []
    for index, ch in enumerate(text):
        origin.extend([index] * len(ch.casefold()))
    return folded, origin


def find_copied_spans(text, source, *, window):
    """The stretches of ``text`` that were copied from ``source``.

    ``window`` is an ``int`` of at least 1 (a smaller one raises ``ValueError``
    with a fixed message). Matching is case-insensitive in the full Unicode sense:
    compare ``folded_text = fold_for_match(text)`` with ``folded_source =
    fold_for_match(source)``, so ``"ß"`` in one and ``"SS"`` in the other are the
    same. ``window`` counts FOLDED characters. A folded position ``i`` is covered
    when some ``j`` with ``j <= i < j + window <= len(folded_text)`` has the
    ``window`` characters ``folded_text[j:j + window]`` occurring anywhere in
    ``folded_source``. A character of ``text`` is copied when ANY of the folded
    characters it produced is covered, so a character that is only partly matched
    (the window starts or ends in the middle of the ``"ss"`` of a ``"ß"``) counts
    as copied as a whole. Return the maximal runs of copied characters as
    ``(start, end)`` pairs (``end`` is exclusive), sorted, each run one pair, so
    no two pairs touch or overlap. The positions are positions in the ORIGINAL
    ``text``, never in the folded text. If ``window`` is larger than the folded
    text or than the folded source the result is ``()``. The cost must be linear in
    ``len(text) + len(source)`` for a fixed ``window`` (a set of the source's
    windows, not a search for every window).

    ``find_copied_spans("abcdef", "xxbcdexx", window=3)`` -> ``((1, 5),)``
    (the windows ``bcd`` and ``cde`` occur in the source; ``abc`` and ``def`` do not);
    ``find_copied_spans("ABC", "abc", window=3)`` -> ``((0, 3),)``;
    ``find_copied_spans("abXcd", "abYcd", window=2)`` -> ``((0, 2), (3, 5))``;
    ``find_copied_spans("abc", "abc", window=4)`` -> ``()``;
    ``find_copied_spans("abc", "xyz", window=1)`` -> ``()``;
    ``find_copied_spans("straße", "STRASSE", window=7)`` -> ``((0, 6),)`` (the
    folds are both ``"strasse"``; the span is in the 6 characters of the text);
    ``find_copied_spans("xxßabc", "xxs", window=3)`` -> ``((0, 3),)`` (the window
    ends inside the ``"ss"`` of the ``"ß"``, which is copied whole);
    ``find_copied_spans("ß", "ssss", window=3)`` -> ``()`` (the folded text has 2
    characters).
    """
    if window < 1:
        raise ValueError("window must be at least 1")
    folded_text, origin = _fold_with_origin(text)
    folded_source = fold_for_match(source)
    if window > len(folded_text) or window > len(folded_source):
        return ()
    known = {
        folded_source[i : i + window] for i in range(len(folded_source) - window + 1)
    }
    # Covered runs of folded positions, found left to right: a window that starts
    # inside or right after the last run extends it.
    runs = []
    for j in range(len(folded_text) - window + 1):
        if folded_text[j : j + window] in known:
            if runs and j <= runs[-1][1]:
                runs[-1][1] = j + window
            else:
                runs.append([j, j + window])
    if origin is None:
        return tuple((start, end) for start, end in runs)
    # Back to the original text: whole characters. Two runs can end up in one
    # character (the first and the last of the three characters of "\u0390"), so
    # merge again.
    spans = []
    for start, end in runs:
        first, last = origin[start], origin[end - 1] + 1
        if spans and first <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], last)
        else:
            spans.append([first, last])
    return tuple((start, end) for start, end in spans)


def strip_credentials(text):
    """Remove every credential that ``paw_backend.tools.credentials`` recognises.

    Call ``redacted, count = redact_text(text)``. If ``count`` is 0 return
    ``(text, 0)`` unchanged. Otherwise replace every occurrence of the marker
    ``paw_backend.tools.credentials.REDACTED`` in ``redacted`` with one space,
    collapse, and return ``(that text, count)``; the count is the one
    ``redact_text`` reported (it may be 2 for one credential that both a token
    pattern and an assignment pattern matched; do not correct it). The text of a
    matched credential must not survive anywhere in the result.

    ``f"use ghp_{'a' * 36} here"`` -> ``("use here", 1)``;
    ``"how to reset a password"`` -> unchanged,
    0; ``"DB_PASSWORD=hunter2hunter2 in .env"`` -> a text without ``hunter2hunter2``
    (the name ``DB_PASSWORD=`` stays) and a count of at least 1.
    """
    redacted, count = redact_text(text)
    if count == 0:
        return text, 0
    return _collapse(redacted.replace(REDACTED, " ")), count


def is_private_host(host):
    """Whether a host name (or address) names a private or unknown system.

    ``host`` is a host name without port and path. Lower-case it, remove ONE
    trailing ``.``, and remove one pair of surrounding ``[`` ``]``. Then the
    answer is True when any of these holds, else False:

    * it is empty;
    * it contains ``%``: a zone identifier (``fe80::1%eth0``, or as a URL writes
      it ``fe80::1%25eth0``) belongs to a link-local address, and a DNS name never
      contains ``%``, so such a host is not a public name, whatever precedes the
      ``%``, and is unknown;
    * it is an IP address literal, IPv4 or IPv6, public or not (``ipaddress``
      parses it);
    * it is ``localhost``;
    * it has no ``.`` (a single label such as ``db`` or ``intranet-app``);
    * its LAST label (the text after the last ``.``) is in
      ``PRIVATE_HOST_SUFFIXES``.

    ``"docs.python.org"`` -> False; ``"EXAMPLE.COM."`` -> False;
    ``"corp.example.com"`` -> False (only the last label counts);
    ``"3.13"`` -> False; ``"db.corp"`` -> True; ``"printer.local"`` -> True;
    ``"localhost"`` -> True; ``"db"`` -> True; ``"192.168.0.1"`` -> True;
    ``"8.8.8.8"`` -> True; ``"[::1]"`` -> True; ``""`` -> True;
    ``"[fe80::1%eth0]"`` -> True; ``"fe80::1%25eth0"`` -> True;
    ``"db.internal%eth0"`` -> True; ``"1.2.3.999"`` -> False (not an address; the
    last label ``999`` is not private).
    """
    host = host.lower()
    if host.endswith("."):
        host = host[:-1]
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host or "%" in host:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host == "localhost" or "." not in host:
        return True
    return host.rsplit(".", 1)[1] in PRIVATE_HOST_SUFFIXES


_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
_PEEL = ".,;:!?)]}>\"'"


def abstract_urls(text):
    """Reduce every URL to its public host name, or remove it.

    A URL starts at a scheme (an ASCII letter followed by letters, digits, ``+``,
    ``.`` or ``-``) followed by ``://`` and at least one non-space character, and
    runs to the next space or the end. Trailing ``.``, ``,``, ``;``, ``:``, ``!``,
    ``?``, ``)``, ``]``, ``}``, ``>``, ``"`` and ``'`` are not part of the URL
    (peel them off the end, repeatedly; they stay in the text). The URL's host is
    ``urllib.parse.urlsplit(url).hostname`` (lower-case, without user
    information, port and brackets). A ``ValueError`` from ``urlsplit``, or a
    missing or empty host, counts as a private host. If ``is_private_host(host)``
    the URL is removed; otherwise it is replaced by the host. Each URL counts 1.

    ``"read https://docs.python.org/3/library/asyncio.html for details"`` ->
    ``("read docs.python.org for details", 1)``;
    ``"open http://192.168.1.10:8080/admin"`` -> ``("open", 1)``;
    ``"http://localhost:3000/x and https://Example.COM/A?q=1#f"`` ->
    ``("and example.com", 2)``; ``"(see https://python.org/downloads)."`` ->
    ``("(see python.org).", 1)``; ``"git clone ssh://git@github.com/org/repo.git"``
    -> ``("git clone github.com", 1)``; ``"file:///etc/passwd"`` -> ``("", 1)``;
    ``"see http:// here"`` -> unchanged, 0.
    """
    count = 0

    def repl(match):
        nonlocal count
        url = match.group(0)
        stripped = url.rstrip(_PEEL)
        tail = url[len(stripped) :]
        count += 1
        try:
            host = urlsplit(stripped).hostname
        except ValueError:
            host = None
        if not host or is_private_host(host):
            return " " + tail
        return host + tail

    result = _URL.sub(repl, text)
    return _collapse(result), count


_EMAIL = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)


def abstract_emails(text):
    """Remove every e-mail address.

    An address is: one or more of ``A-Z a-z 0-9 . _ % + -``, then ``@``, then a
    domain of one or more labels of ``A-Z a-z 0-9 -`` separated by ``.`` whose
    last label is at least 2 ASCII letters and which contains at least one ``.``.
    Only the address is removed; text around it stays.

    ``"contact tom.k+dev@example.co.jp today"`` -> ``("contact today", 1)``;
    ``"a@x.org and b@y.org"`` -> ``("and", 2)``; ``"mailto:bob@x.org"`` ->
    ``("mailto:", 1)``; ``"a@b.c"``, ``"user@localhost"`` and ``"@octocat"`` are
    unchanged, 0.
    """
    result, count = _EMAIL.subn(" ", text)
    return _collapse(result), count


_OPEN = "\"'([{<"


def _starts_like_path(s):
    if s.startswith(("/", "~/", "./", "../", "\\\\")):
        return True
    return (
        len(s) >= 3
        and s[0].isascii()
        and s[0].isalpha()
        and s[1] == ":"
        and s[2] in "/\\"
    )


def abstract_paths(text):
    r"""Remove every whitespace-delimited token that is a file system path.

    Work on the tokens of ``text.split()``; keep the other tokens in order, joined
    by single spaces. Let ``core`` be the token without leading ``"``, ``'``, ``(``,
    ``[``, ``{`` and ``<`` characters. A token is a path, and removed (it counts
    1), when any of these holds:

    * ``core`` starts with ``/``, ``~/``, ``./``, ``../`` or two backslashes;
    * ``core`` starts with an ASCII letter, ``:`` and then ``/`` or a backslash
      (a Windows drive, ``C:\x``);
    * the token contains at least two path separators in all (``/`` and
      backslash both count);
    * the token contains ``=`` and the text after the first ``=`` (without
      leading ``"``, ``'``, ``(``, ``[``, ``{``, ``<``) satisfies one of the first
      two bullets, as ``core`` would (``--config=/etc/app.conf``).

    ``"open /etc/passwd now"`` -> ``("open now", 1)``; ``"see ~/notes.txt"`` ->
    ``("see", 1)``; ``"run ./build.sh"`` -> ``("run", 1)``;
    ``"edit src/app/main.py please"`` -> ``("edit please", 1)``;
    ``"read (C:\\Users\\tom\\x.txt)"`` -> ``("read", 1)``;
    ``"start --config=/etc/app.conf"`` -> ``("start", 1)``;
    ``"use TCP/IP and/or"`` -> unchanged, 0 (one separator, not at the start);
    ``"a=b file.txt"`` -> unchanged, 0. (The examples are Python string literals.)
    """
    kept = []
    count = 0
    for token in text.split():
        core = token.lstrip(_OPEN)
        value = token.partition("=")[2].lstrip(_OPEN) if "=" in token else ""
        if (
            _starts_like_path(core)
            or token.count("/") + token.count("\\") >= 2
            or (value and _starts_like_path(value))
        ):
            count += 1
        else:
            kept.append(token)
    return " ".join(kept), count


# One endpoint, matched whole: optional user information, a host, an optional port,
# an optional path. The time is linear: no repetition is nested in another, the user
# information ends at the only "@" it can hold, and the two classes around the first
# ":" of an IPv6 literal cannot both take that ":".
_ZONE = r"(?:%[^\s\[\]/]*)?"
_ENDPOINT = re.compile(
    r"(?:[^/@\[\]]*@)?"
    r"(?:\[(?P<v6>[0-9A-Fa-f.]*:[0-9A-Fa-f:.]*)" + _ZONE + r"\]"
    r"|(?P<name>(?i:localhost)|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\.?)"
    r"(?::\d{1,5})?(?:/\S*)?"
)
_BARE_V6 = re.compile(r"(?P<address>[0-9A-Fa-f.]*:[0-9A-Fa-f:.]*)" + _ZONE)


def _endpoint_host(core):
    """The bare host of an endpoint token, or ``None`` if ``core`` is not one."""
    match = _ENDPOINT.fullmatch(core)
    if match:
        return match.group("v6") or match.group("name")
    match = _BARE_V6.fullmatch(core)
    if match:
        try:
            ipaddress.IPv6Address(match.group("address"))
        except ValueError:
            return None
        return match.group("address")
    return None


def abstract_hosts(text):
    """Remove every token that names a private host or address.

    Work on the tokens of ``text.split()``; keep the other tokens in order, joined
    by single spaces. Let ``core`` be the token with leading ``"``, ``'``, ``(``,
    ``{`` and ``<`` characters removed and trailing ``"``, ``'``, ``)``, ``}``,
    ``>``, ``,``, ``;``, ``:``, ``.``, ``!`` and ``?`` characters removed (``[`` and
    ``]`` are never removed, so that an IPv6 literal in brackets stays whole).
    The token is dropped, and counts 1, when ``core`` is an endpoint and the host
    of that endpoint is private. The WHOLE core must match, in this order:

    1. optionally user information: any characters except ``/``, ``@``, ``[`` and
       ``]`` (there may be none), then ``@``;
    2. a host, which is one of: ``localhost`` in any case; two or more labels of
       ``A-Z a-z 0-9 -`` separated by ``.``, each of these two forms with ONE
       optional trailing ``.`` (an absolute name); an IPv6 literal in brackets
       (``[``, hex digits, ``:`` and ``.`` with at least one ``:``, optionally a
       zone identifier, which is ``%`` and any characters except white space,
       ``[``, ``]`` and ``/``, and ``]``);
    3. optionally ``:`` and 1 to 5 digits (the port);
    4. optionally ``/`` and anything (the path).

    A core that is no endpoint of this kind is dropped too when it is an IPv6
    address without brackets: ``ipaddress.IPv6Address`` accepts the part before an
    optional zone identifier (``%`` and any characters except white space, ``[``,
    ``]`` and ``/``). It has no port and no user information.

    The host is judged with ``is_private_host``, after the user information, the
    port, the path, the brackets, the zone identifier and the trailing ``.`` are
    taken off: ``is_private_host`` must be True for what is left.

    ``"connect to db.internal:5432 now"`` -> ``("connect to now", 1)``;
    ``"ping 192.168.1.5, then"`` -> ``("ping then", 1)``;
    ``"use localhost:8080/health"`` -> ``("use", 1)``; ``"see (printer.local)"`` ->
    ``("see", 1)``; ``"[::1]:8080"`` -> ``("", 1)``;
    ``"[fe80::1%eth0]:8080"`` -> ``("", 1)``; ``"db.internal.:5432"`` ->
    ``("", 1)``; ``"ssh admin@10.0.0.5"`` -> ``("ssh", 1)``; ``"ping fe80::1%eth0"``
    -> ``("ping", 1)``; ``"docs at example.com."``, ``"example.com.:8080"``,
    ``"python 3.13"``, ``"server1"``, ``"user@db:5432"`` and ``"file.py"`` are
    unchanged, 0 (public, or a single label that cannot be told from a word).
    """
    kept = []
    count = 0
    for token in text.split():
        core = token.lstrip("\"'({<").rstrip("\"')}>,;:.!?")
        host = _endpoint_host(core)
        if host is not None and is_private_host(host):
            count += 1
        else:
            kept.append(token)
    return " ".join(kept), count


_B = r"(?<![A-Za-z0-9])"
_E = r"(?![A-Za-z0-9])"
_IDS = re.compile(
    _B
    + r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
    + _E
    + "|"
    + _B
    + r"cred_[0-9a-f]{32}"
    + _E
    + "|"
    + _B
    + rf"[0-9A-Fa-f]{{{MIN_HEX_HASH_CHARS},}}"
    + _E
    + "|"
    + rf"(?<![A-Za-z0-9.])\d{{{MIN_ID_DIGITS},}}(?![A-Za-z0-9]|\.\d)"
)


def abstract_ids(text):
    """Remove identifiers: UUIDs, credential handles, hashes and long numbers.

    Remove every match (each counts 1) of one of these, tried in this order at
    each position ("bounded" means: not preceded and not followed by an ASCII
    letter or digit; a ``_`` or ``-`` does not count as one):

    1. a UUID: 8-4-4-4-12 hex digits (either case) separated by ``-``, bounded;
    2. a credential handle: ``cred_`` and 32 lowercase hex digits, bounded;
    3. a hex run: ``MIN_HEX_HASH_CHARS`` (12) or more hex digits (either case), bounded;
    4. a digit run: ``MIN_ID_DIGITS`` (5) or more digits, not preceded by an ASCII
       letter, digit or ``.``, and not followed by an ASCII letter, digit, or
       ``.`` and a digit (so ``3.14159265`` and ``12345.678`` are decimals and stay).

    ``"user 123e4567-e89b-12d3-a456-426614174000 failed"`` ->
    ``("user failed", 1)`` (the UUID is one match, not several);
    ``"ticket 1234567 vs 1234"`` -> ``("ticket vs 1234", 1)`` (5 digits go, 4 stay);
    ``"commit deadbeefcafe12 pushed"`` -> ``("commit pushed", 1)``;
    ``"deadbeefcaf"`` (11 hex digits) is unchanged; ``"abc12345"`` and ``"12345abc"``
    are unchanged; ``"pi is 3.14159265"`` is unchanged; ``"id 12345."`` ->
    ``("id .", 1)``; ``"cred_" + "0" * 32`` -> ``("", 1)``.
    """
    result, count = _IDS.subn(" ", text)
    return _collapse(result), count


_OPAQUE = re.compile(rf"[A-Za-z0-9+/_=-]{{{MIN_OPAQUE_TOKEN_CHARS},}}")


def drop_opaque_tokens(text):
    """Remove every long opaque token (base64 blobs, keys, long hashes).

    An opaque token is a maximal run of ``MIN_OPAQUE_TOKEN_CHARS`` (40) or more
    characters from ``A-Z a-z 0-9 + / _ = -``. It is removed and counts 1.

    ``"key " + "A" * 40 + " end"`` -> ``("key end", 1)``; a run of 39 is unchanged;
    ``"x " + "a-b_" * 10`` (40 characters) -> ``("x", 1)``; two separate runs count 2;
    ``"A" * 45 + "."`` -> ``(".", 1)``.
    """
    result, count = _OPAQUE.subn(" ", text)
    return _collapse(result), count


_VERSION = re.compile(r"(?<![A-Za-z0-9.])(v?\d+\.\d+)(?:\.\d+)+(?![A-Za-z0-9])")


def generalize_versions(text):
    """Cut a version number with three or more parts down to two parts.

    A version is: an optional ``v`` immediately followed by digits, ``.``, digits,
    and then one or more groups of ``.`` and digits; it must not be preceded by an
    ASCII letter, digit or ``.`` (a ``v`` is part of the match, not a letter that
    blocks it), and must not be followed by an ASCII letter or digit (a
    following ``-`` or ``_`` is fine). It is replaced by its first two parts (with
    the ``v`` if there was one) and counts 1. Two-part numbers are unchanged.

    ``"python 3.13.15"`` -> ``("python 3.13", 1)``;
    ``"fastapi 0.141.1 and v2.0.1"`` -> ``("fastapi 0.141 and v2.0", 2)``;
    ``"1.2.3.4"`` -> ``("1.2", 1)``; ``"1.2.3-beta"`` -> ``("1.2-beta", 1)``;
    ``"3.13"``, ``"abc1.2.3"`` and ``"1.2.3rc1"`` are unchanged, 0.
    """
    result, count = _VERSION.subn(r"\1", text)
    return _collapse(result), count


def truncate_query(text, max_chars):
    """Cut a collapsed ``text`` to at most ``max_chars`` characters, at a word.

    ``max_chars`` is an ``int`` of at least 1. If ``len(text) <= max_chars`` return
    ``text``. Otherwise let ``head = text[:max_chars]``: if the character
    ``text[max_chars]`` is a space, or ``head`` has no space, the result is ``head``
    (a hard cut when there is no space); otherwise cut ``head`` back to its last
    space. The result has no trailing space.

    ``truncate_query("aaaa bbbb cccc", 9)`` -> ``"aaaa bbbb"``;
    ``truncate_query("aaaa bbbb cccc", 7)`` -> ``"aaaa"``;
    ``truncate_query("aaaa bbbb", 3)`` -> ``"aaa"``;
    ``truncate_query("aaaa bbbb", 4)`` -> ``"aaaa"``;
    ``truncate_query("aaaa", 4)`` -> ``"aaaa"``;
    ``truncate_query("ab cd", 100)`` -> ``"ab cd"``.
    """
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    if text[max_chars] == " " or " " not in head:
        return head
    return head[: head.rindex(" ")]


def query_fingerprint(query):
    """``"sha256:"`` plus the lowercase hex SHA-256 of ``query`` as UTF-8.

    The text is hashed exactly as given (no normalisation, no salt).
    ``query_fingerprint("abc")`` is
    ``"sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"``.
    """
    return "sha256:" + hashlib.sha256(query.encode("utf-8")).hexdigest()
