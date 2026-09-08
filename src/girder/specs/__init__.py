"""OpenSpec subsystem (impl-plan §6.9): validation, freezing, generation."""

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
    "FreezeError",
    "FreezeResult",
    "SpecDocument",
    "SpecGenerationError",
    "SpecGenerator",
    "SpecValidationError",
    "TaskSpec",
    "approve_and_freeze",
    "parse_spec",
]
