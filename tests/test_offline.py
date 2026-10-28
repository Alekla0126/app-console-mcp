"""Offline tests: no network and no credentials needed."""
import asyncio
import inspect
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
                1 for p in inspect.signature(fn).parameters.values() if p.default is p.empty)
            assert parser.parse_args(args)._fn is fn


def test_cli_types_flags_and_lists():
    a = cli.build_parser().parse_args(["play", "upload", "com.x", "app.aab", "--rollout-percent", "10",
                                       "--draft", "--confirm"])
    assert (a.package, a.file, a.rollout_percent, a.draft, a.confirm, a.track) == (
        "com.x", "app.aab", 10.0, True, True, "internal")
    a = cli.build_parser().parse_args(["play", "search-rank", "com.x", "pdf scanner", "doc scan"])
    assert a.keywords == ["pdf scanner", "doc scan"]
    # a flag that defaults to true is turned off with --no-…
    assert cli.build_parser().parse_args(["admob", "apps"]).ad_units is True
    assert cli.build_parser().parse_args(["admob", "apps", "--no-ad-units"]).ad_units is False


def test_cli_prints_errors_without_traceback(capsys, monkeypatch):
    monkeypatch.delenv("GOOGLE_PLAY_SERVICE_ACCOUNT", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    play._session.cache_clear()
    assert cli.main(["play", "tracks", "com.x"]) == 1
    assert "GOOGLE_PLAY_SERVICE_ACCOUNT is not set" in capsys.readouterr().err


def test_parse_doc():
    summary, body, helps = cli.parse_doc(inspect.getdoc(play.play_upload))  # as the CLI reads it
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


def test_all_in_one_server_has_every_tool_once():
    names = [fn.__name__ for fn in cli.all_tools().tools]
    assert len(names) == len(set(names)) == sum(len(t.tools) for t in cli.TOOLSETS.values())
    assert cli.build_parser().parse_args(["mcp", "all"]).service == "all"


# ---------------------------------------------------------------- AdMob: the rest of the API

@pytest.fixture
def admob_offline(monkeypatch):
    """An AdMob account with two apps and two ad sources, without the network."""
    monkeypatch.setattr(admob, "_account", lambda: "accounts/pub-1")
    monkeypatch.setattr(admob, "_currency", lambda: "USD")
    monkeypatch.setattr(admob, "_source_names", lambda: {admob.ADMOB_NETWORK: "AdMob Network", "777": "Meta"})
    monkeypatch.setattr(admob, "_list", lambda resource, *a, **k: {
        "apps": [{"appId": "ca-app-pub-1~10", "platform": "ANDROID", "linkedAppInfo": {"appStoreId": "com.x"}}],
    }.get(resource, []))


def test_admob_limited_access_errors_say_what_to_do(admob_offline):
    def scope():
        raise core.ConsoleError("HTTP 403 on GET /x: Request had insufficient authentication scopes.")

    def denied():
        raise core.ConsoleError("HTTP 403 on GET /x: The caller does not have permission")
    with pytest.raises(core.ConsoleError, match="login --monetization"):
        admob._limited(scope)
    with pytest.raises(core.ConsoleError, match="limited access, and pub-1 is not enabled"):
        admob._limited(denied)


def test_admob_mediation_lines_find_their_mappings(admob_offline, monkeypatch):
    monkeypatch.setattr(admob, "_adapters", lambda source: [{"adapterId": "71"}])
    monkeypatch.setattr(admob, "_mappings",
                        lambda unit: [{"adapterId": "71", "name": f"accounts/pub-1/adUnits/{unit[-1]}/adUnitMappings/9"}])
    lines = admob._parse_lines(f"{admob.ADMOB_NETWORK}@live, 777@1.5", ["ca-app-pub-1/1"])
    assert lines["-1"] == {"id": "-1", "displayName": "AdMob Network", "adSourceId": admob.ADMOB_NETWORK,
                           "state": "ENABLED", "cpmMode": "LIVE"}
    assert lines["-2"]["cpmMode"] == "MANUAL" and lines["-2"]["cpmMicros"] == "1500000"
    assert lines["-2"]["adUnitMappings"] == {"ca-app-pub-1/1": "accounts/pub-1/adUnits/1/adUnitMappings/9"}
    monkeypatch.setattr(admob, "_mappings", lambda unit: [])
    with pytest.raises(core.ConsoleError, match="admob_map_ad_unit"):
        admob._parse_lines("777@live", ["ca-app-pub-1/1"])
    with pytest.raises(core.ConsoleError, match="at least 0.01"):
        admob._parse_lines(f"{admob.ADMOB_NETWORK}@0.001", [])
    with pytest.raises(core.ConsoleError, match="Unknown ad source"):
        admob._parse_lines("123@live", [])


def test_admob_writes_are_dry_runs_and_check_the_scope(admob_offline, monkeypatch):
    out = admob.admob_create_ad_unit("ca-app-pub-1~10", "Coins", "rewarded", reward_amount=5, reward_item="coins")
    assert out.startswith("DRY RUN") and '"rewardSettings": {"unitAmount": "5", "unitType": "coins"}' in out
    assert '"adTypes": ["RICH_MEDIA", "VIDEO"]' in out
    assert '"adTypes": ["RICH_MEDIA"]' in admob.admob_create_ad_unit("ca-app-pub-1~10", "Top", "BANNER")
    with pytest.raises(core.ConsoleError, match="already in AdMob"):
        admob.admob_create_app("android", app_store_id="com.x")
    with pytest.raises(core.ConsoleError, match="No app"):
        admob.admob_create_ad_unit("ca-app-pub-1~99", "x", "BANNER")
    # confirm with a saved token that lacks admob.monetization: stopped before calling Google
    session = type("S", (), {"granted_scopes": set(admob.SCOPES)})()
    monkeypatch.setattr(admob, "_session", lambda: session)
    monkeypatch.setattr(admob, "_beta", lambda *a, **k: pytest.fail("must not call Google"))
    with pytest.raises(core.ConsoleError, match="without admob.monetization"):
        admob.admob_create_app("IOS", display_name="New", confirm=True)


def test_admob_update_group_sends_only_what_changes(admob_offline, monkeypatch):
    group = {"mediationGroupId": "5", "displayName": "Banners", "state": "DISABLED",
             "mediationGroupLines": {"11": {"adSourceId": "777", "cpmMode": "LIVE", "state": "ENABLED"}}}
    monkeypatch.setattr(admob, "_group", lambda gid: group)
    out = admob.admob_update_mediation_group("5", state="enabled", line_id="11", line_cpm=2)
    assert out.startswith("DRY RUN") and "state DISABLED → ENABLED" in out and "cpm=2" in out
    sent = {}
    monkeypatch.setattr(admob, "_require_monetization", lambda: None)
    monkeypatch.setattr(admob, "_beta", lambda method, path, **kw: sent.update(method=method, path=path, **kw))
    admob.admob_update_mediation_group("5", state="ENABLED", confirm=True)
    assert sent == {"method": "PATCH", "path": "mediationGroups/5", "params": {"updateMask": "state"},
                    "json": {"state": "ENABLED"}}
    with pytest.raises(core.ConsoleError, match="no line 99"):
        admob.admob_update_mediation_group("5", line_id="99", line_state="DISABLED")


def test_admob_mediation_groupings_switch_reports(admob_offline, monkeypatch):
    seen = []
    monkeypatch.setattr(admob, "run_report", lambda s, e, dims, mediation, *a, **k: (seen.append(mediation), ([], "USD"))[1])
    admob.admob_report(by="mediation_group")
    admob.admob_report(by="app_version,serving_restriction")
    assert seen[0] is True and seen[3] is False
    with pytest.raises(core.ConsoleError, match="ad_type isn't in the mediation report"):
        admob.admob_report(by="ad_type,ad_source")


def test_admob_campaign_report(admob_offline, monkeypatch):
    def row(name, impr, clicks, installs, cost):
        return {"dimensionValues": {"CAMPAIGN_NAME": {"value": name}}, "metricValues": {
            "IMPRESSIONS": {"integerValue": str(impr)}, "CLICKS": {"integerValue": str(clicks)},
            "INSTALLS": {"integerValue": str(installs)}, "ESTIMATED_COST": {"microsValue": str(int(cost * 1e6))}}}
    monkeypatch.setattr(admob, "_session", lambda: None)
    rows = [row("Small", 100, 1, 0, 0.5), row("Big", 1000, 50, 10, 20)]
    monkeypatch.setattr(admob, "call", lambda *a, **k: {"rows": rows})
    out = admob.admob_campaign_report(days=7, end_date="2026-01-10")
    assert "1100 impr, 51 clicks, 10 installs, cost 20.50 USD, CPI 2.05" in out
    assert out.splitlines()[1].startswith("  Big: 1000 impr, 50 clicks (CTR 5.0%), 10 installs, cost 20.00 USD, CPI 2.00")
    with pytest.raises(core.ConsoleError, match="at most 30 days"):
        admob.admob_campaign_report(days=31)


# ---------------------------------------------------------------- Play: graphics, users, data safety

def test_play_image_uploads_are_checked_before_any_call(tmp_path, monkeypatch):
    monkeypatch.setattr(play, "_session", lambda *a: pytest.fail("no network"))
    png = tmp_path / "a.png"
    png.write_bytes(b"x")
    with pytest.raises(core.ConsoleError, match="image_type must be"):
        play.play_upload_images("com.x", "en-US", "screens", [str(png)])
    with pytest.raises(core.ConsoleError, match="Only PNG or JPEG"):
        play.play_upload_images("com.x", "en-US", "phoneScreenshots", [__file__])
    with pytest.raises(core.ConsoleError, match="exactly one"):
        play.play_upload_images("com.x", "en-US", "icon", [str(png), str(png)])


def test_play_permission_presets_and_names():
    assert play._permissions("deploy")[:3] == ["CAN_ACCESS_APP", "CAN_VIEW_NON_FINANCIAL_DATA", "CAN_VIEW_APP_QUALITY"]
    names = play._permissions("read, reply_to_reviews,CAN_MANAGE_ORDERS")
    assert names[-2:] == ["CAN_REPLY_TO_REVIEWS", "CAN_MANAGE_ORDERS"]
    with pytest.raises(core.ConsoleError, match="Unknown permission"):
        play._permissions("admin")


def test_play_grant_invites_or_updates(monkeypatch):
    users = [{"email": "ci@p.iam.gserviceaccount.com",
              "grants": [{"packageName": "com.x", "appLevelPermissions": ["CAN_ACCESS_APP"]}]}]
    monkeypatch.setattr(play, "_users", lambda: users)
    plan = play.play_grant("ci@p.iam.gserviceaccount.com", "com.x", "read")
    assert "access_app → access_app,view_non_financial_data" in plan
    assert play.play_grant("new@p.iam.gserviceaccount.com", "com.x").startswith("DRY RUN")
    sent = []
    monkeypatch.setattr(play, "_users_call", lambda method, path="", **kw: sent.append((method, path, kw)))
    play.play_grant("ci@p.iam.gserviceaccount.com", "com.x", "read", confirm=True)
    play.play_grant("ci@p.iam.gserviceaccount.com", "com.y", "read", confirm=True)
    play.play_grant("new@p.iam.gserviceaccount.com", "com.x", "read", confirm=True)
    play.play_revoke("ci@p.iam.gserviceaccount.com", "com.x", confirm=True)
    assert [(m, p) for m, p, _ in sent] == [("PATCH", "/ci@p.iam.gserviceaccount.com/grants/com.x"),
                                            ("POST", "/ci@p.iam.gserviceaccount.com/grants"), ("POST", ""),
                                            ("DELETE", "/ci@p.iam.gserviceaccount.com/grants/com.x")]
    assert sent[0][2]["params"] == {"updateMask": "appLevelPermissions"}
    assert sent[2][2]["json"]["grants"][0]["packageName"] == "com.x"
    with pytest.raises(core.ConsoleError, match="no grant on com.z"):
        play.play_revoke("ci@p.iam.gserviceaccount.com", "com.z")


def test_play_users_403_says_which_permission(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLAY_DEVELOPER_ID", "123")
    monkeypatch.setattr(play, "_session", lambda *a: None)

    def denied(*a, **k):
        raise core.ConsoleError("HTTP 403 on GET /x: You do not have permission to access this object")
    monkeypatch.setattr(play, "call", denied)
    with pytest.raises(core.ConsoleError, match="Manage users and permissions"):
        play.play_users()


def test_play_data_safety_csv(tmp_path, monkeypatch):
    good = tmp_path / "ds.csv"
    good.write_text("Question ID (machine readable),Response ID (machine readable),Response value,Answer requirement\n"
                    "PSL_DATA_COLLECTION,,true,REQUIRED\nPSL_ENCRYPTED,,,OPTIONAL\n")
    out = play.play_data_safety("com.x", str(good))
    assert out.startswith("DRY RUN") and "2 rows, 1 answered" in out
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n")
    with pytest.raises(core.ConsoleError, match="Data safety export"):
        play.play_data_safety("com.x", str(bad))
    sent = {}
    monkeypatch.setattr(play, "_api", lambda method, path, **kw: sent.update(method=method, path=path, **kw))
    play.play_data_safety("com.x", str(good), confirm=True)
    assert sent["path"] == "com.x/dataSafety" and sent["json"]["safetyLabels"].startswith("Question ID")


# ---------------------------------------------------------------- App Store: screenshots

def test_appstore_screenshot_upload(tmp_path, monkeypatch):
    shots = [tmp_path / f"{i}.png" for i in range(3)]
    for i, x in enumerate(shots):
        x.write_bytes(bytes([i]) * 10)
    monkeypatch.setattr(appstore, "_app", lambda a: {"id": "1", "attributes": {"name": "App"}})
    monkeypatch.setattr(appstore, "_localization", lambda *a: ({"attributes": {"versionString": "2.0"}}, {"id": "L"}))
    existing = {"id": "S", "attributes": {"screenshotDisplayType": "APP_IPHONE_67"},
                "_shots": [{"id": f"old{i}"} for i in range(8)]}
    monkeypatch.setattr(appstore, "_screenshot_sets", lambda loc: [existing])
    with pytest.raises(core.ConsoleError, match="at most 10"):
        appstore.appstore_upload_screenshots("App", "en-US", "app_iphone_67", [str(x) for x in shots])
    out = appstore.appstore_upload_screenshots("App", "en-US", "APP_IPHONE_67", [str(x) for x in shots], replace=True)
    assert out.startswith("DRY RUN") and "8 → 3 screenshots" in out
    calls, puts = [], []

    def asc(method, path, **kw):
        calls.append((method, path))
        if method == "POST":
            return {"data": {"id": "N", "attributes": {"uploadOperations": [
                {"method": "PUT", "url": "https://u", "offset": 0, "length": 10,
                 "requestHeaders": [{"name": "Content-Type", "value": "image/png"}]}]}}}
        return {}
    monkeypatch.setattr(appstore, "_asc", asc)
    ok = type("R", (), {"status_code": 200})()
    monkeypatch.setattr(appstore.requests, "request", lambda m, u, data, headers, timeout: puts.append(data) or ok)
    appstore.appstore_upload_screenshots("App", "en-US", "APP_IPHONE_67", [str(shots[0])], replace=True, confirm=True)
    assert calls.count(("DELETE", "/v1/appScreenshots/old0")) == 1 and ("POST", "/v1/appScreenshots") in calls
    assert ("PATCH", "/v1/appScreenshots/N") in calls and puts == [bytes([0]) * 10]
    with pytest.raises(core.ConsoleError, match="APP_IPHONE_67"):
        appstore.appstore_upload_screenshots("App", "en-US", "iphone", [str(shots[0])])
