"""Offline tests: no network and no credentials needed."""
import asyncio
from datetime import date

import pytest

from app_console_mcp import admob, appstore, cli, core, play, server


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(core, "CONFIG_DIR", tmp_path)


# ---------------------------------------------------------------- core

def test_check_limits():
    core.check_limits({"title": "x" * 30}, play.LISTING_LIMITS)
    with pytest.raises(core.ConsoleError, match="31 characters"):
        core.check_limits({"title": "x" * 31}, play.LISTING_LIMITS)


def test_diff_shows_the_changed_part_of_long_texts():
    before = {"promotionalText": "A" * 100 + " old ending"}
    after = {"promotionalText": "A" * 100 + " new ending"}
    line = core.diff_lines(before, after)
    assert "old ending" in line and "new ending" in line


def test_date_range_ends_yesterday_by_default():
    start, end = core.date_range(7, date(2026, 1, 10))
    assert (start, end) == (date(2026, 1, 4), date(2026, 1, 10))
    with pytest.raises(core.ConsoleError):
        core.date_range(0)


def test_setting_reads_env_then_config(monkeypatch):
    monkeypatch.delenv("ASC_KEY_ID", raising=False)
    assert core.setting("ASC_KEY_ID") == ""
    core.save_config({"ASC_KEY_ID": "FROMFILE"})
    assert core.setting("ASC_KEY_ID") == "FROMFILE"
    monkeypatch.setenv("ASC_KEY_ID", "FROMENV")
    assert core.setting("ASC_KEY_ID") == "FROMENV"
    monkeypatch.delenv("ASC_KEY_ID")
    core.save_config({})
    with pytest.raises(core.ConsoleError, match="appconsole config set ASC_KEY_ID"):
        core.setting("ASC_KEY_ID", "hint")


class FakeResponse:
    def __init__(self, payload, status=400):
        self.payload, self.status_code, self.text, self.reason = payload, status, str(payload), "Bad"

    def json(self):
        return self.payload


def test_api_error_google_and_apple():
    assert core.api_error(FakeResponse({"error": {"message": "Package not found"}})) == "Package not found"
    apple = {"errors": [{"title": "Invalid", "detail": "Name too long"}]}
    assert core.api_error(FakeResponse(apple)) == "Invalid: Name too long"


# ---------------------------------------------------------------- CLI

def test_every_tool_has_a_cli_command():
    parser = cli.build_parser()
    for key, ts in cli.TOOLSETS.items():
        for fn in ts.tools:
            name = fn.__name__.removeprefix(key + "_").replace("_", "-")
            args = [key, name] + ["x"] * sum(
                1 for p in __import__("inspect").signature(fn).parameters.values() if p.default is p.empty)
            assert parser.parse_args(args)._fn is fn


def test_cli_types_flags_and_lists():
    a = cli.build_parser().parse_args(["play", "upload", "com.x", "app.aab", "--rollout-percent", "10",
                                       "--draft", "--confirm"])
    assert (a.package, a.file, a.rollout_percent, a.draft, a.confirm, a.track) == (
        "com.x", "app.aab", 10.0, True, True, "internal")
    a = cli.build_parser().parse_args(["play", "search-rank", "com.x", "pdf scanner", "doc scan"])
    assert a.keywords == ["pdf scanner", "doc scan"]


