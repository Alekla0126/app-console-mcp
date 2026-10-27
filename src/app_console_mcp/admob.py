"""AdMob: accounts, apps and ad units, ad sources, earnings and campaign reports, and (with Google's
limited access) creating apps and ad units, ad unit mappings, mediation groups and mediation A/B tests.

Auth: AdMob does not accept service accounts, only a Google user. Either run `appconsole admob login`
once (OAuth desktop client of your own) or use gcloud application-default credentials with AdMob scopes.
Changing anything also needs the admob.monetization scope (`appconsole admob login --monetization`) and a
publisher that Google enabled for the limited-access part of the API.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from functools import lru_cache

import google.auth
from google.auth.exceptions import DefaultCredentialsError
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

from .core import CONFIG_DIR, DRY_RUN, ConsoleError, Toolset, call, clip, date_range, pct_change, setting

API = "https://admob.googleapis.com/v1"
API_BETA = "https://admob.googleapis.com/v1beta"
SCOPES = ["https://www.googleapis.com/auth/admob.readonly", "https://www.googleapis.com/auth/admob.report"]
SCOPE_MONETIZATION = "https://www.googleapis.com/auth/admob.monetization"
TOKEN_FILE = CONFIG_DIR / "admob-token.json"
ADMOB_NETWORK = "5450213213286189855"  # the AdMob Network ad source: its lines need no ad unit mapping

NETWORK_DIMS = {"app": "APP", "ad_unit": "AD_UNIT", "country": "COUNTRY", "format": "FORMAT", "platform": "PLATFORM",
                "date": "DATE", "week": "WEEK", "month": "MONTH", "ad_type": "AD_TYPE",
                "app_version": "APP_VERSION_NAME", "sdk_version": "GMA_SDK_VERSION", "os_version": "MOBILE_OS_VERSION",
                "serving_restriction": "SERVING_RESTRICTION"}
MEDIATION_ONLY = {"ad_source": "AD_SOURCE", "ad_source_instance": "AD_SOURCE_INSTANCE", "mediation_group": "MEDIATION_GROUP"}
DIMENSIONS = {**NETWORK_DIMS, **MEDIATION_ONLY}
NETWORK_METRICS = ["ESTIMATED_EARNINGS", "IMPRESSIONS", "AD_REQUESTS", "MATCH_RATE", "SHOW_RATE",
                   "IMPRESSION_RPM", "IMPRESSION_CTR", "CLICKS"]
MEDIATION_METRICS = ["ESTIMATED_EARNINGS", "IMPRESSIONS", "AD_REQUESTS", "MATCH_RATE", "OBSERVED_ECPM",
                     "IMPRESSION_CTR", "CLICKS"]
CAMPAIGN_DIMS = {"campaign": "CAMPAIGN_NAME", "ad": "AD_NAME", "placement": "PLACEMENT_NAME",
                 "platform": "PLACEMENT_PLATFORM", "country": "COUNTRY", "format": "FORMAT", "date": "DATE"}
CAMPAIGN_METRICS = ["IMPRESSIONS", "CLICKS", "INSTALLS", "ESTIMATED_COST"]
AD_FORMATS = ["APP_OPEN", "BANNER", "BANNER_INTERSTITIAL", "INTERSTITIAL", "NATIVE", "REWARDED", "REWARDED_INTERSTITIAL"]
AD_TYPES = ["RICH_MEDIA", "VIDEO"]

toolset = Toolset(
    "admob", "admob", "AdMob",
    "AdMob through the official API. Reads: accounts, apps and ad units, ad sources and adapters, earnings "
    "(admob_report) and campaign reports. Changes (create app or ad unit, ad unit mapping, mediation group, "
    "A/B experiment) are a dry run unless confirm=true, need Google's limited access for the publisher and the "
    "admob.monetization scope; only pass confirm=true after the user approved that exact change. Dates are in "
    "the account's reporting time zone.",
)
tool = toolset.tool

LOGIN_HINT = ("Run `appconsole admob login --client-secrets <oauth-client.json>` once, or "
              "`gcloud auth application-default login --scopes=https://www.googleapis.com/auth/admob.readonly,"
              "https://www.googleapis.com/auth/admob.report,https://www.googleapis.com/auth/cloud-platform`.")
MONETIZATION_HINT = ("Authorize the admob.monetization scope: `appconsole admob login --monetization --client-secrets "
                     "<oauth-client.json>`.")
LIMITED_HINT = ("Google keeps this part of the AdMob API (creating apps and ad units, ad unit mappings, mediation "
                "groups and A/B experiments) in limited access, and {account} is not enabled for it yet. Ask your AdMob "
                "account manager or AdMob support (Help > Contact us) to enable the AdMob API v1beta limited-access "
                "methods for this publisher id, then run `appconsole admob login --monetization`.")


@lru_cache(maxsize=1)
def _session() -> AuthorizedSession:
    token_file = setting("ADMOB_TOKEN_FILE") or str(TOKEN_FILE)
    granted = None
    try:
        creds = Credentials.from_authorized_user_file(token_file)  # the scopes it was granted, monetization included
        granted = set(creds.scopes or [])
    except FileNotFoundError:
        try:
            creds, _ = google.auth.default(scopes=SCOPES)
        except DefaultCredentialsError as err:
            raise ConsoleError(f"No AdMob credentials. {LOGIN_HINT}") from err
    session = AuthorizedSession(creds)
    session.granted_scopes = granted  # None for gcloud's credentials: Google tells us when a scope is missing
    return session


def login(client_secrets: str, no_browser: bool = False, monetization: bool = False) -> str:
    """OAuth consent in the browser once; the refresh token is stored in ~/.config/appconsole."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    scopes = SCOPES + ([SCOPE_MONETIZATION] if monetization else [])
    flow = InstalledAppFlow.from_client_secrets_file(client_secrets, scopes)
    creds = flow.run_local_server(port=0, open_browser=not no_browser)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json())
    TOKEN_FILE.chmod(0o600)
    _session.cache_clear()
    _account.cache_clear()
    _currency.cache_clear()
    return f"Saved AdMob credentials to {TOKEN_FILE}" + (" (with admob.monetization)." if monetization else ".")


