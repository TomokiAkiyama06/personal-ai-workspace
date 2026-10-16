#!/usr/bin/env -S python3 -I
"""``git`` inside the tool sandbox (Decision 0090, 1; Decision 0083, 2).

Installed in the image as ``/usr/local/bin/git``. It decides where one call
goes and replaces itself with that program:

* a call aimed at the node's repository (``/workspace``) goes to the shim
  (``/usr/local/lib/paw/paw-git-shim``: the host's read-only git with the
  shim's rules, unchanged);
* anything else goes to the image's real git (``/usr/lib/paw/git``, not on
  ``PATH``): a repository a test or the model makes under ``/tmp``.

``init`` and ``clone`` are decided by the repository they create: the
directory (by default where git runs, for ``clone`` the source's name under
it, ``<name>.git`` for a bare clone) and ``--separate-git-dir``. Every other
call by where it runs: the working directory with each ``-C`` applied in
turn. Either way the shim takes the call when ``--git-dir`` / ``--work-tree``,
a ``-c`` / ``--config-env`` value that is an absolute path, or ``GIT_DIR`` and
the other variables that move the repository point into ``/workspace``.
Paths are resolved (``realpath``); the options of ``init`` / ``clone`` are
read like git does (unique prefixes of long options, clustered short ones). A
call it cannot read (an unknown option, a missing value, more directories than
the command takes) goes to the shim, which refuses what it does not allow.

This is not a boundary: the model can run the real git itself. The mounts and
masks are (Decision 0083, 6): the sandbox sees no git directory and no shared
object store of the node's repository. Standard library only.
"""

import os
import re
import sys

WORKSPACE = "/workspace"
REAL_GIT = "/usr/lib/paw/git"
SHIM_PROGRAM = "/usr/local/lib/paw/paw-git-shim"
FATAL = 128

REAL = "real"
SHIM = "shim"

#: Variables that point git at a repository, a work tree or its files.
PATH_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
)
LIST_VARIABLES = ("GIT_ALTERNATE_OBJECT_DIRECTORIES",)

#: git's global options without a value.
GLOBAL_FLAGS = frozenset(
    {
        "-p",
        "-P",
        "-v",
        "-h",
        "--paginate",
        "--no-pager",
        "--bare",
        "--no-replace-objects",
        "--no-lazy-fetch",
        "--no-optional-locks",
        "--no-advice",
        "--literal-pathspecs",
        "--glob-pathspecs",
        "--noglob-pathspecs",
        "--icase-pathspecs",
        "--exec-path",
        "--html-path",
        "--man-path",
        "--info-path",
        "--version",
        "--help",
    }
)
#: git's global options with a value that is not a place.
GLOBAL_OTHER_VALUES = frozenset(
    {"--namespace", "--super-prefix", "--attr-source", "--list-cmds", "--exec-path"}
)

#: The options of ``init`` / ``clone`` (git 2.47, ``git init -h`` / ``git
#: clone -h``): ``VALUE`` takes one (``--name value`` or ``--name=value``),
#: ``OPTIONAL`` only as ``--name=value``, ``FLAG`` none. ``NEGATABLE`` ones are
#: also ``--no-<name>`` (a flag). git accepts a unique prefix of a long option
#: (``--sep``), and clustered short options (``-qb main``, ``-qbmain``).
VALUE, OPTIONAL, FLAG = "value", "optional", "flag"


def _long_options(negatable: dict[str, str], plain: dict[str, str]) -> dict[str, str]:
    options = dict(plain)
    for name, kind in negatable.items():
        options[f"--{name}"] = kind
        options[f"--no-{name}"] = FLAG
    return options


