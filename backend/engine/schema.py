#!/usr/bin/env python3
"""RFP Schema — single source of truth for all extraction targets.

Loads the RFP template schema from ``extract-config.json`` (or inline dict)
and provides helpers to navigate, flatten, validate, and describe what
should be extracted from clinical protocol documents.
"""
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FieldDef:
    """Metadata for a single leaf field in the RFP schema."""
    section: str
    section_title: str
    field_name: str
    description: str
    field_type: str
    required: bool
    enum_values: list[str] | None = None
    nullable: bool = False
    parent_path: str = ""

@dataclass
class SectionDef:
    """One section (table / group) in the RFP template."""
    name: str
    title: str
    description: str
    fields: list[FieldDef] = field(default_factory=list)
    required: bool = False

# ---------------------------------------------------------------------------
# Schema loader
# ---------------------------------------------------------------------------

_SCHEMA_DATA: dict | None = None

def load_schema(path: str | Path | None = None) -> dict:
    """Load and cache the full RFP schema dict."""
    global _SCHEMA_DATA
    if _SCHEMA_DATA is not None:
        return _SCHEMA_DATA

    candidates = []
    if path:
        candidates.append(Path(path))
    candidates.append(Path(__file__).parent / "extract-config.json")
    candidates.append(Path.cwd() / "extract-config.json")

    for p in candidates:
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            schema = raw.get("data_schema", raw)
            _SCHEMA_DATA = schema
            return schema

    raise FileNotFoundError(
        "RFP schema not found. Provide extract-config.json or set path."
    )

# ---------------------------------------------------------------------------
# Resolve JSON Schema anyOf/oneOf to effective node
# ---------------------------------------------------------------------------

def _resolve_node(val: dict) -> dict:
    """Resolve anyOf/oneOf wrappers and return the effective schema node.

    If *val* has no anyOf/oneOf, returns *val* unchanged.
    Otherwise returns the *first non-null branch* (with ``"nullable": True``
    added if a null branch was present).
    """
    for combo_key in ("anyOf", "oneOf"):
        alternatives = val.get(combo_key)
        if not alternatives:
            continue
        has_null = any(alt.get("type") == "null" for alt in alternatives)
        non_null = [alt for alt in alternatives if alt.get("type") != "null"]
        if non_null:
            resolved = dict(non_null[0])  # shallow copy
            if has_null:
                resolved["nullable"] = True
            return resolved
    return dict(val)  # no anyOf/oneOf

def _resolve_type(val: dict) -> str:
    """Return the effective JSON Schema type for a value node."""
    node = _resolve_node(val)
    return node.get("type", "string")

def _resolve_nullable(val: dict) -> bool:
    """True if the value accepts null (directly or via anyOf/oneOf)."""
    if val.get("nullable"):
        return True
    for combo_key in ("anyOf", "oneOf"):
        for alt in val.get(combo_key, []):
            if alt.get("type") == "null":
                return True
    return False

def _resolve_enum(val: dict) -> list[str] | None:
    """Return enum list from any location."""
    node = _resolve_node(val)
    return node.get("enum")

# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------

def _walk_properties(obj, section, section_title, parent_path="", required_set=None, depth=0):
    """Recursively walk a schema properties block and yield leaf FieldDefs."""
    if required_set is None:
        required_set = frozenset()
    fields = []
    props = obj.get("properties", {})
    for key, val in props.items():
        effective = _resolve_node(val)
        path = f"{parent_path}.{key}" if parent_path else key
        typ = effective.get("type", "string")
        desc = val.get("description", "")
        enum = _resolve_enum(val)
        nullable = _resolve_nullable(val)
        req = key in required_set

        if typ == "object" and "properties" in effective:
            nested_req = set(effective.get("required", []))
            fields.extend(
                _walk_properties(effective, section, section_title, path, nested_req, depth + 1)
            )
        elif typ == "array" and "properties" in effective.get("items", {}):
            item_props = effective["items"]["properties"]
            item_req = set(effective["items"].get("required", []))
            for ik, iv in item_props.items():
                ieffective = _resolve_node(iv)
                ipath = f"{path}[].{ik}"
                ityp = ieffective.get("type", "string")
                idesc = iv.get("description", "")
                ienum = _resolve_enum(iv)
                inullable = _resolve_nullable(iv)
                ireq = ik in item_req
                fields.append(FieldDef(
                    section=section, section_title=section_title,
                    field_name=ipath, description=idesc, field_type=ityp,
                    required=ireq, enum_values=ienum, nullable=inullable,
                    parent_path=path,
                ))
        else:
            fields.append(FieldDef(
                section=section, section_title=section_title,
                field_name=key if depth == 0 else path,
                description=desc, field_type=typ, required=req,
                enum_values=enum, nullable=nullable, parent_path=parent_path,
            ))
    return fields