def _get(path: str, **params):
    return call(_session(), "GET", f"{API}/{path}", params=params or None)


@lru_cache(maxsize=1)
def _account() -> str:
    pub = setting("ADMOB_PUBLISHER_ID")
    if pub:
        return pub if pub.startswith("accounts/") else f"accounts/{pub}"
    accounts = _get("accounts").get("account", [])
    if not accounts:
        raise ConsoleError("This Google user has no AdMob account.")
    if len(accounts) > 1:
        raise ConsoleError("Several AdMob accounts: set ADMOB_PUBLISHER_ID to one of "
                           + ", ".join(a["publisherId"] for a in accounts))
    return accounts[0]["name"]


@lru_cache(maxsize=1)
def _currency() -> str:
    return _get(_account()).get("currencyCode", "")


def _list(resource: str, api: str = API, key: str = "", path: str = "") -> list[dict]:
    items, token = [], None
    while True:
        r = call(_session(), "GET", f"{api}/{_account()}/{path or resource}",
                 params={"pageSize": 1000, **({"pageToken": token} if token else {})})
        items += r.get(key or resource, [])
        token = r.get("nextPageToken")
        if not token:
            return items


def _limited(fn, *args, **kwargs):
    """Run a limited-access call, turning Google's 403s into what to do about them."""
    try:
        return fn(*args, **kwargs)
    except ConsoleError as err:
        msg = str(err)
        if "HTTP 403" in msg and "insufficient authentication scopes" in msg.lower():
            raise ConsoleError(f"The AdMob token lacks the admob.monetization scope. {MONETIZATION_HINT}") from err
        if "HTTP 403" in msg:
            raise ConsoleError(LIMITED_HINT.format(account=_account().split("/")[-1])) from err
        raise


def _beta(method: str, path: str, **kw):
    return _limited(call, _session(), method, f"{API_BETA}/{_account()}/{path}", **kw)


def _require_monetization() -> None:
    """A saved token without the scope can't write: say so before calling (gcloud tokens are checked by Google)."""
    granted = getattr(_session(), "granted_scopes", None)
    if granted is not None and SCOPE_MONETIZATION not in granted:
        raise ConsoleError(f"The saved AdMob token was granted without admob.monetization. {MONETIZATION_HINT}")


def _app_name(a: dict) -> str:
    return (a.get("linkedAppInfo", {}).get("displayName") or a.get("manualAppInfo", {}).get("displayName")
            or a["appId"])


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _fragment(ad_unit_id: str) -> str:
    """ca-app-pub-123/456 → 456 (the part the API's resource names use)."""
    return ad_unit_id.strip().split("/")[-1]


def _plan(body: dict) -> str:
    return json.dumps(body, ensure_ascii=False)


def _sent(kind: str, body: dict) -> str:
    return (f"{DRY_RUN}. AdMob has no server-side dry run: Google checks this only when it is sent.\n"
            f"Would {kind}:\n{_plan(body)}")


# ---------------------------------------------------------------- accounts, apps, ad sources

@tool()
def admob_accounts() -> str:
    """AdMob publisher accounts this Google user can read, with currency and reporting time zone."""
    accounts = _get("accounts").get("account", [])
    return "\n".join(f"{a['publisherId']}  currency={a.get('currencyCode')}  tz={a.get('reportingTimeZone')}"
                     for a in accounts) or "No AdMob accounts."


