"""Shared Pydantic base accepting camelCase JSON bodies (matching every existing `invoke(cmd,
args)` call site's argument names, e.g. `tableAreasByPage`, `suggestedName`) while keeping
snake_case field names in the Python code. This is what lets the frontend's ~27 `invoke()` call
sites stay completely untouched -- only the wrapper function itself and the handful of
file-dialog/drag-drop touch points needed rewriting.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
