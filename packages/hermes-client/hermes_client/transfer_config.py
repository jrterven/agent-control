"""Bounded, secret-free native configuration for profile transfers.

The dashboard's normalized config loses model routing fields. Transfer snapshots
must come from its raw document and must never expose that document or its path.
"""
from __future__ import annotations

import math
from typing import Any

import yaml

from .admin import contains_secret_fields, sanitize_admin_payload, writable_config_projection
from .limits import (
    MAX_UPSTREAM_JSON_DEPTH, MAX_UPSTREAM_JSON_NODES, MAX_UPSTREAM_STRING_BYTES,
    UpstreamPayloadError, UpstreamPayloadTooLarge, validate_json_shape,
)


class _ConfigLoader(yaml.SafeLoader):
    """Limit YAML composition before constructors recurse or expand aliases."""

    def __init__(self, value: str):
        super().__init__(value)
        self.config_depth = 0
        self.config_nodes = 0

    def compose_node(self, parent, index):
        self.config_depth += 1
        self.config_nodes += 1
        try:
            if (self.config_depth > MAX_UPSTREAM_JSON_DEPTH
                    or self.config_nodes > MAX_UPSTREAM_JSON_NODES
                    or self.check_event(yaml.AliasEvent)):
                raise UpstreamPayloadError("Hermes transfer config exceeds the YAML structure limits")
            return super().compose_node(parent, index)
        finally:
            self.config_depth -= 1

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise UpstreamPayloadError("Hermes transfer config has invalid or duplicate keys")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def project_transfer_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UpstreamPayloadError("Hermes transfer config must be a mapping")
    validate_json_shape(value)
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise UpstreamPayloadError("Hermes transfer config keys must be strings")
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif item is not None and type(item) not in {str, bool, int, float}:
            raise UpstreamPayloadError("Hermes transfer config contains unsupported values")
        elif isinstance(item, float) and not math.isfinite(item):
            raise UpstreamPayloadError("Hermes transfer config contains nonfinite values")
    security = value.get("security")
    if (isinstance(security, dict) and "redact_secrets" in security
            and type(security["redact_secrets"]) is not bool):
        raise UpstreamPayloadError("Hermes secret-redaction preference must be a boolean")
    projected = writable_config_projection(sanitize_admin_payload(value))
    if contains_secret_fields(projected):
        raise UpstreamPayloadError("Hermes transfer config contains unsupported secret fields")
    return projected


def parse_transfer_config(text: Any) -> dict[str, Any]:
    if not isinstance(text, str):
        raise UpstreamPayloadError("Hermes raw config must contain YAML text")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError:
        raise UpstreamPayloadError("Hermes transfer config is invalid text") from None
    if size > MAX_UPSTREAM_STRING_BYTES:
        raise UpstreamPayloadTooLarge("Hermes transfer config is too large")
    loader = _ConfigLoader(text)
    try:
        value = loader.get_single_data()
    except UpstreamPayloadError:
        raise
    except (yaml.YAMLError, RecursionError, ValueError, TypeError):
        # Parser messages may quote credentials or lines from the private YAML.
        raise UpstreamPayloadError("Hermes transfer config is invalid YAML") from None
    finally:
        loader.dispose()
    return project_transfer_config(value)