@tool()
def admob_apps(search: str = "", ad_units: bool = True) -> str:
    """AdMob apps (platform, store id, approval state) and their ad units (id, format, name).

    Useful to check that the ad unit ids in your code exist before a release.

    Args:
        search: part of an app name, app id or store id
        ad_units: also list each app's ad units
    """
    f = search.lower()
    apps = [a for a in _list("apps") if not f or f in _app_name(a).lower() or f in a["appId"].lower()
            or f in a.get("linkedAppInfo", {}).get("appStoreId", "").lower()]
    units = _list("adUnits") if ad_units and apps else []
    out = []
    for a in apps:
        store = a.get("linkedAppInfo", {}).get("appStoreId", "not linked to a store")
        out.append(f"{_app_name(a)} [{a.get('platform')}] {a['appId']} store={store} {a.get('appApprovalState', '')}")
        out += [f"   · {u['adUnitId']} {u.get('adFormat')} «{u.get('displayName')}»"
                for u in units if u["appId"] == a["appId"]]
    return "\n".join(out) or "No app matches."


def _adapters(ad_source_id: str) -> list[dict]:
    return _list("adapters", API_BETA, path=f"adSources/{ad_source_id}/adapters")


@tool()
def admob_ad_sources(search: str = "", adapters: bool = False) -> str:
    """Ad sources (networks) available for mediation, and optionally their adapters with the settings an ad unit
    mapping needs (adapter id, platform, formats, required keys).

    Args:
        search: part of a source name or its id, e.g. meta, applovin
        adapters: also list each source's adapters (needs search when more than 10 sources match)
    """
    f = search.lower()
    sources = [s for s in _list("adSources", API_BETA) if not f or f in s["title"].lower() or f in s["adSourceId"]]
    if adapters and len(sources) > 10:
        raise ConsoleError(f"{len(sources)} sources match: narrow them with search to list adapters.")
    out = []
    for s in sources:
        out.append(f"{s['adSourceId']}  {s['title']}")
        if adapters:
            for a in _adapters(s["adSourceId"]):
                meta = a.get("adapterConfigMetadata", [])
                keys = ", ".join(f"{m['adapterConfigMetadataId']}={m['adapterConfigMetadataLabel']}"
                                 + ("" if m.get("isRequired") else " (optional)") for m in meta)
                out.append(f"   · adapter {a['adapterId']} {a.get('platform')} {','.join(a.get('formats', []))}"
                           + (f" · settings: {keys}" if keys else ""))
    return "\n".join(out) or "No ad source matches."


# ---------------------------------------------------------------- reports

def _ymd(d: date) -> dict:
    return {"year": d.year, "month": d.month, "day": d.day}


def run_report(start: date, end: date, dims: list[str], mediation: bool, currency: str, rows: int,
               app_filter: str = "") -> tuple[list[dict], str]:
    spec = {"dateRange": {"startDate": _ymd(start), "endDate": _ymd(end)}, "dimensions": dims,
            "metrics": MEDIATION_METRICS if mediation else NETWORK_METRICS,
            "maxReportRows": rows}
    time_dims = [d for d in dims if d in ("DATE", "WEEK", "MONTH")]
    spec["sortConditions"] = ([{"dimension": d, "order": "ASCENDING"} for d in time_dims] if time_dims
                              else [{"metric": "ESTIMATED_EARNINGS", "order": "DESCENDING"}])
    if currency:
        spec["localizationSettings"] = {"currencyCode": currency.upper()}
    if app_filter:
        spec["dimensionFilters"] = [{"dimension": "APP", "matchesAny": {"values": [app_filter]}}]
    kind = "mediationReport" if mediation else "networkReport"
    r = call(_session(), "POST", f"{API}/{_account()}/{kind}:generate", json={"reportSpec": spec}, timeout=120)
    chunks = r if isinstance(r, list) else [r]
    header = next((c["header"] for c in chunks if "header" in c), {})
    return [c["row"] for c in chunks if "row" in c], header.get("localizationSettings", {}).get("currencyCode", "")


def metric(row: dict, name: str) -> float:
    m = row.get("metricValues", {}).get(name, {})
    if "microsValue" in m:
        return int(m["microsValue"]) / 1e6
    if "integerValue" in m:
        return float(m["integerValue"])
    return float(m.get("doubleValue", 0) or 0)


def _label(row: dict, dims: list[str]) -> str:
    vals = row.get("dimensionValues", {})
    parts = []
    for d in dims:
        v = vals.get(d, {})
        text = v.get("displayLabel") or v.get("value", "?")
        if d == "DATE" and len(text) == 8 and text.isdigit():
            text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
        parts.append(text)
    return " / ".join(parts)


