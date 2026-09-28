"""Serve the built Web App (``apps/web/dist``) on the API's own origin.

PAW-060, Decision 0044 (Proposed; this implements its recommendation). The session
cookie is ``__Host-``, ``Secure``, ``SameSite=Strict`` and every state-changing
request must come from this origin (``OriginCheckMiddleware``), so the browser app
has to be served by the same origin as ``/api/v1``. Setting
``PAW_WEB_DIST_DIR`` to the directory that ``npm run build`` produced turns this
on; unset, the backend serves the API only (the default, unchanged).

Only ``GET`` / ``HEAD`` of a path outside ``/api`` is looked at; everything else
goes to the application unchanged. A path that names a file of the build is that
file; a path whose last segment has no ``.`` (a client-side route such as
``/settings/devices`` or ``/pair``) is ``index.html``; anything else falls through
to the application's JSON 404. Hidden segments (``.``-prefixed) and anything that
resolves outside the directory (``..``, a symlink) are never served.

The pages get their own ``Content-Security-Policy`` (the API's ``default-src
'none'`` would block the app's own scripts): scripts, styles and requests only from
this origin, no inline script, no framing. ``index.html`` is revalidated on every
load (``no-cache``), the content-hashed files of ``assets/`` are cached for a year.
The other defensive headers (``X-Frame-Options`` and so on) are still added by
``SecurityHeadersMiddleware`` around this.
"""

from pathlib import Path

from starlette.responses import FileResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# The Web App's CSP. ``img-src data:`` is for small inline images of the build;
# ``connect-src 'self'`` covers ``fetch`` and the event stream of this origin.
WEB_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)
INDEX_CACHE_CONTROL = "no-cache"
ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
_READ_METHODS = frozenset({"GET", "HEAD"})


def validate_dist_dir(path: Path) -> Path:
    """The resolved build directory, or ``ValueError`` if it is not one."""
    if not path.is_absolute():
        raise ValueError("web_dist_dir must be an absolute path")
    resolved = path.resolve()
    if not resolved.is_dir() or _inside(resolved, resolved / "index.html") is None:
        raise ValueError("web_dist_dir must be a directory that contains index.html")
    return resolved


def _inside(root: Path, candidate: Path) -> Path | None:
    """``candidate`` resolved, if it is a file that stays beneath ``root``.

    Symlinks are followed first, so a link that leaves the build directory
    (``index.html`` included) is never a file of the build.
    """
    try:
        target = candidate.resolve()
        if target.is_relative_to(root) and target.is_file():
            return target
    except (OSError, ValueError):
        # A name the file system refuses (too long, say): not a file of the build.
        pass
    return None


def _is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


class WebAppMiddleware:
    """Pure ASGI middleware; see the module docstring."""

    def __init__(self, app: ASGIApp, *, dist_dir: Path) -> None:
        self.app = app
        self.dist_dir = validate_dist_dir(dist_dir)
        self.index = self.dist_dir / "index.html"

    def _resolve(self, path: str) -> tuple[Path, str] | None:
        """The file to serve for ``path`` and its Cache-Control, or ``None``."""
        if "\x00" in path or "\\" in path:
            return None
        segments = [segment for segment in path.split("/") if segment]
        # index.html is checked on every request too: it may be replaced after
        # start-up (a new build), and a link out of the directory is refused.
        index = _inside(self.dist_dir, self.index)
        if not segments:
            return (index, INDEX_CACHE_CONTROL) if index else None
        if any(segment.startswith(".") for segment in segments):
            return None
        candidate = _inside(self.dist_dir, self.dist_dir / Path(*segments))
        if candidate is not None:
            if candidate == index:
                return index, INDEX_CACHE_CONTROL
            immutable = candidate.is_relative_to(self.dist_dir / "assets")
            return candidate, ASSET_CACHE_CONTROL if immutable else INDEX_CACHE_CONTROL
        if "." not in segments[-1]:
            # A client-side route: the app itself decides what to show.
            return (index, INDEX_CACHE_CONTROL) if index else None
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"].upper() not in _READ_METHODS
            or _is_api_path(scope["path"])
        ):
            await self.app(scope, receive, send)
            return
        found = self._resolve(scope["path"])
        if found is None:
            await self.app(scope, receive, send)
            return
        file, cache_control = found
        response = FileResponse(
            file,
            headers={
                "Content-Security-Policy": WEB_CONTENT_SECURITY_POLICY,
                "Cache-Control": cache_control,
            },
        )
        await response(scope, receive, send)
