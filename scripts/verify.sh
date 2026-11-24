#!/usr/bin/env bash
# Replay sqlsc the way a person does and print VERIFY PASS or VERIFY FAIL.
# Offline by default. --sql also drives a real SQL Server.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

SQL=0
KEEP=0
usage() {
  cat <<'EOF'
Usage: scripts/verify.sh [--sql] [--keep]

  (no flags)  Install sqlsc from this checkout and replay the commands that
              do not need a SQL Server.
  --sql       Also replay status, diff, script, commit, and get-latest
              against a real SQL Server.
  --keep      Leave the temp repos and any container this run started.

SQL Server for --sql, in this order:
  1. SQLSC_TEST_SERVER and SQLSC_PASSWORD, when both are set. CI sets these
     (.github/workflows/ci.yml: localhost,1433 and the sa password).
  2. Otherwise Docker runs mcr.microsoft.com/mssql/server:2022-latest the way
     that workflow's service container does, and publishes port 1433.
     ODBC Driver 18 for SQL Server must already be installed.

Prints a VERIFY PASS or VERIFY FAIL summary. Evidence stays under
/tmp/sqlsc-verify-evidence/<run-id> after the temp repos are removed.
EOF
}

for arg in "$@"; do
  case "$arg" in
    --sql) SQL=1 ;;
    --keep) KEEP=1 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "unknown argument: $arg" >&2
      usage >&2
      exit 2
      ;;
  esac
done

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
RUN_HEX="$(printf '%s' "$RUN_ID" | sha256sum | cut -c1-8)"
WORK="${SQLSC_VERIFY_WORK:-/tmp/sqlsc-verify-work-$RUN_ID}"
EVIDENCE="${SQLSC_VERIFY_EVIDENCE:-/tmp/sqlsc-verify-evidence/$RUN_ID}"
OFFLINE_SERVER="203.0.113.10,1433"
OFFLINE_DB="VerifyDb"
SRC_DB="sqlsc_verify_src_${RUN_HEX}"
EMPTY_DB="sqlsc_verify_empty_${RUN_HEX}"
COMMIT_MESSAGE="Script sales.Customer"

FAIL_STEP=""
FAIL_DETAIL=""
OFFLINE_RESULT="not-run"
SQL_RESULT="not-run"
CONTAINER_ID=""
STARTED_SQL=0
OFFLINE_REPO=""
SQL_REPO=""
PY=""
SQLSC=""
COOP=""
SOURCE_VERSION=""

die() {
  FAIL_STEP=$1
  FAIL_DETAIL=$2
  echo "FAIL ${FAIL_STEP}: ${FAIL_DETAIL}" >&2
  exit 1
}

copy_if() {
  local src=$1 dest=$2
  if [[ -f "$src" ]]; then
    mkdir -p "$(dirname "$dest")"
    cp -a "$src" "$dest"
  fi
}