@tool()
def admob_report(days: int = 7, by: str = "app", currency: str = "", rows: int = 20, app_id: str = "",
                 end_date: str = "") -> str:
    """AdMob earnings, impressions, eCPM, match, show rate and CTR, with the change against the previous period.

    Args:
        days: number of full days ending yesterday (or end_date)
        by: grouping, comma-separated: app, ad_unit, country, format, platform, date, week, month, ad_type,
            app_version, sdk_version, os_version, serving_restriction (why ads are limited), or the mediation
            groupings ad_source, ad_source_instance, mediation_group
        currency: report currency, e.g. USD (empty uses the account currency)
        rows: maximum rows to show
        app_id: optional AdMob app id (ca-app-pub-...~...) to limit the report to one app
        end_date: optional last day, YYYY-MM-DD
    """
    keys = [k.strip() for k in by.split(",") if k.strip()]
    bad = [k for k in keys if k not in DIMENSIONS]
    if bad:
        raise ConsoleError(f"Unknown grouping {bad}. Use: {', '.join(DIMENSIONS)}")
    dims = [DIMENSIONS[k] for k in keys]
    mediation = any(d in MEDIATION_ONLY.values() for d in dims)
    if mediation and "AD_TYPE" in dims:
        raise ConsoleError("ad_type isn't in the mediation report: drop it or the mediation groupings.")
    if "APP" in dims and "PLATFORM" not in dims:
        dims.append("PLATFORM")  # an Android and an iOS app often share a name
    start, end = date_range(days, date.fromisoformat(end_date) if end_date else None)
    data, cur = run_report(start, end, dims, mediation, currency, rows, app_id)
    now_total, _ = run_report(start, end, [], mediation, currency, 1, app_id)
    prev_start, prev_end = date_range(days, start - timedelta(days=1))
    prev, _ = run_report(prev_start, prev_end, [], mediation, currency, 1, app_id)
    total = {m: metric(now_total[0], m) if now_total else 0 for m in ("ESTIMATED_EARNINGS", "IMPRESSIONS")}
    before = metric(prev[0], "ESTIMATED_EARNINGS") if prev else 0
    ecpm = "OBSERVED_ECPM" if mediation else "IMPRESSION_RPM"
    out = [f"AdMob {start} to {end} by {', '.join(keys)}: {total['ESTIMATED_EARNINGS']:.2f} {cur} "
           f"({pct_change(total['ESTIMATED_EARNINGS'], before)} vs previous {days} d: {before:.2f}), "
           f"{total['IMPRESSIONS']:.0f} impressions" + (" (top rows only)" if len(data) >= rows else "")]
    for r in data:
        show = "" if mediation else f", show {metric(r, 'SHOW_RATE') * 100:.0f}%"
        out.append(f"  {clip(_label(r, dims), 50)}: {metric(r, 'ESTIMATED_EARNINGS'):.2f} {cur}, "
                   f"{metric(r, 'IMPRESSIONS'):.0f} impr, eCPM {metric(r, ecpm):.2f}, "
                   f"match {metric(r, 'MATCH_RATE') * 100:.0f}%{show}, CTR {metric(r, 'IMPRESSION_CTR') * 100:.1f}%")
    return "\n".join(out)


