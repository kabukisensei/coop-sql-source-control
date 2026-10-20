"""Finding a Fabric item's SQL host from its workspace and item ids.

coop's `.coop/project.yml` names Fabric Warehouse, Lakehouse and SQL database
targets by `workspace_id` and `item_id`, not by host. When such a target has no
`server`, sqlsc asks the Fabric REST API for the item's SQL connection string
(and, for a SQL database, its real database name) with the same Entra sign-in it
uses for SQL, so coop's targets work as they are.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

FABRIC_API = "https://api.fabric.microsoft.com/v1"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
COLLECTIONS = {
    "fabric_warehouse": "warehouses",
    "fabric_lakehouse": "lakehouses",
    "fabric_sql_database": "sqlDatabases",
}


class FabricLookupError(Exception):
    """The item could not be found or read; the message says what to check."""


def item_path(kind: str, workspace_id: str, item_id: str) -> str:
    return f"{FABRIC_API}/workspaces/{workspace_id}/{COLLECTIONS[kind]}/{item_id}"


def connection_host(kind: str, item: dict[str, Any]) -> str:
    """The SQL host from a Get Warehouse / Lakehouse / SQL Database response."""
    props = item.get("properties") or {}
    if kind == "fabric_lakehouse":
        props = props.get("sqlEndpointProperties") or {}
    field = "serverFqdn" if kind == "fabric_sql_database" else "connectionString"
    host = str(props.get(field) or "").strip()
    if host.lower().startswith("tcp:"):
        host = host[4:]
    return host.split(",")[0]


def database_name(kind: str, item: dict[str, Any]) -> str:
    """A Fabric SQL database's name on its server, which is not its display name
    ("Sales-<item id>"); empty for the other kinds, whose name is the display name."""
    if kind != "fabric_sql_database":
        return ""
    return str((item.get("properties") or {}).get("databaseName") or "").strip()


def lookup_server(kind: str, workspace_id: str, item_id: str, tenant_id: str = "") -> str:
    return connection_host(kind, _item(kind, workspace_id, item_id, tenant_id))


def lookup_database(kind: str, workspace_id: str, item_id: str, tenant_id: str = "") -> str:
    return database_name(kind, _item(kind, workspace_id, item_id, tenant_id))


def _item(kind: str, workspace_id: str, item_id: str, tenant_id: str) -> dict[str, Any]:
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
    if not connection_host(kind, item):
        raise FabricLookupError("the item has no SQL connection string yet (is its SQL endpoint provisioned?)")
    _cache[key] = item
    return item


def _token(tenant_id: str) -> str:
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:  # pragma: no cover
        raise FabricLookupError("azure-identity is missing; reinstall this tool") from exc
    from .connect import quiet_azure_identity, token_reason

    quiet_azure_identity()
    kwargs: dict[str, Any] = {"exclude_interactive_browser_credential": True}
    if tenant_id:
        kwargs["additionally_allowed_tenants"] = [tenant_id]
    try:
        return DefaultAzureCredential(**kwargs).get_token(FABRIC_SCOPE, tenant_id=tenant_id or None).token
    except Exception as exc:  # azure-identity raises several unrelated types
        raise FabricLookupError(f"no Entra ID token for the Fabric API; run `az login` ({token_reason(exc)})") from exc