INIT_LONG = _long_options(
    {
        "template": VALUE,
        "bare": FLAG,
        "quiet": FLAG,
        "separate-git-dir": VALUE,
        "initial-branch": VALUE,
        "object-format": VALUE,
        "ref-format": VALUE,
    },
    {"--shared": OPTIONAL},
)
CLONE_LONG = _long_options(
    {
        "verbose": FLAG,
        "quiet": FLAG,
        "progress": FLAG,
        "reject-shallow": FLAG,
        "bare": FLAG,
        "mirror": FLAG,
        "local": FLAG,
        "shared": FLAG,
        "recurse-submodules": OPTIONAL,
        "recursive": OPTIONAL,
        "jobs": VALUE,
        "template": VALUE,
        "reference": VALUE,
        "reference-if-able": VALUE,
        "dissociate": FLAG,
        "origin": VALUE,
        "branch": VALUE,
        "upload-pack": VALUE,
        "depth": VALUE,
        "shallow-since": VALUE,
        "shallow-exclude": VALUE,
        "single-branch": FLAG,
        "shallow-submodules": FLAG,
        "separate-git-dir": VALUE,
        "ref-format": VALUE,
        "config": VALUE,
        "server-option": VALUE,
        "filter": VALUE,
        "also-filter-submodules": FLAG,
        "remote-submodules": FLAG,
        "sparse": FLAG,
        "bundle-uri": VALUE,
    },
    {
        "--no-checkout": FLAG,
        "--checkout": FLAG,
        "--no-hardlinks": FLAG,
        "--hardlinks": FLAG,
        "--no-tags": FLAG,
        "--tags": FLAG,
        "--ipv4": FLAG,
        "--ipv6": FLAG,
    },
)
#: Options that are another's alias (one option when deciding a prefix).
ALIASES = {
    "--recursive": "--recurse-submodules",
    "--no-recursive": "--no-recurse-submodules",
}
#: Short options: the flags, and those that take a value (with their long name).
INIT_SHORT = ({"q"}, {"b": "--initial-branch"})
CLONE_SHORT = (
    {"v", "q", "n", "l", "s", "4", "6"},
    {
        "o": "--origin",
        "b": "--branch",
        "u": "--upload-pack",
        "c": "--config",
        "j": "--jobs",
    },
)


class _Unreadable(Exception):
    pass


def _resolve(base: str, path: str) -> str:
    return os.path.realpath(os.path.join(base, path))


def route(
    argv: list[str], cwd: str, environ: dict[str, str], workspace: str = WORKSPACE
) -> str:
    """``SHIM`` or ``REAL`` for ``git <argv>`` run in ``cwd`` with ``environ``."""
    mount = os.path.realpath(workspace)

    def inside(path: str) -> bool:
        return path == mount or path.startswith(mount.rstrip("/") + "/")

    try:
        place, rest, pointed = _global_options(argv, cwd, environ)
    except _Unreadable:
        return SHIM
    pointed = [_resolve(place, p) for p in pointed]
    for name in PATH_VARIABLES:
        if environ.get(name):
            pointed.append(_resolve(place, environ[name]))
    for name in LIST_VARIABLES:
        pointed += [_resolve(place, p) for p in _alternates(environ.get(name, ""))]
    if any(inside(p) for p in pointed):
        return SHIM
    subcommand, arguments = (rest[0], rest[1:]) if rest else (None, [])
    if subcommand in ("init", "clone"):
        try:
            targets = _created(subcommand, arguments, place, environ)
        except _Unreadable:
            return SHIM
        return SHIM if any(inside(t) for t in targets) else REAL
    return SHIM if inside(os.path.realpath(place)) else REAL


#: The escapes of a C-style quoted path (git's ``unquote_c_style``).
C_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    '"': '"',
}


def _alternates(value: str) -> list[str]:
    """The entries of a ``:``-separated list of object directories, read like
    git's ``parse_alt_odb_entry``: an entry that starts with ``"`` is a
    C-style quoted path (one that does not unquote is taken as it is), one
    that starts with ``#`` is a comment, empty ones are skipped."""
    entries: list[str] = []
    at = 0
    while at < len(value):
        if value[at] == "#":
            end = _find(value, ":", at)
            entry = ""
        else:
            unquoted = _unquote(value, at) if value[at] == '"' else None
            if unquoted is not None:
                entry, end = unquoted
            else:
                end = _find(value, ":", at)
                entry = value[at:end]
        if entry:
            entries.append(entry)
        # git skips the one character after the entry (the separator).
        at = end + 1
    return entries


def _find(value: str, character: str, start: int) -> int:
    found = value.find(character, start)
    return len(value) if found < 0 else found


def _unquote(value: str, at: int) -> tuple[str, int] | None:
    """The C-style quoted path at ``value[at]`` (a ``"``) and where it ends
    (after the closing quote), or None when it is not quoted correctly. Paths
    are bytes to git: an octal escape is a byte."""
    out = bytearray()
    i = at + 1
    while i < len(value):
        character = value[i]
        i += 1
        if character == '"':
            # git then uses the path as a C string: it ends at a NUL.
            return os.fsdecode(bytes(out).split(b"\0")[0]), i
        if character != "\\":
            out += os.fsencode(character)
            continue
        if i >= len(value):
            return None
        escape = value[i]
        i += 1
        if escape in C_ESCAPES:
            out += C_ESCAPES[escape].encode()
        elif escape in "0123" and re.fullmatch("[0-7]{2}", value[i : i + 2]):
            out.append(int(escape + value[i : i + 2], 8))
            i += 2
        else:
            return None
    return None