@tool()
def admob_campaign_report(days: int = 7, by: str = "campaign", rows: int = 20, end_date: str = "") -> str:
    """AdMob app promotion campaigns: impressions, clicks, CTR, installs, estimated cost and cost per install.

    Args:
        days: number of full days ending yesterday (or end_date), at most 30
        by: grouping, comma-separated: campaign, ad, placement, platform, country, format, date
        rows: maximum rows to show
        end_date: optional last day, YYYY-MM-DD
    """
    if days > 30:
        raise ConsoleError("The campaign report covers at most 30 days.")
    keys = _split(by)
    bad = [k for k in keys if k not in CAMPAIGN_DIMS]
    if bad:
        raise ConsoleError(f"Unknown grouping {bad}. Use: {', '.join(CAMPAIGN_DIMS)}")
    dims = [CAMPAIGN_DIMS[k] for k in keys]
    start, end = date_range(days, date.fromisoformat(end_date) if end_date else None)
    r = call(_session(), "POST", f"{API_BETA}/{_account()}/campaignReport:generate", timeout=120, json={"reportSpec": {
        "dateRange": {"startDate": _ymd(start), "endDate": _ymd(end)}, "dimensions": dims, "metrics": CAMPAIGN_METRICS}})
    data = r.get("rows", []) if isinstance(r, dict) else [c.get("row", c) for c in r]
    cur = _currency()
    tot = {m: sum(metric(x, m) for x in data) for m in CAMPAIGN_METRICS}
    cpi = f"{tot['ESTIMATED_COST'] / tot['INSTALLS']:.2f}" if tot["INSTALLS"] else "n/a"
    out = [f"AdMob campaigns {start} to {end} by {', '.join(keys)}: {tot['IMPRESSIONS']:.0f} impr, "
           f"{tot['CLICKS']:.0f} clicks, {tot['INSTALLS']:.0f} installs, cost {tot['ESTIMATED_COST']:.2f} {cur}, CPI {cpi}"]
    order = (lambda x: _label(x, dims)) if "DATE" in dims else (lambda x: -metric(x, "ESTIMATED_COST"))
    for x in sorted(data, key=order)[:rows]:
        impr, clicks, inst, cost = (metric(x, m) for m in ("IMPRESSIONS", "CLICKS", "INSTALLS", "ESTIMATED_COST"))
        out.append(f"  {clip(_label(x, dims), 50)}: {impr:.0f} impr, {clicks:.0f} clicks"
                   f" (CTR {clicks / impr * 100 if impr else 0:.1f}%), {inst:.0f} installs, cost {cost:.2f} {cur}"
                   + (f", CPI {cost / inst:.2f}" if inst else ""))
    return "\n".join(out if data else out[:1] + ["  no campaign activity"])


# ---------------------------------------------------------------- mediation (limited access)

def _groups() -> list[dict]:
    return _limited(_list, "mediationGroups", API_BETA)


def _group(group_id: str) -> dict:
    found = [g for g in _groups() if g.get("mediationGroupId") == group_id.strip()]
    if not found:
        raise ConsoleError(f"No mediation group {group_id}. List them with admob_mediation_groups.")
    return found[0]


def _source_names() -> dict[str, str]:
    return {s["adSourceId"]: s["title"] for s in _list("adSources", API_BETA)}


def _fmt_line(line_id: str, ln: dict, names: dict[str, str]) -> str:
    cpm = "live" if ln.get("cpmMode") == "LIVE" else f"{int(ln.get('cpmMicros') or 0) / 1e6:g}"
    return (f"   · line {line_id} «{ln.get('displayName', '')}» {names.get(ln.get('adSourceId'), ln.get('adSourceId'))} "
            f"cpm={cpm} {ln.get('state')}" + (f" variant={ln['experimentVariant']}" if ln.get("experimentVariant") else ""))


@tool()
def admob_mediation_groups(search: str = "") -> str:
    """Mediation groups with their targeting (platform, format, ad units, regions), state, A/B test and lines
    (ad source, CPM, state). Limited access: Google must enable the publisher.

    Args:
        search: part of a group name or its id
    """
    f = search.lower()
    groups = [g for g in _groups() if not f or f in g.get("displayName", "").lower() or f in g.get("mediationGroupId", "")]
    names = _source_names() if groups else {}
    out = []
    for g in groups:
        t = g.get("targeting", {})
        regions = ",".join(t.get("targetedRegionCodes", [])) or "all regions"
        out.append(f"{g['mediationGroupId']} «{g.get('displayName')}» {g.get('state')} {t.get('platform')} "
                   f"{t.get('format')} {len(t.get('adUnitIds', []))} ad units, {regions}, "
                   f"A/B {g.get('mediationAbExperimentState', '-')}")
        out += [_fmt_line(i, ln, names) for i, ln in g.get("mediationGroupLines", {}).items()]
    return "\n".join(out) or "No mediation group matches."


def _mappings(ad_unit_id: str) -> list[dict]:
    return _limited(_list, "adUnitMappings", API_BETA, path=f"adUnits/{_fragment(ad_unit_id)}/adUnitMappings")


@tool()
def admob_ad_unit_mappings(ad_unit_id: str) -> str:
    """Ad unit mappings of one ad unit: which third-party adapter it uses and its settings. Limited access.

    Args:
        ad_unit_id: ca-app-pub-.../... ad unit id
    """
    maps = _mappings(ad_unit_id)
    return "\n".join(f"{m['name'].split('/')[-1]} adapter {m.get('adapterId')} «{m.get('displayName', '')}» "
                     f"{m.get('state', '')} · "
                     + ", ".join(f"{k}={clip(v, 40)}" for k, v in m.get("adUnitConfigurations", {}).items())
                     for m in maps) or f"{ad_unit_id} has no ad unit mappings."


