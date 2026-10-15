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
it) and ``--separate-git-dir``. Every other call by where it runs: the working
directory with each ``-C`` applied in turn. Either way the shim takes the call
when ``--git-dir`` / ``--work-tree``, a ``-c`` / ``--config-env`` value that is
an absolute path, or ``GIT_DIR`` and the other variables that move the
repository point into ``/workspace``. Paths are resolved (``realpath``). A call
it cannot read (an unknown global option, a missing value, more directories
than the command takes) goes to the shim, which refuses what it does not allow.

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

#: Options of ``init`` / ``clone`` that take a value (``--name value`` or
#: ``--name=value``; the short ones also ``-xvalue``).
INIT_VALUES = frozenset(
    {
        "--template",
        "--separate-git-dir",
        "-b",
        "--initial-branch",
        "--object-format",
        "--ref-format",
    }
)
CLONE_VALUES = frozenset(
    {
        "-o",
        "--origin",
        "-b",
        "--branch",
        "-u",
        "--upload-pack",
        "--reference",
        "--reference-if-able",
        "--separate-git-dir",
        "--depth",
        "--shallow-since",
        "--shallow-exclude",
        "-c",
        "--config",
        "--server-option",
        "--template",
        "-j",
        "--jobs",
        "--filter",
        "--bundle-uri",
        "--ref-format",
        "--revision",
    }
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
        pointed += [_resolve(place, p) for p in environ.get(name, "").split(":") if p]
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
    takes_value = INIT_VALUES if subcommand == "init" else CLONE_VALUES
    positional: list[str] = []
    created: list[str] = []
    i = 0
    while i < len(arguments):
        word = arguments[i]
        if word == "--":
            positional += arguments[i + 1 :]
            break
        if not word.startswith("-") or word == "-":
            positional.append(word)
            i += 1
            continue
        name, has_value, value = word.partition("=")
        short = len(word) > 2 and not word.startswith("--") and word[:2]
        if has_value and name.startswith("--"):
            pass
        elif short and short in takes_value:
            name, value = short, word[2:]
        elif word in takes_value:
            value = _value(arguments, i)
            i += 1
        if name == "--separate-git-dir":
            created.append(_resolve(place, value))
        elif name in ("-c", "--config"):
            created += [_resolve(place, p) for p in _config_paths(value)]
        i += 1
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
            created.append(_resolve(place, _clone_name(positional[0])))
    return created


def _clone_name(source: str) -> str:
    """The directory ``git clone <source>`` makes (git's "humanish" name)."""
    name = source.rstrip("/")
    name = name.removesuffix("/.git").rstrip("/")
    name = re.split(r"[/:]", name)[-1]
    for suffix in (".git", ".bundle"):
        name = name.removesuffix(suffix)
    if not name:
        raise _Unreadable()
    return name


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