def flatten_fields(schema=None):
    """Return every leaf FieldDef across all schema sections."""
    schema = schema or load_schema()
    sections = schema.get("properties", {})
    top_required = set(schema.get("required", []))
    all_fields = []

    for sec_name, sec_val in sections.items():
        effective = _resolve_node(sec_val)
        sec_desc = sec_val.get("description", "")
        sec_type = effective.get("type", "object")
        sec_req = set(effective.get("required", []))

        if sec_type == "object" and "properties" in effective:
            all_fields.extend(
                _walk_properties(effective, sec_name, sec_desc, required_set=sec_req)
            )
        else:
            all_fields.append(FieldDef(
                section=sec_name, section_title=sec_desc,
                field_name=sec_name, description=sec_desc,
                field_type=sec_type, required=sec_name in top_required,
            ))

    return all_fields


def describe_section(name, schema=None):
    """Get a single section definition."""
    schema = schema or load_schema()
    val = schema.get("properties", {}).get(name)
    if val is None:
        return None
    fields = flatten_fields(schema)
    section_fields = [f for f in fields if f.section == name]
    top_required = set(schema.get("required", []))
    return SectionDef(
        name=name, title=val.get("description", name),
        description=val.get("description", ""),
        fields=section_fields, required=name in top_required,
    )


def build_default_extraction(schema=None):
    """Build an empty dict matching the schema structure with None defaults."""
    schema = schema or load_schema()
    def _default(obj):
        effective = _resolve_node(obj)
        typ = effective.get("type", "string")
        if typ == "object" and "properties" in effective:
            return {k: _default(v) for k, v in effective.get("properties", {}).items()}
        elif typ == "array":
            return []
        else:
            return None
    return _default(schema)


def validate_extraction(data, schema=None):
    """Validate extracted data against schema. Returns list of error strings."""
    schema = schema or load_schema()
    errors = []

    def _check(obj, schema_node, path=""):
        effective = _resolve_node(schema_node)
        typ = effective.get("type", "string")
        props = effective.get("properties", {})

        if typ == "object":
            for key, val_def in props.items():
                if key not in obj:
                    errors.append(f"Missing field: {path}.{key}")
                    continue
                _check(obj[key], val_def, f"{path}.{key}")
            for req_key in effective.get("required", []):
                if req_key not in obj or obj[req_key] is None:
                    errors.append(f"Required field missing/null: {path}.{req_key}")
            if effective.get("additionalProperties") is False:
                for key in obj:
                    if key not in props and key not in effective.get("required", []):
                        errors.append(f"Extra field: {path}.{key}")
        elif typ == "array":
            if not isinstance(obj, list):
                errors.append(f"Expected array at {path}")
                return
            items_def = effective.get("items", {})
            for i, item in enumerate(obj):
                item_eff = _resolve_node(items_def)
                if item_eff.get("type") == "object" or "properties" in item_eff:
                    _check(item, items_def, f"{path}[{i}]")
        else:
            enum_vals = _resolve_enum(schema_node)
            if enum_vals and obj not in enum_vals and obj is not None:
                errors.append(f"Invalid value {obj!r} at {path}: must be one of {enum_vals}")

    _check(data, schema)
    return errors


def summary(schema=None):
    """Return a human-readable summary of the schema."""
    schema = schema or load_schema()
    fields = flatten_fields(schema)
    total = len(fields)
    required_fields = sum(1 for f in fields if f.required)
    section_count = len(schema.get("properties", {}))
    lines = [
        f"RFP Schema: {total} fields across {section_count} sections",
        f"Required: {required_fields}  Optional: {total - required_fields}",
    ]
    for sec_name in sorted(schema.get("properties", {})):
        sec_fields = [f for f in fields if f.section == sec_name]
        req = sum(1 for f in sec_fields if f.required)
        lines.append(f"  {sec_name}: {len(sec_fields)} fields ({req} required)")
    return "\n".join(lines)


# ===== Instantiate on import =====
rfp_schema = load_schema()
ALL_FIELDS = flatten_fields(rfp_schema)

if __name__ == "__main__":
    print(summary())
