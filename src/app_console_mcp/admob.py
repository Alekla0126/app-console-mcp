"""AdMob: accounts, apps and ad units, and earnings reports (network and mediation).

Auth: AdMob does not accept service accounts, only a Google user. Either run `appconsole admob login`
once (OAuth desktop client of your own) or use gcloud application-default credentials with AdMob scopes.
"""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

import google.auth
from google.auth.exceptions import DefaultCredentialsError
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

from .core import CONFIG_DIR, ConsoleError, Toolset, call, clip, date_range, pct_change, setting

API = "https://admob.googleapis.com/v1"
SCOPES = ["https://www.googleapis.com/auth/admob.readonly", "https://www.googleapis.com/auth/admob.report"]
TOKEN_FILE = CONFIG_DIR / "admob-token.json"
DIMENSIONS = {"app": "APP", "ad_unit": "AD_UNIT", "country": "COUNTRY", "format": "FORMAT",
              "platform": "PLATFORM", "date": "DATE", "month": "MONTH", "ad_source": "AD_SOURCE"}
NETWORK_METRICS = ["ESTIMATED_EARNINGS", "IMPRESSIONS", "AD_REQUESTS", "MATCH_RATE", "SHOW_RATE",
                   "IMPRESSION_RPM", "CLICKS"]
MEDIATION_METRICS = ["ESTIMATED_EARNINGS", "IMPRESSIONS", "AD_REQUESTS", "MATCH_RATE", "OBSERVED_ECPM", "CLICKS"]

toolset = Toolset(
    "admob", "admob", "AdMob",
    "AdMob through the official API (read-only: the API does not let third parties create ad units). "
    "Use admob_report for earnings; dates are in the account's reporting time zone.",
)
tool = toolset.tool

LOGIN_HINT = ("Run `appconsole admob login --client-secrets <oauth-client.json>` once, or "
              "`gcloud auth application-default login --scopes=https://www.googleapis.com/auth/admob.readonly,"
              "https://www.googleapis.com/auth/admob.report,https://www.googleapis.com/auth/cloud-platform`.")


@lru_cache(maxsize=1)
def _session() -> AuthorizedSession:
    token_file = setting("ADMOB_TOKEN_FILE") or str(TOKEN_FILE)
    try:
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    except FileNotFoundError:
        try:
            creds, _ = google.auth.default(scopes=SCOPES)
        except DefaultCredentialsError as err:
            raise ConsoleError(f"No AdMob credentials. {LOGIN_HINT}") from err
    return AuthorizedSession(creds)


def login(client_secrets: str, no_browser: bool = False) -> str:
    """OAuth consent in the browser once; the refresh token is stored in ~/.config/appconsole."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(client_secrets, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=not no_browser)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json())
    TOKEN_FILE.chmod(0o600)
    _session.cache_clear()
    _account.cache_clear()
    return f"Saved AdMob credentials to {TOKEN_FILE}."


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


def _list(resource: str) -> list[dict]:
    items, token = [], None
    while True:
        r = _get(f"{_account()}/{resource}", pageSize=1000, **({"pageToken": token} if token else {}))
        items += r.get(resource, [])
        token = r.get("nextPageToken")
        if not token:
            return items


def _app_name(a: dict) -> str:
    return (a.get("linkedAppInfo", {}).get("displayName") or a.get("manualAppInfo", {}).get("displayName")
            or a["appId"])


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


def _ymd(d: date) -> dict:
    return {"year": d.year, "month": d.month, "day": d.day}


def run_report(start: date, end: date, dims: list[str], mediation: bool, currency: str, rows: int,
               app_filter: str = "") -> tuple[list[dict], str]:
    spec = {"dateRange": {"startDate": _ymd(start), "endDate": _ymd(end)}, "dimensions": dims,
            "metrics": MEDIATION_METRICS if mediation else NETWORK_METRICS,
            "maxReportRows": rows}
    time_dims = [d for d in dims if d in ("DATE", "MONTH")]
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
    """AdMob earnings, impressions, eCPM, match and show rate, with the change against the previous period.

    Args:
        days: number of full days ending yesterday (or end_date)
        by: grouping, comma-separated: app, ad_unit, country, format, platform, date, month, ad_source
            (ad_source uses the mediation report)
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
    if "APP" in dims and "PLATFORM" not in dims:
        dims.append("PLATFORM")  # an Android and an iOS app often share a name
    mediation = "AD_SOURCE" in dims
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
                   f"match {metric(r, 'MATCH_RATE') * 100:.0f}%{show}")
    return "\n".join(out)
