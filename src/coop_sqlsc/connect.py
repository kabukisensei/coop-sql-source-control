"""Opening a connection to a target over ODBC Driver 18.

Entra ID targets (Azure SQL, Fabric, Synapse) get an access token for the
`https://database.windows.net/` audience from azure-identity, which tries the
same sign-ins coop does: `az login`, environment service principal, managed
identity. The token goes to the driver as SQL_COPT_SS_ACCESS_TOKEN, so no
password is ever in the connection string.
"""

from __future__ import annotations

import re
import struct
from typing import Any

from .config import ConfigError, Target, password_for

SQL_COPT_SS_ACCESS_TOKEN = 1256
TOKEN_SCOPE = "https://database.windows.net/.default"
_DRIVER_RE = re.compile(r"^ODBC Driver (\d+) for SQL Server$")


class ConnectError(Exception):
    """The target could not be reached; the message says what to check."""


def pick_driver(drivers: list[str]) -> str | None:
    versions = sorted((int(m.group(1)), d) for d in drivers if (m := _DRIVER_RE.match(d)) and int(m.group(1)) >= 18)
    return versions[-1][1] if versions else None


def connection_string(target: Target, driver: str) -> str:
    parts = [
        f"Driver={{{driver}}}",
        f"Server={_server(target)}",
        f"Database={{{target.database}}}",
        "Encrypt=yes",
        f"TrustServerCertificate={'yes' if target.trust_server_certificate else 'no'}",
        f"Connection Timeout={60 if target.kind == 'azure_sql' else 20}",
        "APP=coop-sql-source-control",
    ]
    if target.auth == "sql":
        parts.append(f"UID={{{target.user}}}")
    elif target.auth == "windows":
        parts.append("Trusted_Connection=yes")
    elif target.auth == "entra-interactive":
        parts.append("Authentication=ActiveDirectoryInteractive")
        if target.user:
            parts.append(f"UID={{{target.user}}}")
    return ";".join(parts) + ";"


def _server(target: Target) -> str:
    server = target.server
    if target.kind != "sql_server" and "," not in server and not server.lower().startswith("tcp:"):
        server = f"tcp:{server},1433"
    return server


def open_connection(target: Target) -> Any:
    try:
        import pyodbc
    except ImportError as exc:  # pragma: no cover - depends on the machine
        raise ConnectError(
            "pyodbc could not load. Install the ODBC Driver 18 for SQL Server "
            "(winget install --id Microsoft.msodbcsql.18 -e) and reinstall this tool."
        ) from exc

    driver = pick_driver(list(pyodbc.drivers()))
    if driver is None:
        raise ConnectError(
            "ODBC Driver 18 for SQL Server is not installed. On Windows: winget install --id Microsoft.msodbcsql.18 -e"
        )
    conn_str = connection_string(target, driver)
    attrs: dict[int, Any] = {}
    if target.auth == "sql":
        conn_str += f"PWD={{{_escape(password_for(target))}}};"
    elif target.auth == "entra":
        attrs[SQL_COPT_SS_ACCESS_TOKEN] = pack_token(entra_token(target))
    # Each run is one short process, so pooling buys nothing; and with it on, the
    # ODBC driver manager asks COM for the caller's transaction context, which can
    # hang when sqlsc runs under a GUI host such as SSMS.
    pyodbc.pooling = False
    try:
        conn = pyodbc.connect(conn_str, attrs_before=attrs or None, autocommit=True)
    except pyodbc.Error as exc:
        raise ConnectError(_explain(target, exc)) from exc
    return conn


def entra_token(target: Target) -> str:
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:  # pragma: no cover
        raise ConnectError("azure-identity is missing; reinstall this tool.") from exc
    kwargs: dict[str, Any] = {"exclude_interactive_browser_credential": True}
    if target.tenant_id:
        kwargs["additionally_allowed_tenants"] = [target.tenant_id]
    try:
        credential = DefaultAzureCredential(**kwargs)
        token = credential.get_token(TOKEN_SCOPE, tenant_id=target.tenant_id or None)
    except Exception as exc:  # azure-identity raises several unrelated types
        raise ConnectError(
            f"Could not get an Entra ID token for '{target.name}'. Run `az login` "
            f"(add --tenant <id> for another tenant) and try again. Details: {exc}"
        ) from exc
    return token.token


def pack_token(token: str) -> bytes:
    encoded = token.encode("utf-16-le")
    return struct.pack("<I", len(encoded)) + encoded


def _escape(value: str) -> str:
    return value.replace("}", "}}")


def _explain(target: Target, exc: Exception) -> str:
    text = str(exc)
    if "Login failed" in text or "18456" in text:
        hint = "the server rejected the sign-in; check the user, password or Entra account"
    elif "certificate" in text.lower():
        hint = (
            "the server's certificate is not trusted; for a local dev server set "
            f"`trust_server_certificate: true` under targets.{target.name}"
        )
    elif "timeout" in text.lower() or "TCP Provider" in text:
        hint = "the server did not answer; check the server name, firewall and VPN"
    elif "Cannot open database" in text or "4060" in text:
        hint = f"the database '{target.database}' does not exist or you cannot open it"
    else:
        hint = "see the driver message"
    return f"Could not connect to {target.label} '{target.name}' ({target.server}): {hint}. Driver said: {text}"


__all__ = ["ConnectError", "ConfigError", "open_connection", "connection_string", "pick_driver", "pack_token"]
