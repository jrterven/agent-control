"""Pure config helpers from audited Hermes 939e45c91d751fadd94dcd1b873ac3cb44846213.

Extracted from hermes_cli/config.py, web_server_config.py and web_routers/models.py;
tests compile their AST without importing or executing the Hermes runtime.
"""
from typing import Any, Dict


def _normalize_config_for_web(config: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten a dict-form model to its string form for the dashboard."""
    config = dict(config)
    model_val = config.get("model")
    if isinstance(model_val, dict):
        ctx_len = model_val.get("context_length", 0)
        config["model"] = model_val.get("default", model_val.get("name", ""))
        config["model_context_length"] = ctx_len if isinstance(ctx_len, int) else 0
    else:
        config["model_context_length"] = 0
    return config


def _main_model_fields(model_cfg) -> tuple[str, str]:
    """(model, provider) from config's model section, which may be a plain string."""
    if isinstance(model_cfg, dict):
        return model_cfg.get("default", model_cfg.get("name", "")), model_cfg.get("provider", "")
    return (str(model_cfg) if model_cfg else ""), ""


def _normalize_max_turns_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Move legacy root-level ``max_turns`` under ``agent``; the schema default is injected only
    when the user set max_turns somewhere (so save_config can otherwise omit it)."""
    config = dict(config)
    agent_config = dict(config.get("agent") or {})
    if "max_turns" in config and "max_turns" not in agent_config:
        agent_config["max_turns"] = config["max_turns"]
    config["agent"] = agent_config
    config.pop("max_turns", None)
    return config