on_exit() {
  local code=$?
  set +e
  if [[ "$OFFLINE_RESULT" == "running" ]]; then OFFLINE_RESULT=fail; fi
  if [[ "$SQL_RESULT" == "running" ]]; then SQL_RESULT=fail; fi

  if [[ -n "$OFFLINE_REPO" && -d "$OFFLINE_REPO" ]]; then
    mkdir -p "$EVIDENCE/offline"
    copy_if "$OFFLINE_REPO/sqlsc.yml" "$EVIDENCE/offline/sqlsc.yml"
    copy_if "$OFFLINE_REPO/.gitattributes" "$EVIDENCE/offline/gitattributes"
    copy_if "$OFFLINE_REPO/.gitignore" "$EVIDENCE/offline/gitignore"
    copy_if "$OFFLINE_REPO/database/.editorconfig" "$EVIDENCE/offline/editorconfig"
    copy_if "$OFFLINE_REPO/database/${OFFLINE_DB}.sqlproj" "$EVIDENCE/offline/${OFFLINE_DB}.sqlproj"
    shopt -s nullglob
    local mig
    for mig in "$OFFLINE_REPO"/database/Migrations/*_verify-seed.sql; do
      cp -a "$mig" "$EVIDENCE/offline/"
    done
    shopt -u nullglob
  fi
  if [[ -n "$SQL_REPO" && -d "$SQL_REPO" ]]; then
    mkdir -p "$EVIDENCE/sql"
    copy_if "$SQL_REPO/database/sales/Tables/Customer.sql" "$EVIDENCE/sql/Customer.sql"
    copy_if "$SQL_REPO/database/Security/sales.sql" "$EVIDENCE/sql/sales.sql"
    if [[ -d "$SQL_REPO/.git" ]]; then
      git -C "$SQL_REPO" log -1 --format='%s' >"$EVIDENCE/sql/commit-subject.txt" 2>/dev/null || true
      git -C "$SQL_REPO" ls-tree -r --name-only HEAD >"$EVIDENCE/sql/commit-files.txt" 2>/dev/null || true
    fi
  fi

  if [[ -n "$PY" && -x "$PY" && -f "$WORK/databases.txt" ]]; then
    "$PY" "$WORK/sqladmin.py" drop-file "$WORK/databases.txt" >"$EVIDENCE/drop.txt" 2>&1 || true
  fi
  if [[ -n "$CONTAINER_ID" && "$KEEP" != "1" ]]; then
    docker rm -f "$CONTAINER_ID" >"$EVIDENCE/docker-rm.txt" 2>&1 || true
  fi

  local work_removed="no"
  if [[ "$KEEP" == "1" ]]; then
    work_removed="kept"
  elif [[ -n "$WORK" && -d "$WORK" ]]; then
    rm -rf "$WORK"
  fi
  if [[ "$KEEP" != "1" && -n "$WORK" && ! -d "$WORK" ]]; then
    work_removed="yes"
  fi

  local verdict="FAIL"
  if [[ "$code" -eq 0 ]]; then verdict="PASS"; fi
  {
    echo "VERIFY ${verdict}"
    echo "offline: ${OFFLINE_RESULT}"
    echo "sql: ${SQL_RESULT}"
    echo "step: ${FAIL_STEP:--}"
    echo "detail: ${FAIL_DETAIL:--}"
    echo "evidence: ${EVIDENCE}"
    echo "work: ${WORK}"
    echo "work_removed: ${work_removed}"
    echo "seconds: ${SECONDS}"
  } >"$EVIDENCE/summary.txt"
  cat "$EVIDENCE/summary.txt"
}
case "$WORK" in
  "$ROOT"|"$ROOT"/*) echo "work directory must not be inside the checkout ($WORK)" >&2; exit 1 ;;
esac
case "$EVIDENCE" in
  "$ROOT"|"$ROOT"/*) echo "evidence directory must not be inside the checkout ($EVIDENCE)" >&2; exit 1 ;;
esac

mkdir -p "$WORK" "$EVIDENCE" "$EVIDENCE/offline" "$EVIDENCE/sql"
trap on_exit EXIT
exec > >(tee -a "$EVIDENCE/transcript.txt") 2>&1

export NO_COLOR=1
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export LC_ALL=C.UTF-8
export PIP_DISABLE_PIP_VERSION_CHECK=1

echo "verify run ${RUN_ID}"
echo "checkout: ${ROOT}"
echo "mode: $([[ "$SQL" -eq 1 ]] && echo '--sql' || echo offline)"

command -v python3 >/dev/null 2>&1 || die launch "python3 is required (3.10 to 3.13)"
command -v git >/dev/null 2>&1 || die launch "git is required"
SOURCE_VERSION="$(python3 - "$ROOT/src/coop_sqlsc/__init__.py" <<'PY'
import pathlib, re, sys
text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.search(r'(?m)^__version__ = "([^"]+)"', text)
print(match.group(1) if match else "", end="")
PY
)"
[[ -n "$SOURCE_VERSION" ]] || die launch "could not read __version__ from src/coop_sqlsc/__init__.py"
echo "source version: ${SOURCE_VERSION}"

echo
echo "--- launch: install from the checkout (no SQL Server) ---"
if ! python3 -m venv "$WORK/venv"; then
  die launch "python3 -m venv failed. Debian/Ubuntu needs the python3-venv package (python3.12-venv); CI's setup-python already includes it."
fi
PY="$WORK/venv/bin/python"
if ! "$WORK/venv/bin/pip" install "$ROOT" >"$EVIDENCE/pip.log" 2>&1; then
  tail -n 40 "$EVIDENCE/pip.log" >&2
  die launch "pip install of the checkout failed; see ${EVIDENCE}/pip.log"
fi
SQLSC="$WORK/venv/bin/sqlsc"
COOP="$WORK/venv/bin/coop-sqlsc"
[[ -x "$SQLSC" && -x "$COOP" ]] || die launch "install did not create sqlsc and coop-sqlsc"
echo "installed: $("$PY" -c 'import coop_sqlsc,sys; print(coop_sqlsc.__file__)')"

cat >"$WORK/assert_json.py" <<'PY'
import json
import sys

doc = json.load(open(sys.argv[1], encoding="utf-8"))
for expr in sys.argv[2:]:
    if not eval(expr, {"doc": doc, "json": json}):
        sys.stderr.write(f"assertion failed: {expr}\n")
        sys.stderr.write(json.dumps(doc, indent=2)[:4000] + "\n")
        raise SystemExit(1)
PY

cat >"$WORK/sqladmin.py" <<'PY'
"""Small SQL Server admin for scripts/verify.sh. Talks to the server the CLI will use."""

import re
import sys

import pyodbc

from coop_sqlsc.connect import pick_driver

pyodbc.pooling = False


def driver() -> str:
    chosen = pick_driver(list(pyodbc.drivers()))
    if not chosen:
        sys.stderr.write(
            "ODBC Driver 18 for SQL Server is not installed. "
            "CI installs it in .github/workflows/ci.yml (the ODBC Driver 18 step).\n"
        )
        raise SystemExit(2)
    return chosen


def connect(database: str):
    import os

    server = os.environ["SQLSC_TEST_SERVER"]
    password = os.environ["SQLSC_PASSWORD"]
    return pyodbc.connect(
        f"Driver={{{driver()}}};Server={server};Database={database};UID=sa;PWD={password};"
        "Encrypt=yes;TrustServerCertificate=yes;Connection Timeout=20;",
        autocommit=True,
    )


def check_name(name: str) -> str:
    if not re.fullmatch(r"sqlsc_verify_[A-Za-z0-9_]+", name):
        raise SystemExit(f"refusing database name {name!r}")
    return name


def main() -> None:
    cmd = sys.argv[1]
    if cmd == "doctor":
        conn = connect("master")
        try:
            row = conn.cursor().execute("SELECT 1").fetchone()
        finally:
            conn.close()
        if not row or row[0] != 1:
            raise SystemExit("SELECT 1 did not return 1")
        print(f"driver: {driver()}")
        print("select1: ok")
        return
    if cmd == "create-source":
        name = check_name(sys.argv[2])
        master = connect("master")
        try:
            master.cursor().execute(f"CREATE DATABASE [{name}]")
        finally:
            master.close()
        conn = connect(name)
        try:
            cur = conn.cursor()
            cur.execute("CREATE SCHEMA sales")
            cur.execute(
                "CREATE TABLE sales.Customer ("
                "CustomerId int NOT NULL CONSTRAINT PK_Customer PRIMARY KEY, "
                "Name nvarchar(100) NOT NULL)"
            )
        finally:
            conn.close()
        print(name)
        return
    if cmd == "create-empty":
        name = check_name(sys.argv[2])
        master = connect("master")
        try:
            master.cursor().execute(f"CREATE DATABASE [{name}]")
        finally:
            master.close()
        print(name)
        return
    if cmd == "has-customer":
        name = check_name(sys.argv[2])
        conn = connect(name)
        try:
            row = conn.cursor().execute("SELECT OBJECT_ID(N'sales.Customer', N'U')").fetchone()
        finally:
            conn.close()
        print("yes" if row and row[0] else "no")
        return
    if cmd == "drop-file":
        path = sys.argv[2]
        try:
            names = [line.strip() for line in open(path, encoding="utf-8") if line.strip()]
        except OSError:
            return
        if not names:
            return
        master = connect("master")
        try:
            cur = master.cursor()
            for name in names:
                name = check_name(name)
                try:
                    cur.execute(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
                    cur.execute(f"DROP DATABASE [{name}]")
                    print(f"dropped {name}")
                except Exception as exc:
                    print(f"drop failed {name}: {exc}")
        finally:
            master.close()
        return
    raise SystemExit(f"unknown sqladmin command {cmd}")


if __name__ == "__main__":
    main()
PY

capture() {
  local label=$1
  shift
  echo
  echo "== ${label} =="
  printf '+'
  printf ' %q' "$@"
  printf '\n'
  set +e
  "$@" >"$WORK/stdout.txt" 2>"$WORK/stderr.txt"
  LAST_CODE=$?
  set -e
  echo "exit=${LAST_CODE}"
  if [[ -s "$WORK/stdout.txt" ]]; then
    cat "$WORK/stdout.txt"
  fi
  if [[ -s "$WORK/stderr.txt" ]]; then
    echo "--- stderr ---"
    cat "$WORK/stderr.txt"
  fi
  cp -a "$WORK/stdout.txt" "$EVIDENCE/last-stdout.txt"
  cp -a "$WORK/stderr.txt" "$EVIDENCE/last-stderr.txt"
}

need_exit() {
  local want=$1
  local step=$2
  if [[ "$LAST_CODE" -ne "$want" ]]; then
    die "$step" "expected exit ${want}, got ${LAST_CODE}"
  fi
}

assert_json() {
  local step=$1
  shift
  if ! "$PY" "$WORK/assert_json.py" "$WORK/stdout.txt" "$@"; then
    die "$step" "JSON assertion failed"
  fi
}

need_file() {
  local path=$1
  local step=$2
  if [[ ! -f "$path" ]]; then
    die "$step" "missing file ${path}"
  fi
}

init_git() {
  local repo=$1
  git init -b main "$repo" >/dev/null
  git -C "$repo" config user.name "sqlsc verify"
  git -C "$repo" config user.email "sqlsc-verify@example.com"
  git -C "$repo" config commit.gpgsign false
  git -C "$repo" config tag.gpgsign false
  git -C "$repo" config core.hooksPath /dev/null
}

echo
echo "--- doctor: version and help (no SQL Server) ---"
capture "sqlsc --version" "$SQLSC" --version
need_exit 0 version
[[ "$(cat "$WORK/stdout.txt")" == "sqlsc, version ${SOURCE_VERSION}" ]] || die version "stdout was $(tr '\n' ' ' <"$WORK/stdout.txt") expected sqlsc, version ${SOURCE_VERSION}"
capture "coop-sqlsc --version" "$COOP" --version
need_exit 0 version
[[ "$(cat "$WORK/stdout.txt")" == "sqlsc, version ${SOURCE_VERSION}" ]] || die version "coop-sqlsc --version did not print the checkout version"
capture "sqlsc --help" "$SQLSC" --help
need_exit 0 help
grep -q "sqlsc init" "$WORK/stdout.txt" || die help "--help did not mention sqlsc init"
grep -q "sqlsc status" "$WORK/stdout.txt" || die help "--help did not mention sqlsc status"
grep -q "sqlsc commit" "$WORK/stdout.txt" || die help "--help did not mention sqlsc commit"
grep -q "sqlsc get-latest" "$WORK/stdout.txt" || die help "--help did not mention sqlsc get-latest"
capture "sqlsc -h" "$SQLSC" -h
need_exit 0 help
grep -q "sqlsc get-latest" "$WORK/stdout.txt" || die help "-h did not mention sqlsc get-latest"
capture "coop-sqlsc --help" "$COOP" --help
need_exit 0 help
grep -q "Source control for SQL Server" "$WORK/stdout.txt" || die help "coop-sqlsc --help did not describe the tool"
capture "sqlsc not-a-command" "$SQLSC" not-a-command
need_exit 2 help
grep -q "No such command" "$WORK/stderr.txt" || die help "unknown command did not report No such command"

echo
echo "--- offline: init, targets, migrations add, sqlproj (no SQL Server) ---"
OFFLINE_RESULT=running
OFFLINE_REPO="$WORK/offline"
init_git "$OFFLINE_REPO"
pushd "$OFFLINE_REPO" >/dev/null
capture "sqlsc init" "$SQLSC" --json init \
  --kind sql_server \
  --server "$OFFLINE_SERVER" \
  --database "$OFFLINE_DB" \
  --auth sql \
  --user sa \
  --trust-server-certificate \
  --layout schema \
  --folder database
popd >/dev/null
need_exit 0 init
assert_json init \
  'doc["ok"] is True' \
  'doc["command"]=="init"' \
  'doc["layout"]=="schema"' \
  'doc["folder"]=="database"' \
  'doc["target"]=="dev"'
need_file "$OFFLINE_REPO/sqlsc.yml" init
need_file "$OFFLINE_REPO/.gitattributes" init
need_file "$OFFLINE_REPO/.gitignore" init
need_file "$OFFLINE_REPO/database/.editorconfig" init
grep -q "database: ${OFFLINE_DB}" "$OFFLINE_REPO/sqlsc.yml" || die init "sqlsc.yml has no database name"
grep -q "layout: schema" "$OFFLINE_REPO/sqlsc.yml" || die init "sqlsc.yml has no schema layout"
grep -q "trust_server_certificate: true" "$OFFLINE_REPO/sqlsc.yml" || die init "sqlsc.yml did not record trust_server_certificate"
grep -q "user: sa" "$OFFLINE_REPO/sqlsc.yml" || die init "sqlsc.yml has no user"
if grep -Ei 'password|PWD=' "$OFFLINE_REPO/sqlsc.yml" >/dev/null; then
  die init "sqlsc.yml contains a password"
fi
[[ "$(cat "$OFFLINE_REPO/.gitattributes")" == "*.sql text diff" ]] || die init ".gitattributes was not the sql text diff line"
grep -Fxq '.sqlsc/' "$OFFLINE_REPO/.gitignore" || die init ".gitignore does not ignore .sqlsc/"
grep -q "charset = utf-8" "$OFFLINE_REPO/database/.editorconfig" || die init ".editorconfig does not set charset = utf-8"

capture "sqlsc targets" timeout 10 "$SQLSC" --json --config "$OFFLINE_REPO/sqlsc.yml" targets
need_exit 0 targets
assert_json targets \
  'doc["ok"] is True' \
  'doc["command"]=="targets"' \
  'len(doc["targets"])==1' \
  'doc["targets"][0]["name"]=="dev"' \
  'doc["targets"][0]["database"]=="VerifyDb"' \
  'doc["targets"][0]["server"]=="203.0.113.10,1433"' \
  'doc["targets"][0]["auth"]=="sql"' \
  'doc["targets"][0]["kind"]=="sql_server"' \
  '"password" not in json.dumps(doc).lower()'
capture "coop-sqlsc targets" timeout 10 "$COOP" --json --config "$OFFLINE_REPO/sqlsc.yml" targets
need_exit 0 targets
assert_json targets 'doc["ok"] is True' 'doc["targets"][0]["database"]=="VerifyDb"'

pushd "$OFFLINE_REPO" >/dev/null
capture "sqlsc init again" "$SQLSC" --json init \
  --kind sql_server \
  --server "$OFFLINE_SERVER" \
  --database "$OFFLINE_DB" \
  --auth sql \
  --user sa
popd >/dev/null
need_exit 2 init
assert_json init 'doc["ok"] is False' 'doc["error"]["kind"]=="config"' ' "already exists" in doc["error"]["message"]'

capture "migrations add" "$SQLSC" --json --config "$OFFLINE_REPO/sqlsc.yml" migrations add verify-seed
need_exit 0 migrations-add
assert_json migrations-add 'doc["ok"] is True' 'doc["command"]=="migrations add"' 'doc["when"]=="after"'
shopt -s nullglob
mig_files=("$OFFLINE_REPO"/database/Migrations/*_verify-seed.sql)
shopt -u nullglob
[[ "${#mig_files[@]}" -eq 1 ]] || die migrations-add "expected one Migrations/*_verify-seed.sql, found ${#mig_files[@]}"
[[ "$(head -n 1 "${mig_files[0]}")" == "-- sqlsc: after" ]] || die migrations-add "migration file does not start with -- sqlsc: after"
grep -q "verify-seed" "$WORK/stdout.txt" || die migrations-add "JSON did not name verify-seed"

capture "sqlproj" "$SQLSC" --json --config "$OFFLINE_REPO/sqlsc.yml" sqlproj
need_exit 0 sqlproj
assert_json sqlproj \
  'doc["ok"] is True' \
  'doc["command"]=="sqlproj"' \
  'doc["written"] is True' \
  'doc["path"]=="database/VerifyDb.sqlproj"'
need_file "$OFFLINE_REPO/database/VerifyDb.sqlproj" sqlproj
grep -q 'Sdk="Microsoft.Build.Sql/2.3.0"' "$OFFLINE_REPO/database/VerifyDb.sqlproj" || die sqlproj "sqlproj SDK pin missing"
grep -q "Sql160DatabaseSchemaProvider" "$OFFLINE_REPO/database/VerifyDb.sqlproj" || die sqlproj "sqlproj platform is not SQL Server"
grep -Fxq 'bin/' "$OFFLINE_REPO/.gitignore" || die sqlproj ".gitignore missing bin/"
grep -Fxq 'obj/' "$OFFLINE_REPO/.gitignore" || die sqlproj ".gitignore missing obj/"
grep -Fxq '.vs/' "$OFFLINE_REPO/.gitignore" || die sqlproj ".gitignore missing .vs/"
OFFLINE_RESULT=pass
echo "offline checks passed"

if [[ "$SQL" -ne 1 ]]; then
  SQL_RESULT="not-run"
  echo
  echo "SQL Server workflows not run. Re-run with --sql to drive status, diff, script, commit, and get-latest."
  exit 0
fi

echo
echo "--- sql: doctor (real SQL Server) ---"
SQL_RESULT=running
if ! "$PY" -c 'import pyodbc; from coop_sqlsc.connect import pick_driver; pyodbc.pooling=False; raise SystemExit(0 if pick_driver(list(pyodbc.drivers())) else 1)'; then
  die sql-odbc "ODBC Driver 18 for SQL Server is not installed. See the ODBC Driver 18 step in .github/workflows/ci.yml."
fi

if [[ -n "${SQLSC_TEST_SERVER:-}" && -n "${SQLSC_PASSWORD:-}" ]]; then
  echo "using SQLSC_TEST_SERVER=${SQLSC_TEST_SERVER}"
else
  command -v docker >/dev/null 2>&1 || die sql-docker "Docker is not installed and SQLSC_TEST_SERVER is unset. Set SQLSC_TEST_SERVER and SQLSC_PASSWORD, or install Docker."
  if "$PY" -c 'import socket,sys; s=socket.socket(); s.settimeout(0.5)
try:
    s.connect(("127.0.0.1", 1433)); sys.exit(0)
except OSError:
    sys.exit(1)'; then
    die sql-docker "port 1433 is already open. Set SQLSC_TEST_SERVER and SQLSC_PASSWORD to use that server. This script will not take over a server it did not start."
  fi
  echo "starting mcr.microsoft.com/mssql/server:2022-latest (same image and password as CI)"
  if ! CONTAINER_ID="$(docker run -d \
    --name "sqlsc-verify-${RUN_HEX}" \
    -e ACCEPT_EULA=Y \
    -e 'MSSQL_SA_PASSWORD=Sqlsc_Ci_Pw1!' \
    -p 1433:1433 \
    --health-cmd "/opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -P 'Sqlsc_Ci_Pw1!' -Q 'SELECT 1' || exit 1" \
    --health-interval 10s \
    --health-timeout 5s \
    --health-retries 18 \
    mcr.microsoft.com/mssql/server:2022-latest)"; then
    die sql-docker "docker run failed"
  fi
  STARTED_SQL=1
  export SQLSC_TEST_SERVER="localhost,1433"
  export SQLSC_PASSWORD='Sqlsc_Ci_Pw1!'
  echo "container: ${CONTAINER_ID}"
fi

ready=0
for _ in $(seq 1 48); do
  if [[ -n "$CONTAINER_ID" ]]; then
    state="$(docker inspect -f '{{.State.Status}}' "$CONTAINER_ID" 2>/dev/null || echo missing)"
    if [[ "$state" == "exited" || "$state" == "dead" || "$state" == "missing" ]]; then
      docker logs "$CONTAINER_ID" 2>&1 | tail -n 50 >&2 || true
      die sql-docker "container status is ${state}"
    fi
    echo "waiting for SQL Server (container ${state})"
  fi
  if "$PY" "$WORK/sqladmin.py" doctor >"$WORK/doctor.txt" 2>"$WORK/doctor.err"; then
    cat "$WORK/doctor.txt"
    ready=1
    break
  fi
  sleep 5
done
[[ "$ready" -eq 1 ]] || die sql-doctor "SQL Server did not accept SELECT 1 (ODBC Driver 18, Encrypt=yes, TrustServerCertificate=yes)"

echo
echo "--- sql: status, diff, script, commit, get-latest ---"
SQL_REPO="$WORK/sql"
init_git "$SQL_REPO"
printf '%s\n' "$SRC_DB" "$EMPTY_DB" >"$WORK/databases.txt"
"$PY" "$WORK/sqladmin.py" create-source "$SRC_DB" >/dev/null
"$PY" "$WORK/sqladmin.py" create-empty "$EMPTY_DB" >/dev/null

pushd "$SQL_REPO" >/dev/null
capture "sqlsc init (live)" "$SQLSC" --json init \
  --kind sql_server \
  --server "$SQLSC_TEST_SERVER" \
  --database "$SRC_DB" \
  --auth sql \
  --user sa \
  --trust-server-certificate \
  --layout schema \
  --folder database
popd >/dev/null
need_exit 0 sql-init
assert_json sql-init 'doc["ok"] is True' 'doc["layout"]=="schema"'
if grep -Ei 'password|PWD=' "$SQL_REPO/sqlsc.yml" >/dev/null; then
  die sql-init "sqlsc.yml contains a password"
fi

CUSTOMER="$SQL_REPO/database/sales/Tables/Customer.sql"
SCHEMA_FILE="$SQL_REPO/database/Security/sales.sql"

capture "status --exit-code (before script)" "$SQLSC" --json --config "$SQL_REPO/sqlsc.yml" status --exit-code
need_exit 1 sql-status
assert_json sql-status \
  'doc["ok"] is True' \
  'doc["command"]=="status"' \
  'any(c["schema"]=="sales" and c["name"]=="Customer" and c["action"]=="new" for c in doc["changes"])' \
  'doc["summary"]["new"] >= 1'

capture "diff sales.Customer" "$SQLSC" --json --config "$SQL_REPO/sqlsc.yml" diff sales.Customer
need_exit 0 sql-diff
assert_json sql-diff \
  'doc["ok"] is True' \
  'doc["command"]=="diff"' \
  'len(doc["changes"])==1' \
  'doc["changes"][0]["name"]=="Customer"' \
  '"CustomerId" in doc["changes"][0]["diff"]'

capture "script --dry-run" "$SQLSC" --json --config "$SQL_REPO/sqlsc.yml" script --dry-run
need_exit 0 sql-script-dry-run
assert_json sql-script-dry-run 'doc["ok"] is True' 'doc["dry_run"] is True' 'len(doc["files"])>=1'
if [[ -e "$CUSTOMER" ]]; then
  die sql-script-dry-run "dry-run wrote ${CUSTOMER}"
fi
# Nothing matches yet, so the sync record stays unwritten. A dry-run that created it
# would be recording a sync that did not happen.
if [[ -e "$SQL_REPO/.sqlsc/sync/dev.json" ]]; then
  die sql-script-dry-run "dry-run wrote .sqlsc/sync/dev.json before anything matched"
fi

capture "script" "$SQLSC" --json --config "$SQL_REPO/sqlsc.yml" script
need_exit 0 sql-script
assert_json sql-script 'doc["ok"] is True' 'doc["dry_run"] is False' 'len(doc["files"])>=1'
need_file "$CUSTOMER" sql-script
need_file "$SCHEMA_FILE" sql-script
grep -q "CREATE TABLE" "$CUSTOMER" || die sql-script "Customer.sql has no CREATE TABLE"
grep -q "CustomerId" "$CUSTOMER" || die sql-script "Customer.sql has no CustomerId"
grep -q "PK_Customer" "$CUSTOMER" || die sql-script "Customer.sql has no PK_Customer"
grep -q "CREATE SCHEMA" "$SCHEMA_FILE" || die sql-script "sales.sql has no CREATE SCHEMA"
need_file "$SQL_REPO/.sqlsc/sync/dev.json" sql-script

capture "status --exit-code (scripted, not committed)" "$SQLSC" --json --config "$SQL_REPO/sqlsc.yml" status --exit-code
need_exit 1 sql-status
assert_json sql-status \
  'any(c["name"]=="Customer" and c["uncommitted"] is True for c in doc["changes"])'

capture "commit" "$SQLSC" --json --config "$SQL_REPO/sqlsc.yml" commit --no-pull -m "$COMMIT_MESSAGE"
need_exit 0 sql-commit
assert_json sql-commit \
  'doc["ok"] is True' \
  'doc["command"]=="commit"' \
  'isinstance(doc["commit"], str) and len(doc["commit"])>=7' \
  '"sqlsc.yml" in doc["setup_files"]'
[[ "$(git -C "$SQL_REPO" log -1 --format='%s')" == "$COMMIT_MESSAGE" ]] || die sql-commit "git subject was not ${COMMIT_MESSAGE}"
git -C "$SQL_REPO" ls-tree -r --name-only HEAD >"$WORK/commit-files.txt"
for rel in sqlsc.yml .gitattributes .gitignore database/.editorconfig database/sales/Tables/Customer.sql database/Security/sales.sql; do
  grep -qx "$rel" "$WORK/commit-files.txt" || die sql-commit "commit is missing ${rel}"
done

capture "status --exit-code (after commit)" "$SQLSC" --json --config "$SQL_REPO/sqlsc.yml" status --exit-code
need_exit 0 sql-status
assert_json sql-status 'doc["ok"] is True' 'doc["changes"]==[]'

"$PY" - "$SQL_REPO/sqlsc.yml" "$EMPTY_DB" <<'PY'
import sys
from pathlib import Path

import yaml

path, database = Path(sys.argv[1]), sys.argv[2]
data = yaml.safe_load(path.read_text(encoding="utf-8"))
dev = data["targets"]["dev"]
data["targets"]["empty"] = {
    "kind": dev["kind"],
    "server": dev["server"],
    "database": database,
    "auth": dev["auth"],
    "user": dev["user"],
    "trust_server_certificate": True,
}
path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
PY

before="$("$PY" "$WORK/sqladmin.py" has-customer "$EMPTY_DB")"
[[ "$before" == "no" ]] || die sql-get-latest "empty database already had sales.Customer"

capture "get-latest --output" "$SQLSC" --json --config "$SQL_REPO/sqlsc.yml" get-latest -t empty -o "$EVIDENCE/sql/get-latest.sql"
need_exit 0 sql-get-latest
assert_json sql-get-latest 'doc["ok"] is True' 'doc["command"]=="get-latest"' 'doc["output"].endswith("get-latest.sql")'
need_file "$EVIDENCE/sql/get-latest.sql" sql-get-latest
grep -q "CREATE TABLE" "$EVIDENCE/sql/get-latest.sql" || die sql-get-latest "--output script has no CREATE TABLE"
grep -q "Customer" "$EVIDENCE/sql/get-latest.sql" || die sql-get-latest "--output script has no Customer"
after_output="$("$PY" "$WORK/sqladmin.py" has-customer "$EMPTY_DB")"
[[ "$after_output" == "no" ]] || die sql-get-latest "--output applied the script; sales.Customer exists"

capture "get-latest" "$SQLSC" --json --config "$SQL_REPO/sqlsc.yml" get-latest -t empty
need_exit 0 sql-get-latest
assert_json sql-get-latest \
  'doc["ok"] is True' \
  'doc["applied"] is True' \
  'doc["failed"]==[]' \
  'doc["transaction"] is True' \
  'doc["still_different"]==[]'
applied="$("$PY" "$WORK/sqladmin.py" has-customer "$EMPTY_DB")"
[[ "$applied" == "yes" ]] || die sql-get-latest "get-latest did not create sales.Customer"

capture "status --exit-code (empty target)" "$SQLSC" --json --config "$SQL_REPO/sqlsc.yml" status -t empty --exit-code
need_exit 0 sql-status
assert_json sql-status 'doc["ok"] is True' 'doc["changes"]==[]'

SQL_RESULT=pass
echo "sql checks passed"
exit 0