def test_cli_prints_errors_without_traceback(capsys, monkeypatch):
    monkeypatch.delenv("GOOGLE_PLAY_SERVICE_ACCOUNT", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    play._session.cache_clear()
    assert cli.main(["play", "tracks", "com.x"]) == 1
    assert "GOOGLE_PLAY_SERVICE_ACCOUNT is not set" in capsys.readouterr().err


def test_parse_doc():
    summary, body, helps = cli.parse_doc(play.play_upload.__doc__.replace("\n    ", "\n"))
    assert summary.startswith("Upload an .aab")
    assert "Args:" not in body
    assert helps["track"].startswith("internal, alpha")


# ---------------------------------------------------------------- MCP server

@pytest.mark.parametrize("key", ["play", "appstore", "admob"])
def test_servers_build_and_mark_write_tools(key):
    ts = cli.TOOLSETS[key]
    srv = server.build(ts)
    tools = asyncio.run(srv.list_tools())
    tools = getattr(tools, "tools", tools)
    assert {t.name for t in tools} == {fn.__name__ for fn in ts.tools}
    for t in tools:  # mcp 2.x uses snake_case attribute names, 1.x camelCase
        writes = next(fn for fn in ts.tools if fn.__name__ == t.name).writes
        read_only = getattr(t.annotations, "read_only_hint", None)
        assert (read_only if read_only is not None else t.annotations.readOnlyHint) is (not writes)
        schema = getattr(t, "input_schema", None) or t.inputSchema
        if writes:
            assert "confirm" in schema["properties"]


# ---------------------------------------------------------------- Google Play

def test_play_status_and_notes():
    assert play._status(100, False) == {"status": "completed"}
    assert play._status(5, False) == {"status": "inProgress", "userFraction": 0.05}
    assert play._status(5, True) == {"status": "draft"}
    with pytest.raises(core.ConsoleError):
        play._status(0, False)
    with pytest.raises(core.ConsoleError, match="max 500"):
        play._release_notes("x" * 501, "en-US")


def test_play_dry_runs_do_not_call_google(tmp_path, monkeypatch):
    monkeypatch.setattr(play, "_session", lambda *a: pytest.fail("no network in a dry run"))
    aab = tmp_path / "app.aab"
    aab.write_bytes(b"0" * 1000)
    out = play.play_upload("com.x", str(aab), track="production", rollout_percent=20)
    assert out.startswith("DRY RUN") and "inProgress 20" in out
    assert play.play_reply("com.x", "r1", "Thanks!").startswith("DRY RUN")
    with pytest.raises(core.ConsoleError, match="350"):
        play.play_reply("com.x", "r1", "x" * 351)
    with pytest.raises(core.ConsoleError, match="exactly one"):
        play.play_rollout("com.x", rollout_percent=50, halt=True)


# ---------------------------------------------------------------- App Store Connect

SALES = "\t".join(["Provider", "SKU", "Title", "Product Type Identifier", "Units", "Developer Proceeds",
                   "Currency of Proceeds", "Parent Identifier"]) + "\n" + "\n".join("\t".join(r) for r in [
    ["APPLE", "app.sku", "My App", "1", "10", "0", "USD", ""],
    ["APPLE", "app.sku", "My App", "7", "3", "0", "USD", ""],
    ["APPLE", "pro.monthly", "Pro", "IAY", "2", "4.20", "USD", "app.sku"],
    ["APPLE", "pro.monthly", "Pro", "IAY", "1", "50", "MXN", "app.sku"],
])


def test_parse_sales_groups_iap_under_parent_app():
    acc = {}
    appstore.parse_sales(SALES, acc)
    app = acc["app.sku"]
    assert (app["title"], app["downloads"], app["updates"], app["iap"]) == ("My App", 10, 3, 3)
    assert app["proceeds"] == {"USD": pytest.approx(8.4), "MXN": 50}


def test_pick_version(monkeypatch):
    def v(s, state, created):
        return {"attributes": {"versionString": s, "appVersionState": state, "createdDate": created}}
    versions = [v("2.0", "PREPARE_FOR_SUBMISSION", "2026-02"), v("1.9", "READY_FOR_DISTRIBUTION", "2026-01")]
    monkeypatch.setattr(appstore, "_versions", lambda *a: versions)
    assert appstore._pick_version("1", "IOS")["attributes"]["versionString"] == "2.0"
    assert appstore._pick_version("1", "IOS", "live")["attributes"]["versionString"] == "1.9"
    monkeypatch.setattr(appstore, "_versions", lambda *a: versions[1:])
    with pytest.raises(core.ConsoleError, match="No editable version"):
        appstore._pick_version("1", "IOS", "editable")


def test_appstore_limits_checked_before_any_call(monkeypatch):
    monkeypatch.setattr(appstore, "_app", lambda *a: pytest.fail("limits must be checked first"))
    with pytest.raises(core.ConsoleError, match="keywords has 101"):
        appstore.appstore_update_listing("1", "en-US", keywords="k" * 101)


# ---------------------------------------------------------------- AdMob

def test_admob_report_formatting(monkeypatch):
    def fake_report(start, end, dims, mediation, currency, rows, app_filter=""):
        if not dims:
            total = 12.5 if start.day > 5 else 10
            return [{"metricValues": {"ESTIMATED_EARNINGS": {"microsValue": str(int(total * 1e6))},
                                      "IMPRESSIONS": {"integerValue": "1000"}}}], "USD"
        row = {"dimensionValues": {"APP": {"value": "ca-app-pub-1~2", "displayLabel": "My App"},
                                   "PLATFORM": {"value": "Android"}},
               "metricValues": {"ESTIMATED_EARNINGS": {"microsValue": "12500000"},
                                "IMPRESSIONS": {"integerValue": "1000"}, "IMPRESSION_RPM": {"doubleValue": 12.5},
                                "MATCH_RATE": {"doubleValue": 0.9}, "SHOW_RATE": {"doubleValue": 0.4}}}
        assert dims == ["APP", "PLATFORM"]
        return [row], "USD"

    monkeypatch.setattr(admob, "run_report", fake_report)
    out = admob.admob_report(days=3, end_date="2026-01-10")
    assert "12.50 USD (+25% vs previous 3 d: 10.00)" in out
    assert "My App / Android: 12.50 USD, 1000 impr, eCPM 12.50, match 90%, show 40%" in out
    with pytest.raises(core.ConsoleError, match="Unknown grouping"):
        admob.admob_report(by="nope")


def test_admob_label_formats_dates():
    row = {"dimensionValues": {"DATE": {"value": "20260105"}}}
    assert admob._label(row, ["DATE"]) == "2026-01-05"