def _parse_lines(specs: str, ad_unit_ids: list[str], start_id: int = -1) -> dict[str, dict]:
    """'SOURCE@live,SOURCE@1.50' → mediation group lines keyed by placeholder ids (-1, -2…), with the ad unit
    mappings each third-party source needs, found among the ad units' existing mappings."""
    lines: dict[str, dict] = {}
    names = _source_names()
    for n, spec in enumerate(_split(specs)):
        source, _, cpm = spec.partition("@")
        source, cpm = source.strip(), (cpm.strip() or "live").lower()
        if source not in names:
            raise ConsoleError(f"Unknown ad source {source}. Find ids with admob_ad_sources.")
        line_id = str(start_id - n)
        line = {"id": line_id, "displayName": names[source], "adSourceId": source, "state": "ENABLED"}
        if cpm == "live":
            line["cpmMode"] = "LIVE"
        else:
            try:
                value = float(cpm)
            except ValueError as err:
                raise ConsoleError(f"CPM in {spec!r} must be 'live' or a number.") from err
            if value < 0.01:
                raise ConsoleError(f"CPM in {spec!r} must be at least 0.01.")
            line.update(cpmMode="MANUAL", cpmMicros=str(round(value * 1e6)))
        if source != ADMOB_NETWORK:
            adapter_ids = {a["adapterId"] for a in _adapters(source)}
            found = {}
            for unit in ad_unit_ids:
                m = next((m for m in _mappings(unit) if m.get("adapterId") in adapter_ids), None)
                if not m:
                    raise ConsoleError(f"Ad unit {unit} has no mapping for {names[source]}: "
                                       "create it with admob_map_ad_unit.")
                found[unit] = m["name"]
            line["adUnitMappings"] = found
        lines[line_id] = line
    return lines


# ---------------------------------------------------------------- changes (limited access)

@tool(write=True)
def admob_create_app(platform: str, display_name: str = "", app_store_id: str = "", confirm: bool = False) -> str:
    """Add an app to AdMob, linked to its store listing (app_store_id) or by name only. Limited access.

    Args:
        platform: ANDROID or IOS
        display_name: name for an app that isn't in a store yet (max 80)
        app_store_id: package name (Android) or numeric App Store id (iOS) to link it to its store
        confirm: true to create it
    """
    platform = platform.upper()
    if platform not in ("ANDROID", "IOS"):
        raise ConsoleError("platform must be ANDROID or IOS.")
    if not app_store_id and not display_name:
        raise ConsoleError("Give app_store_id (linked app) or display_name (not in a store yet).")
    if len(display_name) > 80:
        raise ConsoleError(f"display_name has {len(display_name)} characters (max 80).")
    if app_store_id:
        dupes = [a for a in _list("apps") if a.get("linkedAppInfo", {}).get("appStoreId") == app_store_id
                 and a.get("platform") == platform]
        if dupes:
            raise ConsoleError(f"{app_store_id} is already in AdMob as {dupes[0]['appId']}.")
    body = {"platform": platform, **({"linkedAppInfo": {"appStoreId": app_store_id}} if app_store_id else {}),
            **({"manualAppInfo": {"displayName": display_name}} if display_name else {})}
    if not confirm:
        return _sent("create this app", body)
    _require_monetization()
    a = _beta("POST", "apps", json=body)
    return (f"Created {a.get('appId')} [{a.get('platform')}] {a.get('appApprovalState', '')}: "
            "add the app id to the app's code before ads serve.")


@tool(write=True)
def admob_create_ad_unit(app_id: str, display_name: str, ad_format: str, ad_types: str = "",
                         reward_amount: int = 0, reward_item: str = "", confirm: bool = False) -> str:
    """Create an ad unit in one of the account's apps. Limited access.

    Args:
        app_id: AdMob app id, ca-app-pub-...~...
        display_name: ad unit name (max 80)
        ad_format: APP_OPEN, BANNER, BANNER_INTERSTITIAL, INTERSTITIAL, NATIVE, REWARDED or REWARDED_INTERSTITIAL
        ad_types: comma-separated RICH_MEDIA, VIDEO (empty: RICH_MEDIA for banners, both otherwise)
        reward_amount: reward per view, REWARDED only
        reward_item: reward name, e.g. coins, REWARDED only
        confirm: true to create it
    """
    fmt = ad_format.upper()
    if fmt not in AD_FORMATS:
        raise ConsoleError(f"ad_format must be one of {', '.join(AD_FORMATS)}.")
    types = [t.upper() for t in _split(ad_types)] or (["RICH_MEDIA"] if fmt == "BANNER" else AD_TYPES)
    if any(t not in AD_TYPES for t in types):
        raise ConsoleError("ad_types must be RICH_MEDIA and/or VIDEO.")
    if len(display_name) > 80:
        raise ConsoleError(f"display_name has {len(display_name)} characters (max 80).")
    if (reward_amount or reward_item) and fmt != "REWARDED":
        raise ConsoleError("Reward settings only apply to REWARDED ad units.")
    if not any(a["appId"] == app_id for a in _list("apps")):
        raise ConsoleError(f"No app {app_id} in this account. List them with admob_apps.")
    body = {"appId": app_id, "displayName": display_name, "adFormat": fmt, "adTypes": types}
    if reward_amount or reward_item:
        body["rewardSettings"] = {"unitAmount": str(reward_amount or 1), "unitType": reward_item or "reward"}
    if not confirm:
        return _sent("create this ad unit", body)
    _require_monetization()
    u = _beta("POST", "adUnits", json=body)
    return f"Created ad unit {u.get('adUnitId')} {u.get('adFormat')} «{u.get('displayName')}»."


