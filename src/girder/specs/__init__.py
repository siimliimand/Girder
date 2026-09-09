"""OpenSpec subsystem (impl-plan §6.9): validation, freezing, amendment."""

from girder.specs.amendment import AmendmentError, ResolutionOutcome, resolve_amendment
from girder.specs.decomposer import DecompositionError, decompose_spec
from girder.specs.freeze import FreezeError, FreezeResult, approve_and_freeze
from girder.specs.generator import SpecGenerationError, SpecGenerator
from girder.specs.validator import (
    OPENSPEC_TEMPLATE,
    SPEC_SCHEMA,
    SpecDocument,
    SpecValidationError,
    TaskSpec,
    parse_spec,
)

__all__ = [
    "OPENSPEC_TEMPLATE",
    "SPEC_SCHEMA",
    "AmendmentError",
    "DecompositionError",
    "FreezeError",
    "FreezeResult",
    "ResolutionOutcome",
    "SpecDocument",
    "SpecGenerationError",
    "SpecGenerator",
    "SpecValidationError",
    "TaskSpec",
    "approve_and_freeze",
    "decompose_spec",
    "parse_spec",
    "resolve_amendment",
]
