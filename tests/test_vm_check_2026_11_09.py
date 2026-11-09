"""Regression tests for the 2026-11-09 dev VM workflow check: a first release where a view
calls a scalar function, checkouts across differently spelled remotes, a half-local git
identity, the first SSMS save's byte order mark and `.vs/`, and the status of a file the
database already matches but git has not got.

Offline: fake catalogs and recording connections stand in for the database; git is real.
"""

from __future__ import annotations

import json

import pytest

from coop_sqlsc.compare import DELETED, Change
from coop_sqlsc.config import build_target
from coop_sqlsc.deploy import apply, build_plan
from coop_sqlsc.layout import write_object
from coop_sqlsc.locks import normalize_remote
from coop_sqlsc.objects import ObjectKey

from .test_review_fixes import V1, V2, git, run
from .test_review_fixes import repo as review_repo  # noqa: F401  (fixture)

TARGET = build_target("dev", {"kind": "sql_server", "server": "fixture", "database": "fixture"})

VIEW = ObjectKey("view", "sales", "v_CustomerTotals")
FUNCTION = ObjectKey("function", "dbo", "fn_OrderTotal")
OTHER = ObjectKey("function", "dbo", "fn_Other")
VIEW_TEXT = (
    "CREATE VIEW sales.v_CustomerTotals AS\n"
    "SELECT c.CustomerId, [dbo].[fn_OrderTotal](c.CustomerId) AS Total -- not dbo.fn_Other\n"
    "FROM dbo.Customer AS c;\nGO\n"
)
FUNCTION_TEXT = "CREATE FUNCTION dbo.fn_OrderTotal(@id int) RETURNS int AS BEGIN RETURN @id END;\nGO\n"
OTHER_TEXT = "CREATE FUNCTION dbo.fn_Other() RETURNS int AS BEGIN RETURN 1 END;\nGO\n"


def _created(*pairs: tuple[ObjectKey, str]) -> list[Change]:
    return [Change(key, DELETED, None, text) for key, text in pairs]


# --- a view on a scalar function, into an empty database --------------------------------------


def test_a_view_is_created_after_the_function_it_calls():
    plan = build_plan(_created((VIEW, VIEW_TEXT), (FUNCTION, FUNCTION_TEXT), (OTHER, OTHER_TEXT)), allow_drops=False)
    # The function the view names comes first; a name only in a comment does not count,
    # and the rest keep the usual order.
    assert [s.key for s in plan.ordered()] == [FUNCTION, VIEW, OTHER]
    text = plan.script(TARGET)
    assert text.index("CREATE FUNCTION dbo.fn_OrderTotal") < text.index("CREATE VIEW sales.v_CustomerTotals")


@pytest.mark.parametrize(
    "spelled",
    [
        "dbo.fn_OrderTotal",
        "[dbo].[fn_OrderTotal]",
        "DBO.FN_ORDERTOTAL",
        '"dbo"."fn_OrderTotal"',
        "dbo . [fn_OrderTotal]",
    ],
)
def test_every_spelling_of_a_two_part_name_orders_it(spelled):
    view = VIEW_TEXT.replace("[dbo].[fn_OrderTotal]", spelled)
    plan = build_plan(_created((VIEW, view), (FUNCTION, FUNCTION_TEXT)), allow_drops=False)
    assert [s.key for s in plan.ordered()] == [FUNCTION, VIEW]


def test_names_in_strings_and_other_databases_do_not_order():
    view = VIEW_TEXT.replace("[dbo].[fn_OrderTotal](c.CustomerId)", "'dbo.fn_OrderTotal', otherdb.dbo.fn_OrderTotal(1)")
    plan = build_plan(_created((VIEW, view), (FUNCTION, FUNCTION_TEXT)), allow_drops=False)
    assert [s.key for s in plan.ordered()] == [VIEW, FUNCTION]


def test_objects_that_name_each_other_keep_the_usual_order():
    a, b = ObjectKey("view", "dbo", "a"), ObjectKey("view", "dbo", "b")
    plan = build_plan(
        _created(
            (a, "CREATE VIEW dbo.a AS SELECT * FROM dbo.b;\nGO\n"),
            (b, "CREATE VIEW dbo.b AS SELECT * FROM dbo.a;\nGO\n"),
        ),
        allow_drops=False,
    )
    assert [s.key for s in plan.ordered()] == [a, b]


class Session:
    """Fails a statement naming `missing` until something created it, like SQL Server."""

    def __init__(self, message: str):
        self.autocommit = True
        self.message = message
        self.created: list[str] = []

    def cursor(self):
        return self

    def execute(self, sql, *params):
        if "CREATE VIEW" in sql and "dbo.fn_OrderTotal" not in self.created:
            raise RuntimeError(self.message)
        if "CREATE FUNCTION dbo.fn_OrderTotal" in sql:
            self.created.append("dbo.fn_OrderTotal")
        return self

    def commit(self):
        pass

    def rollback(self):
        pass