@tool(write=True)
def admob_map_ad_unit(ad_unit_id: str, adapter_id: str, settings: list[str], display_name: str = "",
                      confirm: bool = False) -> str:
    """Map an ad unit to a third-party network's adapter, so mediation lines can use it. Limited access.

    Find the adapter id and the settings it needs with admob_ad_sources(search=..., adapters=true).

    Args:
        ad_unit_id: ca-app-pub-.../... ad unit id
        adapter_id: adapter id from admob_ad_sources
        settings: the adapter's settings as id=value, e.g. 118=1234567890_987654321
        display_name: optional name for the mapping
        confirm: true to create it
    """
    configs = {}
    for s in settings:
        key, sep, value = s.partition("=")
        if not sep or not key.strip() or not value.strip():
            raise ConsoleError(f"Settings go as id=value; got {s!r}.")
        configs[key.strip()] = value.strip()
    body = {"adapterId": adapter_id, "adUnitConfigurations": configs,
            **({"displayName": display_name} if display_name else {})}
    if not confirm:
        return _sent(f"map {ad_unit_id}", body)
    _require_monetization()
    m = _beta("POST", f"adUnits/{_fragment(ad_unit_id)}/adUnitMappings", json=body)
    return f"Created mapping {m.get('name', '').split('/')[-1]} for {ad_unit_id} (adapter {adapter_id})."


@tool(write=True)
def admob_create_mediation_group(display_name: str, platform: str, ad_format: str, ad_unit_ids: list[str],
                                 lines: str = "", regions: str = "", enable: bool = False, confirm: bool = False) -> str:
    """Create a mediation group for some ad units, with lines for other ad sources. Created disabled unless
    enable=true. Limited access.

    Args:
        display_name: group name (max 120)
        platform: ANDROID or IOS
        ad_format: BANNER, INTERSTITIAL, REWARDED, NATIVE, APP_OPEN or REWARDED_INTERSTITIAL
        ad_unit_ids: ad units the group targets
        lines: comma-separated SOURCE_ID@live or SOURCE_ID@CPM (manual CPM, account currency, min 0.01);
            third-party sources use each ad unit's existing mapping (admob_map_ad_unit)
        regions: optional comma-separated country codes to target, e.g. US,MX (empty: all)
        enable: serve it right away
        confirm: true to create it
    """
    if len(display_name) > 120:
        raise ConsoleError(f"display_name has {len(display_name)} characters (max 120).")
    targeting = {"platform": platform.upper(), "format": ad_format.upper(), "adUnitIds": ad_unit_ids,
                 **({"targetedRegionCodes": [r.upper() for r in _split(regions)]} if regions else {})}
    body = {"displayName": display_name, "state": "ENABLED" if enable else "DISABLED", "targeting": targeting,
            "mediationGroupLines": _parse_lines(lines, ad_unit_ids) if lines else {}}
    if not confirm:
        return _sent("create this mediation group", body)
    _require_monetization()
    g = _beta("POST", "mediationGroups", json=body)
    return f"Created mediation group {g.get('mediationGroupId')} «{g.get('displayName')}» {g.get('state')}."


