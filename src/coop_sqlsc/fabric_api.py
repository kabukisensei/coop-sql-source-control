"""Finding a Fabric item's SQL host from its workspace and item ids.

coop's `.coop/project.yml` names Fabric Warehouse and Lakehouse targets by
`workspace_id` and `item_id`, not by host. When such a target has no `server`,
sqlsc asks the Fabric REST API for the item's SQL connection string with the
same Entra sign-in it uses for SQL, so coop's targets work as they are.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

FABRIC_API = "https://api.fabric.microsoft.com/v1"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
_cache: dict[tuple[str, str, str], str] = {}


class FabricLookupError(Exception):
    """The item could not be found or read; the message says what to check."""


def item_path(kind: str, workspace_id: str, item_id: str) -> str:
    collection = "warehouses" if kind == "fabric_warehouse" else "lakehouses"
    return f"{FABRIC_API}/workspaces/{workspace_id}/{collection}/{item_id}"


def connection_host(kind: str, item: dict[str, Any]) -> str:
    """The SQL host from a Get Warehouse / Get Lakehouse response."""
    props = item.get("properties") or {}
    if kind == "fabric_lakehouse":
        props = props.get("sqlEndpointProperties") or {}
    host = str(props.get("connectionString") or "").strip()
    if host.lower().startswith("tcp:"):
        host = host[4:]
    return host.split(",")[0]


def lookup_server(kind: str, workspace_id: str, item_id: str, tenant_id: str = "") -> str:
    key = (kind, workspace_id.lower(), item_id.lower())
    if key in _cache:
        return _cache[key]
    url = item_path(kind, workspace_id, item_id)
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {_token(tenant_id)}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            item = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise FabricLookupError(f"the Fabric API answered {exc.code} for {url}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise FabricLookupError(f"the Fabric API could not be read ({exc})") from exc
    host = connection_host(kind, item)
    if not host:
        raise FabricLookupError("the item has no SQL connection string yet (is its SQL endpoint provisioned?)")
    _cache[key] = host
    return host


def _token(tenant_id: str) -> str:
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:  # pragma: no cover
        raise FabricLookupError("azure-identity is missing; reinstall this tool") from exc
    kwargs: dict[str, Any] = {"exclude_interactive_browser_credential": True}
    if tenant_id:
        kwargs["additionally_allowed_tenants"] = [tenant_id]
    try:
        return DefaultAzureCredential(**kwargs).get_token(FABRIC_SCOPE, tenant_id=tenant_id or None).token
    except Exception as exc:  # azure-identity raises several unrelated types
        raise FabricLookupError(f"no Entra ID token for the Fabric API; run `az login` ({exc})") from exc
