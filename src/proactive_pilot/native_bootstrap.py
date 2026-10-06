"""Bounded native MCP initialization; no client, grant changes or home pinning.

All returned data is projected onto booleans, fixed enums, names and scope hashes.
Private ledgers are read only to verify the exact key used by native call_mcp.
No transport/tool call is made except the supported discovery entry point.
"""
from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import io
import re
from typing import Any


class NativeBootstrapError(RuntimeError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = details


def _hash(value):
    return "none" if value is None else hashlib.sha256(str(value).encode()).hexdigest()


def _names(values):
    return sorted(n for n in values if isinstance(n, str)
                  and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", n))


@contextmanager
def quiet_host():
    """Prevent native diagnostics from emitting config, endpoints or exception text.

    Discovery is synchronous at this seam. Suppression also covers host loop logs.
    Restore the caller's logging threshold even when the host raises.
    """
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            yield
    finally:
        logging.disable(previous)


def runtime_snapshot(server):
    from tools import mcp_tool_discovery as discovery
    result = {"status": "unknown", "ledger_available": False,
              "dispatchable": False, "scope_hash": "unknown"}
    try:
        # Status may contain raw errors/metrics: retain only a fixed status enum.
        for entry in discovery.get_mcp_status() or []:
            if entry.get("name") == server:
                status = entry.get("status")
                if status in ("connected", "lazy", "disabled", "connecting", "failed", "configured"):
                    result["status"] = status
    except Exception:
        pass
    try:
        core = discovery._core
        key = discovery._resolve_server_key(server)
        with core._lock:
            live = core._servers.get(key)
            lazy = key in core._lazy_server_configs
            result.update({
                "ledger_available": True,
                "scope_hash": _hash(core._mcp_registry_scope()),
                "resolved_key_hash": _hash(key),
                "registered_key_hashes": sorted(_hash(k) for k in core._servers
                                                if discovery._key_name(k) == server),
                "dispatchable": bool(lazy or (live is not None and live.session is not None)),
                "connecting": key in core._server_connecting,
                "connect_error_present": key in core._server_connect_errors,
                "cooldown_active": bool(discovery._connect_cooldown_active(server)),
            })
    except Exception:
        # A host revision without this ledger cannot be certified by status alone.
        pass
    try:
        from hermes_constants import process_hermes_home_is_pinned
        result["process_home_pinned"] = bool(process_hermes_home_is_pinned())
        from agent.secret_scope import serves_routed_profile, is_multiplex_active, current_secret_scope_home
        result["routed_profile"] = bool(serves_routed_profile())
        result["multiplex_active"] = bool(is_multiplex_active())
        result["secret_scope_home_hash"] = _hash(current_secret_scope_home())
    except Exception:
        result["routing_observation_unavailable"] = True
    return result


def config_snapshot(server):
    """Parent/runtime query only; never return config values or credential material."""
    from tools import mcp_tool_discovery as discovery
    result = {"config_observed": False}
    try:
        # Same effective config and owner scope used by discover_mcp_tools.
        with discovery._owner_secret_scope():
            configured = discovery._config._load_mcp_config()
        cfg = configured.get(server)
        result.update({"config_observed": True, "effective_config_empty": not bool(configured),
                       "configured_server_names": _names(configured),
                       "target_configured": server in configured,
                       "target_enabled": bool(cfg is not None and discovery.mcp_server_enabled(cfg)),
                       "target_lazy": bool(cfg is not None and discovery._resolve_server_lazy(server, cfg))})
    except Exception:
        result["config_query_failed"] = True
    try:
        from hermes_cli.config import load_config
        raw = load_config()
        servers = raw.get("mcp_servers") or {}
        result["raw_target_configured"] = server in servers
        result["raw_server_names"] = _names(servers)
        result["mcp_setting_names"] = _names(raw.get("mcp") or {})
    except Exception:
        result["raw_config_query_failed"] = True
    return result


def bootstrap_native(context, server="firecrawl"):
    """Discover ONLY server in the caller's unchanged call-time context."""
    if not isinstance(server, str) or not _names([server]):
        return {"ok": False, "reason": "invalid_server_name"}
    report = {"ok": False, "server": server, "context_captured": context is not None,
              "scope_mismatch": False, "parallel_client": False, "grant_changes": False}
    if context is None:
        report["reason"] = "context_not_captured"
        return report
    with quiet_host():
        try:
            try:
                report["grant_allowed"] = server in context._mcp_allowlist(context.plugin_id)
            except Exception:
                report["grant_allowed"] = False
            if not report["grant_allowed"]:
                report["reason"] = "native_grant_missing"
                return report
            from tools import mcp_tool_discovery as discovery
            config = config_snapshot(server)
            report["config"] = config
            before = runtime_snapshot(server)
            report["before"] = before
            names = list(discovery.discover_mcp_tools(allowed_mcp_names=[server]) or [])
            report["discovered_tools"] = _names(names)
            report["discovery_empty"] = not bool(names)
            after = runtime_snapshot(server)
            report["status_at_call_scope"] = after
            report["scope_mismatch"] = before["scope_hash"] != after["scope_hash"]
            # Fail closed if the exact dispatch ledger cannot be inspected.
            report["ok"] = bool(context is not None and after["ledger_available"]
                                and after["dispatchable"] and not report["scope_mismatch"])
            if report["ok"]:
                reason = "ready"
            elif context is None:
                reason = "context_not_captured"
            elif report["scope_mismatch"]:
                reason = "scope_changed"
            elif config.get("effective_config_empty"):
                reason = "effective_config_empty"
            elif config.get("target_configured") is False:
                reason = "target_filtered_out"
            elif config.get("target_enabled") is False:
                reason = "target_disabled"
            elif getattr(getattr(discovery, "_core", None), "_MCP_AVAILABLE", None) is False:
                reason = "sdk_unavailable"
            elif after.get("cooldown_active"):
                reason = "retry_cooldown"
            elif after.get("connecting"):
                reason = "connecting"
            elif after.get("connect_error_present") or after["status"] == "failed":
                reason = "native_connect_failed_not_connected"
            elif not after["ledger_available"]:
                reason = "dispatch_ledger_unavailable"
            else:
                reason = "not_connected_after_discovery"
            report["reason"] = reason
            core = getattr(discovery, "_core", None)
            report["sdk_available"] = bool(getattr(core, "_MCP_AVAILABLE", False))
            report["sdk_import_attempted"] = bool(getattr(core, "_MCP_SDK_IMPORT_ATTEMPTED", False))
        except Exception:
            report.update(ok=False, reason="native_discovery_exception")
    return report


def require_native(context, server="firecrawl"):
    report = bootstrap_native(context, server)
    if not report["ok"]:
        raise NativeBootstrapError(report["reason"], **report)
    return context