def _global_options(
    argv: list[str], cwd: str, environ: dict[str, str]
) -> tuple[str, list[str], list[str]]:
    """Where git runs, the subcommand and its arguments, and the places the
    options point at (relative ones are relative to where git runs)."""
    place, pointed = cwd, []
    i = 0
    while i < len(argv):
        word = argv[i]
        if not word.startswith("-"):
            break
        name, has_value, value = word.partition("=")
        if word == "-C":
            place = os.path.join(place, _value(argv, i))
            i += 2
            continue
        if name in ("--git-dir", "--work-tree"):
            if has_value:
                pointed.append(value)
                i += 1
            else:
                pointed.append(_value(argv, i))
                i += 2
            continue
        if word == "-c":
            pointed += _config_paths(_value(argv, i))
            i += 2
            continue
        if name == "--config-env":
            setting = value if has_value else _value(argv, i)
            key, _, variable = setting.rpartition("=")
            pointed += _config_paths(f"{key}={environ.get(variable, '')}")
            i += 1 if has_value else 2
            continue
        if name in GLOBAL_OTHER_VALUES and has_value:
            i += 1
            continue
        if word in GLOBAL_FLAGS:
            i += 1
            continue
        if word in GLOBAL_OTHER_VALUES:
            _value(argv, i)
            i += 2
            continue
        raise _Unreadable()
    return place, argv[i:], pointed


def _value(argv: list[str], i: int) -> str:
    if i + 1 >= len(argv):
        raise _Unreadable()
    return argv[i + 1]


def _config_paths(setting: str) -> list[str]:
    """A configuration value that is an absolute path (``core.worktree=/x``)."""
    _, _, value = setting.partition("=")
    return [value] if value.startswith("/") else []


def _created(
    subcommand: str, arguments: list[str], place: str, environ: dict[str, str]
) -> list[str]:
    """The places ``init`` / ``clone`` create (resolved)."""
    if subcommand == "init":
        long_options, (short_flags, short_values) = INIT_LONG, INIT_SHORT
    else:
        long_options, (short_flags, short_values) = CLONE_LONG, CLONE_SHORT
    positional: list[str] = []
    options: list[tuple[str, str]] = []
    i = 0
    while i < len(arguments):
        word = arguments[i]
        i += 1
        if word == "--":
            positional += arguments[i:]
            break
        if not word.startswith("-") or word == "-":
            positional.append(word)
        elif word.startswith("--"):
            given, has_value, value = word.partition("=")
            name = _long_name(given, long_options)
            kind = long_options[name]
            if kind == VALUE and not has_value:
                value = _value(arguments, i - 1)
                i += 1
            elif kind == FLAG and has_value:
                raise _Unreadable()
            options.append((name, value))
        else:
            for at, letter in enumerate(word[1:], start=2):
                if letter in short_flags:
                    continue
                if letter not in short_values:
                    raise _Unreadable()
                value = word[at:]
                if not value:
                    value = _value(arguments, i - 1)
                    i += 1
                options.append((short_values[letter], value))
                break
    created: list[str] = []
    flags = {"--bare": False, "--mirror": False}
    for name, value in options:
        if name in flags:
            flags[name] = True
        elif name.replace("--no-", "--", 1) in flags:
            flags[name.replace("--no-", "--", 1)] = False
        elif name == "--separate-git-dir":
            created.append(_resolve(place, value))
        elif name == "--config":
            created += [_resolve(place, p) for p in _config_paths(value)]
    if subcommand == "init":
        if len(positional) > 1:
            raise _Unreadable()
        created.append(_resolve(place, positional[0] if positional else "."))
    else:
        if not 1 <= len(positional) <= 2:
            raise _Unreadable()
        if len(positional) == 2:
            created.append(_resolve(place, positional[1]))
        else:
            # A bare clone (``--bare``, ``--mirror``) is made in ``<name>.git``.
            # --mirror implies it; --no-mirror does not undo --bare.
            bare = flags["--bare"] or flags["--mirror"]
            created.append(_resolve(place, _clone_name(positional[0], place, bare)))
    return created


