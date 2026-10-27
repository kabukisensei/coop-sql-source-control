"""sqlsc.yml, and the coop project contract it can borrow targets from.

A repo linked to a database has an `sqlsc.yml` at its root:

    folder: database      # object scripts live here, relative to this file
    layout: type          # optional: `type` (Tables/dbo.Customer.sql, the default) or
                          # `schema` (dbo/Tables/Customer.sql, what SSMS and SqlPackage
                          # write); left out, the files in the folder decide
    target: dev           # the target commands use when none is named
    targets:              # optional when .coop/project.yml has sql_targets
      dev:
        kind: sql_server
        server: localhost,1433
        database: Shop
        auth: sql
        user: sa
        password_env: SQLSC_PASSWORD
    locks:                # optional: where checkouts are recorded
      target: shared      # default: the target being worked on
      repo: sales-db      # default: the git origin URL
    schemas: [sales, finance]           # optional: only these schemas
    exclude_schemas: [staging]          # optional: never these (wins over schemas)

When the repo also has coop's `.coop/project.yml` (here or in a parent folder),
its `sql_targets` entries are available by name too. An entry in sqlsc.yml with
the same name is laid over the coop one field by field, which is how a Fabric
target gets the `server` coop itself discovers through the Fabric API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_NAME = "sqlsc.yml"
COOP_CONTRACT = Path(".coop") / "project.yml"


class ConfigError(Exception):
    """A problem the user fixes in a config file; the message says how."""


# What each kind of target can hold. `tables` False means tables are not ours to
# script (a Lakehouse SQL endpoint's tables come from Delta and are read-only).
KINDS: dict[str, dict[str, Any]] = {
    "sql_server": {
        "label": "SQL Server",
        "tables": True,
        "indexes": True,
        "triggers": True,
        "enforced_keys": True,
        "default_auth": "windows",
    },
    "azure_sql": {
        "label": "Azure SQL Database",
        "tables": True,
        "indexes": True,
        "triggers": True,
        "enforced_keys": True,
        "default_auth": "entra",
    },
    "fabric_sql_database": {
        "label": "Fabric SQL database",
        "tables": True,
        "indexes": True,
        "triggers": True,
        "enforced_keys": True,
        "default_auth": "entra",
    },
    "fabric_warehouse": {
        "label": "Fabric Warehouse",
        "tables": True,
        "indexes": False,
        "triggers": False,
        "enforced_keys": False,
        "default_auth": "entra",
    },
    "fabric_lakehouse": {
        "label": "Fabric Lakehouse SQL endpoint",
        "tables": False,
        "indexes": False,
        "triggers": False,
        "enforced_keys": False,
        "default_auth": "entra",
    },
    "synapse_serverless": {
        "label": "Synapse serverless SQL pool",
        "tables": False,
        "indexes": False,
        "triggers": False,
        "enforced_keys": False,
        "default_auth": "entra",
    },
}

AUTH_MODES = ("sql", "windows", "entra", "entra-interactive")
PROTECTED_NAMES = {"prod", "production", "prd", "live"}


@dataclass
class Target:
    name: str
    kind: str
    server: str
    database: str
    auth: str
    user: str = ""
    password_env: str = "SQLSC_PASSWORD"
    tenant_id: str = ""
    trust_server_certificate: bool = False
    protected: bool = False
    source: str = CONFIG_NAME
    # From sqlsc.yml's schemas / exclude_schemas, lowercased: SQL Server's
    # default collation compares names without case.
    include_schemas: frozenset[str] = frozenset()
    ignore_schemas: frozenset[str] = frozenset()

    def keeps_schema(self, schema: str) -> bool:
        """False for a schema sqlsc.yml leaves out: it is neither scripted, compared nor applied."""
        name = schema.lower()
        if name in self.ignore_schemas:
            return False
        return not self.include_schemas or name in self.include_schemas

    @property
    def caps(self) -> dict[str, Any]:
        return KINDS[self.kind]

    @property
    def label(self) -> str:
        return KINDS[self.kind]["label"]

    def summary(self) -> dict[str, Any]:
        """Safe to print or return to an agent: no password, no secret."""
        return {
            "name": self.name,
            "kind": self.kind,
            "server": self.server,
            "database": self.database,
            "auth": self.auth,
            "protected": self.protected,
            "source": self.source,
        }


@dataclass
class Config:
    path: Path
    folder: str = "database"
    layout: str = ""  # `type`, `schema`, or "" to go by the files (see resolved_layout)
    default_target: str = ""
    targets: dict[str, dict[str, Any]] = field(default_factory=dict)
    coop_targets: dict[str, dict[str, Any]] = field(default_factory=dict)
    coop_path: Path | None = None
    coop_tenant_id: str = ""
    lock_target: str = ""
    lock_repo: str = ""
    include_schemas: frozenset[str] = frozenset()
    ignore_schemas: frozenset[str] = frozenset()

    @property
    def root(self) -> Path:
        return self.path.parent

    @property
    def scripts_dir(self) -> Path:
        return (self.root / self.folder).resolve()

    def resolved_layout(self) -> str:
        """The layout this repo's files are in: `layout:` when set, else what the scripts
        folder holds (an empty or unknown folder is `type`, sqlsc's own default)."""
        from . import layout as layout_mod

        if self.layout:
            return self.layout
        return layout_mod.detect_layout(layout_mod.sql_files(self.scripts_dir)) or layout_mod.TYPE_LAYOUT

    def target_names(self) -> list[str]:
        names = list(self.targets)
        names += [n for n in self.coop_targets if n not in self.targets]
        return names

    def target(self, name: str | None = None) -> Target:
        chosen = name or self.default_target
        if not chosen:
            raise ConfigError(f"No target named. Pass --target, or set `target:` in {self.path.name}.")
        base = dict(self.coop_targets.get(chosen) or {})
        own = self.targets.get(chosen) or {}
        if not base and not own:
            known = ", ".join(self.target_names()) or "none"
            raise ConfigError(f"There is no target called '{chosen}'. Known targets: {known}.")
        merged = {**base, **own}
        if base and self.coop_tenant_id and not _text(merged.get("tenant_id")):
            merged["tenant_id"] = self.coop_tenant_id
        _fill_fabric_server(chosen, merged)
        source = (
            CONFIG_NAME
            if own and not base
            else ("coop project.yml" if base and not own else f"coop project.yml + {CONFIG_NAME}")
        )
        target = build_target(chosen, merged, source=source)
        target.include_schemas = self.include_schemas
        target.ignore_schemas = self.ignore_schemas
        return target


def build_target(name: str, raw: dict[str, Any], *, source: str = CONFIG_NAME) -> Target:
    kind = _text(raw.get("kind"))
    if kind not in KINDS:
        raise ConfigError(f"Target '{name}' has kind '{kind or '(blank)'}'. Use one of: {', '.join(KINDS)}.")
    server = _text(raw.get("server"))
    database = _text(raw.get("database"))
    if not server or server.upper().startswith("TODO"):
        hint = ""
        if kind in ("fabric_warehouse", "fabric_lakehouse"):
            hint = (
                " For Fabric, copy the SQL connection string from the item's settings in the"
                f" Fabric portal and add `server: <host>` under targets.{name} in {CONFIG_NAME}."
            )
        raise ConfigError(f"Target '{name}' has no server.{hint}")
    if not database or database.upper().startswith("TODO"):
        raise ConfigError(f"Target '{name}' has no database.")
    user = _text(raw.get("user"))
    auth = _text(raw.get("auth"))
    if not auth:
        auth = "sql" if (kind == "sql_server" and user) else KINDS[kind]["default_auth"]
    if auth not in AUTH_MODES:
        raise ConfigError(f"Target '{name}' has auth '{auth}'. Use one of: {', '.join(AUTH_MODES)}.")
    if auth == "sql" and not user:
        raise ConfigError(f"Target '{name}' uses SQL authentication but has no `user`.")
    return Target(
        name=name,
        kind=kind,
        server=server,
        database=database,
        auth=auth,
        user=user,
        password_env=_text(raw.get("password_env")) or "SQLSC_PASSWORD",
        tenant_id=_text(raw.get("tenant_id")),
        trust_server_certificate=bool(raw.get("trust_server_certificate", False)),
        # Production is read-only for get-latest unless the entry says otherwise.
        protected=bool(raw.get("protected", name.lower() in PROTECTED_NAMES)),
        source=source,
    )


def _fill_fabric_server(name: str, raw: dict[str, Any]) -> None:
    """A Fabric Warehouse, Lakehouse or SQL database entry with workspace_id and item_id but
    no server (how coop's contract names them) gets its SQL host from the Fabric API. A SQL
    database also gets its name on the server, which is not its display name."""
    kind = _text(raw.get("kind"))
    server = _text(raw.get("server"))
    workspace_id, item_id = _text(raw.get("workspace_id")), _text(raw.get("item_id"))
    fabric_kinds = ("fabric_warehouse", "fabric_lakehouse", "fabric_sql_database")
    if kind not in fabric_kinds or (server and not server.upper().startswith("TODO")):
        return
    if not workspace_id or not item_id:
        return  # build_target explains the missing server
    from .fabric_api import FabricLookupError, lookup_database, lookup_server

    tenant = _text(raw.get("tenant_id"))
    try:
        raw["server"] = lookup_server(kind, workspace_id, item_id, tenant)
        if kind == "fabric_sql_database":
            raw["database"] = lookup_database(kind, workspace_id, item_id, tenant) or raw.get("database")
    except FabricLookupError as exc:
        raise ConfigError(
            f"Target '{name}' has no server, and looking it up from workspace_id and item_id failed: {exc}. "
            f"Add `server: <host>` under targets.{name} in {CONFIG_NAME}."
        ) from exc


def find_config(start: Path | None = None) -> Path | None:
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        candidate = folder / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def find_coop_contract(start: Path) -> Path | None:
    for folder in (start.resolve(), *start.resolve().parents):
        candidate = folder / COOP_CONTRACT
        if candidate.is_file():
            return candidate
    return None


def load(start: Path | None = None, *, config_path: Path | None = None) -> Config:
    if config_path is not None and not config_path.is_file():
        raise ConfigError(f"{config_path} does not exist. Run `sqlsc init` to create it.")
    path = config_path or find_config(start)
    if path is None:
        raise ConfigError(f"No {CONFIG_NAME} found here or in any parent folder. Run `sqlsc init` in the repo first.")
    raw = _read_yaml(path)
    config = Config(
        path=path,
        folder=_text(raw.get("folder")) or "database",
        layout=_text(raw.get("layout")).lower(),
        default_target=_text(raw.get("target")),
        targets=_mapping(raw.get("targets"), f"`targets` in {path.name}"),
    )
    if config.layout and config.layout not in ("type", "schema"):
        raise ConfigError(f"`layout` in {path.name} must be `type` or `schema`, not '{config.layout}'.")
    locks = _mapping(raw.get("locks"), f"`locks` in {path.name}")
    config.lock_target = _text(locks.get("target"))
    config.lock_repo = _text(locks.get("repo"))
    config.include_schemas = _schema_list(raw.get("schemas"), f"`schemas` in {path.name}")
    config.ignore_schemas = _schema_list(raw.get("exclude_schemas"), f"`exclude_schemas` in {path.name}")
    coop = find_coop_contract(path.parent)
    if coop is not None:
        contract = _read_yaml(coop)
        sql_targets = _mapping(contract.get("sql_targets"), "`sql_targets` in .coop/project.yml")
        config.coop_targets = {k: v for k, v in sql_targets.items() if isinstance(v, dict)}
        config.coop_path = coop
        config.coop_tenant_id = _text(
            _mapping(contract.get("fabric"), "`fabric` in .coop/project.yml").get("tenant_id")
        )
        if not config.default_target:
            config.default_target = _text(sql_targets.get("default_environment"))
    return config


def write_new(
    path: Path,
    *,
    folder: str,
    target: str,
    target_entry: dict[str, Any] | None,
    filters: dict[str, list[str]] | None = None,
    layout: str = "",
) -> None:
    lines = [
        "# coop-sql-source-control: links this repo to a database.",
        f"folder: {folder}",
    ]
    if layout:
        lines.append(f"layout: {layout}")
    lines.append(f"target: {target}")
    for key, names in (filters or {}).items():
        lines.append(f"{key}: {_yaml_scalar(list(names))}")
    if target_entry:
        lines.append("targets:")
        lines.append(f"  {target}:")
        for key, value in target_entry.items():
            if value in (None, ""):
                continue
            lines.append(f"    {key}: {_yaml_scalar(value)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def password_for(target: Target) -> str:
    value = os.environ.get(target.password_env, "")
    if not value:
        raise ConfigError(
            f"Target '{target.name}' uses SQL authentication. Set the password in the "
            f"{target.password_env} environment variable (it is never stored in {CONFIG_NAME})."
        )
    return value


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} should hold a mapping of settings.")
    return data


def _mapping(value: Any, what: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{what} should be a mapping.")
    return value


def _schema_list(value: Any, what: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"{what} should be a list of schema names, like [sales, finance].")
    return frozenset(v.strip().lower() for v in value)


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return yaml.safe_dump(value, default_flow_style=True).strip().removesuffix("...").strip()
