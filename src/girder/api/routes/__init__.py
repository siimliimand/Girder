"""Console routes package (WP 10.1) — one router per resource family.

``create_app`` includes :data:`router`; every submodule contributes its own
``APIRouter``. All names that existed at module level in the pre-split
``girder/api/routes.py`` are re-exported here under their original names so
``from girder.api.routes import X`` keeps working unchanged.
"""

from __future__ import annotations

from fastapi import APIRouter

from girder.api.routes import (
    amendments as _amendments,
    clarify as _clarify,
    delivery as _delivery,
    history as _history,
    projects as _projects,
    runs as _runs,
    specs as _specs,
    steering as _steering,
    tiers as _tiers,
    webhooks as _webhooks,
)
from girder.api.routes._shared import (
    excerpt as _excerpt,
    merge_queue as _merge_queue,
    merged_rows as _merged_rows,
    panel_context as _panel_context,
    proposal_task_rows as _proposal_task_rows,
    redact as _redact,
    redact_value as _redact_value,
    require_project as _require_project,
    require_run as _require_run,
    review_window_message as _review_window_message,
    review_window_state as _review_window_state,
    run_context as _run_context,
)

router = APIRouter()
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
from girder.api.routes.projects import (  # noqa: E402
    _project_json,
    _project_timestamps,
)
from girder.api.routes.runs import (  # noqa: E402
    _TASK_STATUS_CLASSES,
    _STREAM_END_STATUSES,
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
    "router",
    "_TASK_STATUS_CLASSES",
    "_STREAM_END_STATUSES",
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
]
