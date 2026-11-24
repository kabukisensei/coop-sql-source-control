# CLI surface

Version and help tell a person which sqlsc they installed and which commands it has, before any database is involved.

## Sub-features

- `version` prints the checkout's version from both entry points.
- `help` lists the starting commands from `--help` and `-h`.
- `unknown` rejects a command that does not exist.

## How to get to it (user POV)

- Run `sqlsc --version` or `coop-sqlsc --version`.
- Run `sqlsc --help`, `sqlsc -h`, or `coop-sqlsc --help`.
- Type a command that is not in the help.

## Driving it with verify.sh

Preconditions:

- `./scripts/verify.sh` has installed this checkout into its virtualenv.
- `SQLSC` is that env's `sqlsc`, and `COOP` is its `coop-sqlsc`.
- The expected version is `__version__` in `src/coop_sqlsc/__init__.py`.

- **Version.** Ask which build this is. Run `sqlsc --version` and `coop-sqlsc --version`. Each exits 0 and prints exactly `sqlsc, version <version>`.
- **Help.** Ask what the tool does. Run `sqlsc --help` and `sqlsc -h`. Each exits 0 and the text contains `sqlsc init`, `sqlsc status`, `sqlsc commit`, and `sqlsc get-latest`.
- **Second entry point.** Run `coop-sqlsc --help`. Exit 0 and the text contains `Source control for SQL Server`.
- **Unknown command.** Run `sqlsc not-a-command`. Exit 2 and stderr contains `No such command`. Stdout is not a JSON document.

## Gotchas

- Both console scripts print the program name `sqlsc` in `--version`. A line that only contains the version number is a failure.
- Help is plain text, not `--json`. Do not parse it as JSON.
- An unknown command exits 2 because Click rejected it. That is not the JSON `"ok": false` envelope.