def _long_name(given: str, options: dict[str, str]) -> str:
    """The long option ``given`` names: itself, or the one it is a unique
    prefix of (as git's option parser accepts)."""
    if given in options:
        return given
    matches = {ALIASES.get(name, name) for name in options if name.startswith(given)}
    if len(given) < 3 or len(matches) != 1:
        raise _Unreadable()
    return matches.pop()


def _clone_name(source: str, place: str, bare: bool) -> str:
    """The directory ``git clone <source>`` makes: git's "humanish" name
    (``git_url_basename``). It strips ``.bundle`` when the source is a bundle
    file and ``.git`` otherwise (only one of them); a bare clone's name ends in
    ``.git`` (added before control characters are turned into spaces)."""
    start = source.find("://")
    start = 0 if start < 0 else start + 3
    # Authentication data: up to the last ``@`` before the first ``/``.
    host_end = _find(source, "/", start)
    at = source.rfind("@", start, host_end)
    if at >= 0:
        start = at + 1
    end = len(source)
    while start < end and (source[end - 1] == "/" or source[end - 1] in SPACES):
        end -= 1
    if end - start > 5 and source[end - 5 : end] == "/.git":
        end -= 5
        while start < end and source[end - 1] == "/":
            end -= 1
    # A port after a host alone (``host:2222``).
    if "/" not in source[start:end] and ":" in source[start:end]:
        port = end
        while start < port and source[port - 1] in "0123456789":
            port -= 1
        if start < port and source[port - 1] == ":":
            end = port - 1
    # The last component; a ``:`` separates too.
    last = end
    while start < last and source[last - 1] not in "/:":
        last -= 1
    name = source[last:end]
    name = name.removesuffix(".bundle" if _is_bundle(source, place) else ".git")
    if not name or name == "/":
        raise _Unreadable()
    if bare:
        name += ".git"
    # Control characters and spaces: one space, none at the ends.
    name = re.sub(r"[\x00-\x20]+", " ", name).strip(" ")
    if not name:
        raise _Unreadable()
    return name


#: git's ``isspace``.
SPACES = " \t\n\r"


def _is_bundle(source: str, place: str) -> bool:
    """Whether git clones ``source`` as a bundle (``get_repo_path``): no
    repository is at ``source`` (as a directory or a ``gitdir:`` file, with
    git's suffixes) and a regular file is at ``source.bundle`` or ``source``."""
    path = os.path.join(place, source)
    for suffix in ("/.git", "", ".git/.git", ".git"):
        candidate = path + suffix
        if os.path.isdir(candidate):
            if _is_git_directory(candidate):
                return False
        elif os.path.isfile(candidate) and _gitfile_target(candidate):
            return False
    return any(os.path.isfile(path + suffix) for suffix in (".bundle", ""))


def _is_git_directory(path: str) -> bool:
    """git's ``is_git_directory``, nearly: a HEAD, and objects and refs in the
    common directory (the one ``commondir`` names, as in a linked worktree's
    git directory, or itself)."""
    if not os.path.lexists(os.path.join(path, "HEAD")):
        return False
    common = path
    try:
        with open(os.path.join(path, "commondir"), "rb") as handle:
            named = os.fsdecode(handle.read()).rstrip("\r\n")
    except (OSError, ValueError):
        named = ""
    if named:
        common = os.path.join(path, named)
    return os.path.isdir(os.path.join(common, "objects")) and os.path.isdir(
        os.path.join(common, "refs")
    )


def _gitfile_target(path: str) -> bool:
    """Whether ``path`` is a ``gitdir: <repository>`` file (more than eight
    bytes) that points at a git directory."""
    try:
        if os.path.getsize(path) <= 8:
            return False
        with open(path, "rb") as handle:
            text = os.fsdecode(handle.read(65536))
    except (OSError, ValueError):
        return False
    if not text.startswith("gitdir: "):
        return False
    target = text[len("gitdir: ") :].rstrip("\r\n")
    target = os.path.join(os.path.dirname(path), target)
    return os.path.isdir(target) and _is_git_directory(target)


def main(argv: list[str]) -> int:
    try:
        cwd = os.getcwd()
    except OSError:
        target = SHIM_PROGRAM
    else:
        choice = route(argv, cwd, dict(os.environ))
        target = REAL_GIT if choice == REAL else SHIM_PROGRAM
    try:
        os.execv(target, [target, *argv])
    except OSError:
        pass
    sys.stderr.write("git: git is not available in this sandbox\n")
    return FATAL


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