@tool(write=True)
def admob_update_mediation_group(group_id: str, state: str = "", display_name: str = "", ad_unit_ids: str = "",
                                 line_id: str = "", line_state: str = "", line_cpm: float = 0,
                                 confirm: bool = False) -> str:
    """Change a mediation group: turn it on or off, rename it, change its ad units, or change one line's state or
    manual CPM. Limited access.

    Args:
        group_id: mediation group id from admob_mediation_groups
        state: ENABLED or DISABLED
        display_name: new name (max 120)
        ad_unit_ids: comma-separated ad unit ids that replace the targeted ones
        line_id: id of the line to change
        line_state: ENABLED, DISABLED or REMOVED for line_id
        line_cpm: new manual CPM for line_id (switches it to manual)
        confirm: true to save
    """
    g = _group(group_id)
    patch: dict = {}
    changes: list[str] = []
    if state:
        if state.upper() not in ("ENABLED", "DISABLED"):
            raise ConsoleError("state must be ENABLED or DISABLED.")
        patch["state"] = state.upper()
        changes.append(f"state {g.get('state')} → {patch['state']}")
    if display_name:
        if len(display_name) > 120:
            raise ConsoleError(f"display_name has {len(display_name)} characters (max 120).")
        patch["displayName"] = display_name
        changes.append(f"name «{g.get('displayName')}» → «{display_name}»")
    if ad_unit_ids:
        units = _split(ad_unit_ids)
        patch["targeting"] = {"adUnitIds": units}
        changes.append(f"ad units {len(g.get('targeting', {}).get('adUnitIds', []))} → {len(units)}")
    if line_id:
        lines = g.get("mediationGroupLines", {})
        if line_id not in lines:
            raise ConsoleError(f"Group {group_id} has no line {line_id}. Lines: {', '.join(lines) or 'none'}")
        ln = dict(lines[line_id])
        if line_state:
            if line_state.upper() not in ("ENABLED", "DISABLED", "REMOVED"):
                raise ConsoleError("line_state must be ENABLED, DISABLED or REMOVED.")
            ln["state"] = line_state.upper()
        if line_cpm:
            if line_cpm < 0.01:
                raise ConsoleError("line_cpm must be at least 0.01.")
            ln.update(cpmMode="MANUAL", cpmMicros=str(round(line_cpm * 1e6)))
        names = _source_names()
        changes.append(f"line {line_id}, before and after:\n{_fmt_line(line_id, lines[line_id], names)}\n"
                       f"{_fmt_line(line_id, ln, names)}")
        patch["mediationGroupLines"] = {**lines, line_id: ln}  # the API takes the whole map back
    if not patch:
        return "Nothing to change."
    mask = {"state": "state", "displayName": "displayName", "targeting": "targeting.adUnitIds",
            "mediationGroupLines": "mediationGroupLines"}
    plan = f"Mediation group {group_id} «{g.get('displayName')}»:\n" + "\n".join(f"- {c}" for c in changes)
    if not confirm:
        return f"{DRY_RUN}. AdMob has no server-side dry run: Google checks this only when it is sent.\n{plan}"
    _require_monetization()
    _beta("PATCH", f"mediationGroups/{group_id}", params={"updateMask": ",".join(mask[k] for k in patch)}, json=patch)
    return f"Saved.\n{plan}"


@tool(write=True)
def admob_ab_experiment(group_id: str, action: str = "start", treatment_lines: str = "", traffic_percent: int = 50,
                        display_name: str = "", winner: str = "", confirm: bool = False) -> str:
    """Start a mediation A/B experiment on a group (its lines vs. treatment lines), or stop it keeping the winner.
    Limited access.

    Args:
        group_id: mediation group id
        action: start or stop
        treatment_lines: for start, comma-separated SOURCE_ID@live or SOURCE_ID@CPM for variant B
        traffic_percent: for start, share of traffic for variant B (1-99)
        display_name: for start, experiment name
        winner: for stop, a (keep the current lines) or b (keep the treatment)
        confirm: true to start or stop it
    """
    g = _group(group_id)
    if action == "start":
        if not treatment_lines:
            raise ConsoleError("Give treatment_lines for variant B.")
        if not 1 <= traffic_percent <= 99:
            raise ConsoleError("traffic_percent must be between 1 and 99.")
        units = g.get("targeting", {}).get("adUnitIds", [])
        treatment = [{"mediationGroupLine": ln} for ln in _parse_lines(treatment_lines, units).values()]
        body = {"displayName": display_name or f"A/B {g.get('displayName')}",
                "treatmentTrafficPercentage": str(traffic_percent), "treatmentMediationLines": treatment}
        if not confirm:
            return _sent(f"start this experiment on {group_id}", body)
        _require_monetization()
        e = _beta("POST", f"mediationGroups/{group_id}/mediationAbExperiments", json=body)
        return f"Started experiment {e.get('experimentId')} on {group_id}: {traffic_percent}% to variant B."
    if action == "stop":
        choice = {"a": "VARIANT_CHOICE_A", "b": "VARIANT_CHOICE_B"}.get(winner.lower())
        if not choice:
            raise ConsoleError("Give winner a (current lines) or b (treatment).")
        body = {"variantChoice": choice}
        if not confirm:
            return _sent(f"stop the experiment on {group_id} keeping variant {winner.upper()}", body)
        _require_monetization()
        e = _beta("POST", f"mediationGroups/{group_id}/mediationAbExperiments:stop", json=body)
        return f"Stopped experiment {e.get('experimentId', '')} on {group_id}; kept variant {winner.upper()}."
    raise ConsoleError("action must be start or stop.")