@pytest.mark.parametrize(
    "message",
    [
        # 4121, what the VM check saw (SQL Server 2022).
        '[42000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Cannot find either column "dbo" or the '
        'user-defined function or aggregate "dbo.fn_OrderTotal", or the name is ambiguous. (4121) (SQLExecDirectW)',
        # 4512, a schema-bound view on a function not created yet.
        "[42000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Cannot schema bind view "
        "'sales.v_CustomerTotals' because name 'dbo.fn_OrderTotal' is invalid for schema binding. (4512)",
    ],
)
def test_a_view_on_a_function_the_order_missed_is_retried(message, monkeypatch):
    # A name the order cannot see (a one-part call through a synonym, say): the retry catches it.
    monkeypatch.setattr("coop_sqlsc.deploy._by_dependency", lambda steps: steps)
    plan = build_plan(_created((VIEW, VIEW_TEXT), (FUNCTION, FUNCTION_TEXT)), allow_drops=False)
    session = Session(message)
    result = apply(session, plan, TARGET)
    assert result.failed == []
    assert result.done == [FUNCTION, VIEW]


# --- checkouts: one name for every spelling of the remote ----------------------------------------


@pytest.mark.parametrize(
    "urls, expected",
    [
        (
            [
                r"C:\dev\sqlsc-test\remote.git",
                "C:/dev/sqlsc-test/remote.git",
                "c:/dev/sqlsc-test/remote/",
                "file:///C:/dev/sqlsc-test/remote.git",
            ],
            "c:/dev/sqlsc-test/remote",
        ),
        (
            [
                "https://Org@dev.azure.com/Org/Project/_git/Repo",
                "https://dev.azure.com/org/project/_git/repo",
                "git@ssh.dev.azure.com:v3/Org/Project/Repo",
                "ssh://git@ssh.dev.azure.com/v3/org/project/repo",
                "https://org.visualstudio.com/DefaultCollection/Project/_git/Repo",
                "https://org.visualstudio.com/Project/_git/Repo",
                "org@vs-ssh.visualstudio.com:v3/org/project/repo",
            ],
            "dev.azure.com/org/project/_git/repo",
        ),
        (
            [
                "git@github.com:Org/repo.git",
                "ssh://git@github.com:22/org/repo.git",
                "https://github.com/org/repo",
                "https://token@github.com/org/repo.git/",
            ],
            "github.com/org/repo",
        ),
        ([r"\\server\share\repo.git", "//server/share/repo"], "//server/share/repo"),
    ],
)
def test_every_spelling_of_a_remote_is_one_checkout_scope(urls, expected):
    assert {normalize_remote(url) for url in urls} == {expected}


def test_the_release_header_names_a_folder_remote_with_one_kind_of_slash():
    assert "\\" not in normalize_remote(r"C:\dev\sqlsc-test\remote.git")


# --- a git identity half set in the repository ---------------------------------------------------


def test_a_local_name_with_the_machines_email_is_called_out(review_repo, tmp_path_factory, monkeypatch):  # noqa: F811
    root, db, _ = review_repo
    machine = tmp_path_factory.mktemp("home") / "gitconfig"
    machine.write_text("[user]\n\temail = someone@personal.example\n\tname = Someone\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(machine))
    git(root, "config", "--unset", "user.email")
    db[ObjectKey("view", "dbo", "v")] = V1

    status = json.loads(run(root, "--json", "status").output)
    (warning,) = [w for w in status["warnings"] if "user.email" in w]
    assert "sets user.name but not user.email" in warning and "someone@personal.example" in warning

    committed = json.loads(run(root, "--json", "commit", "-m", "v1").output)
    assert committed["commit"]  # a warning, not a refusal
    assert any("sets user.name but not user.email" in w for w in committed["warnings"])

    # Both set here: nothing to say.
    git(root, "config", "user.email", "dev@example.com")
    status = json.loads(run(root, "--json", "status").output)
    assert not any("user.email" in w for w in status["warnings"])


# --- SSMS: the byte order mark on first save, and .vs/ --------------------------------------------


def test_init_tells_ssms_to_save_without_a_byte_order_mark(review_repo):  # noqa: F811
    root, _, _ = review_repo
    config = (root / "database" / ".editorconfig").read_text(encoding="utf-8")
    assert "[*.sql]\ncharset = utf-8\n" in config  # utf-8, not utf-8-bom
    assert ".editorconfig" not in (root / ".gitignore").read_text(encoding="utf-8")


def test_sqlproj_ignores_the_vs_folder_and_keeps_an_edited_editorconfig(review_repo):  # noqa: F811
    root, _, _ = review_repo
    (root / "database" / ".editorconfig").write_text("[*.sql]\ncharset = utf-8\nindent_size = 4\n", encoding="utf-8")
    assert run(root, "sqlproj").exit_code == 0
    assert ".vs/" in (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "indent_size = 4" in (root / "database" / ".editorconfig").read_text(encoding="utf-8")


# --- a file the database already matches, not committed yet --------------------------------------


def test_an_applied_file_edit_reads_as_not_committed(review_repo):  # noqa: F811
    root, db, _ = review_repo
    view = ObjectKey("view", "dbo", "v")
    db[view] = V1
    assert run(root, "--json", "commit", "-m", "v1").exit_code == 0
    write_object(root / "database", view, V2)  # edited in the file...
    db[view] = V2  # ...and applied with get-latest

    status = json.loads(run(root, "--json", "status").output)
    (change,) = status["changes"]
    assert change["origin"] == "database" and change["uncommitted"] is True
    assert status["sides"]["to_commit"] == 1

    text = " ".join(run(root, "status").output.split())
    assert "File, not committed" in text
    assert "the database already matches; the files are not committed yet" in text
    assert "the file is not committed to git yet" in " ".join(run(root, "diff", "dbo.v").output.split())
