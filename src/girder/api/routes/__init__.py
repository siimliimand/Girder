"""Console routes package (WP 10.1) — one router per resource family.

``create_app`` includes :data:`router`; every submodule contributes its own
``APIRouter``. The compat surface is the HTTP behavior of the routes themselves
(verified by tests), not the full pre-split symbol table: this package
re-exports the aggregate ``router``, each per-module ``APIRouter``, a few
helpers under their old names (``_next_generation_estimate`` & co.), and the
legacy handler name ``merge_queue`` (now an alias for
:func:`girder.api.routes.delivery.merge_queue_json`). Other pre-split handler
functions moved into their route modules and are not re-exported.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from girder.api.auth import require_api_key
from girder.api.routes import (
    amendments as _amendments,
)
from girder.api.routes import (
    clarify as _clarify,
)
from girder.api.routes import (
    delivery as _delivery,
)
from girder.api.routes import (
    history as _history,
)
from girder.api.routes import (
    projects as _projects,
)
from girder.api.routes import (
    runs as _runs,
)
from girder.api.routes import (
    specs as _specs,
)
from girder.api.routes import (
    steering as _steering,
)
from girder.api.routes import (
    tiers as _tiers,
)
from girder.api.routes import (
    webhooks as _webhooks,
)
from girder.api.routes._shared import (
    excerpt as _excerpt,
)
from girder.api.routes._shared import (
    load_merge_queue as _merge_queue,
)
from girder.api.routes._shared import (
    merged_rows as _merged_rows,
)
from girder.api.routes._shared import (
    panel_context as _panel_context,
)
from girder.api.routes._shared import (
    proposal_task_rows as _proposal_task_rows,
)
from girder.api.routes._shared import (
    redact as _redact,
)
from girder.api.routes._shared import (
    redact_value as _redact_value,
)
from girder.api.routes._shared import (
    require_project as _require_project,
)
from girder.api.routes._shared import (
    require_run as _require_run,
)
from girder.api.routes._shared import (
    review_window_message as _review_window_message,
)
from girder.api.routes._shared import (
    review_window_state as _review_window_state,
)
from girder.api.routes._shared import (
    run_context as _run_context,
)

# WP 12.5: single auth enforcement point for every console route. Static
# assets live on the /static mount (not this router) and are exempt
# structurally; /metrics and webhooks opt out via auth.PUBLIC_PATH_PREFIXES.
router = APIRouter(dependencies=[Depends(require_api_key)])
for _module in (
    _projects,
    _runs,
    _specs,
    _steering,
    _amendments,
    _clarify,
    _delivery,
    _history,
    _tiers,
    _webhooks,
):
    router.include_router(_module.router)

# ---------------------------------------------------------------- legacy names
# Pre-split routes.py module-level names, preserved for backward compatibility
# (zero call-site/test changes). Underscore names keep their exact spelling.

from girder.api.routes.amendments import (  # noqa: E402
    _amendments_inbox,
    _resolve_amendment_route,
)
from girder.api.routes.delivery import (  # noqa: E402
    merge_queue_json as merge_queue,
)
from girder.api.routes.projects import (  # noqa: E402
    _project_json,
    _project_timestamps,
)
from girder.api.routes.runs import (  # noqa: E402
    _STREAM_END_STATUSES,
    _TASK_STATUS_CLASSES,
    _event_class,
    _graph,
    _task_view,
)
from girder.api.routes.specs import (  # noqa: E402
    _estimate_ctx,
    _next_generation_estimate,
    _next_generation_prompt_chars,
    _tier1_role,
)
from girder.api.routes.steering import _github_for  # noqa: E402

__all__ = [
    "_STREAM_END_STATUSES",
    "_TASK_STATUS_CLASSES",
    "_amendments_inbox",
    "_estimate_ctx",
    "_event_class",
    "_excerpt",
    "_github_for",
    "_graph",
    "_history",
    "_merge_queue",
    "_merged_rows",
    "_next_generation_estimate",
    "_next_generation_prompt_chars",
    "_panel_context",
    "_project_json",
    "_project_timestamps",
    "_proposal_task_rows",
    "_redact",
    "_redact_value",
    "_require_project",
    "_require_run",
    "_resolve_amendment_route",
    "_review_window_message",
    "_review_window_state",
    "_run_context",
    "_task_view",
    "_tier1_role",
    "merge_queue",
    "router",
]
