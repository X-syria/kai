"""Resolving a bounded ``route`` label, and accepting a caller's correlation id.

Two independent problems live here because both are about turning unbounded
attacker-controlled input into a bounded label or a bounded id.

**Route label.** Every metric that mentions a route must use the *template*
(``/api/v1/tasks/{task_id}``), never the concrete path. A concrete path would
create one time series per task id, per token, per email - a caller could then
inflate the metrics storage of the deployment by requesting invented URLs.

The template is resolved from the application's own OpenAPI path table, which
is the one public, exact source of the full prefixed paths. It is not read from
``scope["route"].path``: with FastAPI's lazily-included routers the route object
placed in the scope is the *leaf* route, so its ``path`` is ``/tasks/{task_id}``
with the ``/api/v1`` prefix missing - a label that would silently collide with
any other router that ever mounts the same leaf path. This module builds the
table once at startup and matches the concrete path against it.

**Request id.** ``X-Request-ID`` is genuinely useful: a client that reports
"request X failed" should get the server's own id back so support can find the
log line. But the header is attacker-controlled, so honouring it verbatim would
let a client:

* inject CR/LF and forge extra log lines,
* push megabytes into every log line and every response header,
* choose values that collide with an operator's tooling,
* mint an arbitrary number of distinct ids to inflate the log volume.

So the policy is: honour the header only when it looks like an id, and mint one
otherwise. Bounded charset, bounded length, and a UUID fast path so the common
"client sent a real UUID" case keeps its value.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, MutableMapping
from dataclasses import dataclass
from typing import Any, Final

from starlette.datastructures import Headers

from kai.observability.metrics import UNMATCHED_ROUTE

#: Maximum accepted length. Long enough for a UUID, a trace id, or a short
#: proxy-generated id; short enough that a hostile value cannot be used as a
#: cheap amplification channel.
MAX_REQUEST_ID_LENGTH: Final[int] = 64

#: Accepted charset: URL-safe, no control characters, no separators that would
#: let the value break out of a log line or a header. A UUID and a W3C trace id
#: both satisfy this.
_ALLOWED: Final = re.compile(r"^[A-Za-z0-9._~-]{1,64}$")

#: Client-IP shapes we deliberately refuse to echo, so an operator never sees a
#: "request id" that is really an address. Keeping IPs in logs is a separate
#: decision (see the rate limiter, which keys on client address) and must not
#: arrive disguised as a correlation id.
_LOOKS_LIKE_IPV4: Final = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

#: A ``{param}`` (or ``{param:converter}``) segment in a route template.
_PATH_PARAM: Final = re.compile(r"\{[^}/]+(?::[^}/]+)?\}")


def request_id_from_headers(headers: Headers) -> str:
    """Return the request id to use: the caller's if it is safe, else a fresh one.

    Never raises and never returns an empty value.
    """
    supplied = headers.get("x-request-id")
    if supplied is None:
        return new_request_id()
    candidate = supplied.strip()
    if not candidate or len(candidate) > MAX_REQUEST_ID_LENGTH:
        return new_request_id()
    if not _ALLOWED.match(candidate):
        return new_request_id()
    if _LOOKS_LIKE_IPV4.match(candidate):
        return new_request_id()
    return candidate


def new_request_id() -> str:
    """Mint an id this service is certain did not come from a client."""
    return str(uuid.uuid4())


def _template_to_pattern(template: str) -> re.Pattern[str]:
    """Compile one route template into an anchored pattern for a concrete path.

    Every ``{param}`` becomes a single path segment. The KAI route table has no
    catch-all converter, so this is exact for the paths that exist; a template
    with a converter would still match, just with segment-level rather than
    greedy semantics.
    """
    parts = []
    index = 0
    for match in _PATH_PARAM.finditer(template):
        parts.append(re.escape(template[index : match.start()]))
        parts.append("[^/]+")
        index = match.end()
    parts.append(re.escape(template[index:]))
    return re.compile("".join(parts))


@dataclass(frozen=True)
class RouteTable:
    """The application's route templates, precompiled for matching."""

    patterns: tuple[tuple[re.Pattern[str], str], ...] = ()

    def label(self, path: str | None) -> str:
        """Return the template matching ``path``, or the unmatched bucket.

        Never raises and never returns a concrete path, so the result is always
        drawn from a fixed set defined by the code.
        """
        if not path:
            return UNMATCHED_ROUTE
        for pattern, template in self.patterns:
            if pattern.fullmatch(path):
                return template
        return UNMATCHED_ROUTE


def build_route_table(templates: Iterable[str]) -> RouteTable:
    """Build the matcher from the app's own route templates.

    Ordered so that the most specific template is tried first. With anchored
    full matches order is not strictly required for correctness, but a literal
    segment should win over a parameter one for readability of any future
    change that stops anchoring.
    """
    unique = sorted({t for t in templates if t}, key=lambda t: (-t.count("{"), -len(t), t))
    return RouteTable(
        patterns=tuple((_template_to_pattern(t), t) for t in unique),
    )


def route_label(scope: MutableMapping[str, Any], table: RouteTable) -> str:
    """Resolve the bounded route label for a request.

    ``table`` is passed in rather than built here so the cost is paid once at
    startup instead of per request.
    """
    label = table.label(scope.get("path"))
    if label != UNMATCHED_ROUTE:
        return label
    # The table is built from the app itself, so a miss means the request did
    # not resolve to a declared route (a 404, or a middleware-only rejection).
    # Falling back to the scope's route template keeps the label useful if the
    # table was built before a router was mounted.
    route = scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    if isinstance(template, str) and template:
        return _bounded_template(template)
    return UNMATCHED_ROUTE


def _bounded_template(template: str) -> str:
    return template if len(template) <= 128 else UNMATCHED_ROUTE