"""App Store Connect: apps, versions, listings, reviews, builds, submission, release, sales and search rank.

Auth: an App Store Connect API key (Users and Access > Integrations > App Store Connect API):
key id, issuer id and the .p8 private key. A fastlane-style api_key.json also works.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import time
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import jwt
import requests

from .core import DRY_RUN, ConsoleError, Toolset, call, check_limits, clip, date_range, diff_lines, setting

BASE = "https://api.appstoreconnect.apple.com"
VERSION_LIMITS = {"description": 4000, "keywords": 100, "promotionalText": 170, "whatsNew": 4000}
INFO_LIMITS = {"name": 30, "subtitle": 30}
EDITABLE = {"PREPARE_FOR_SUBMISSION", "READY_FOR_REVIEW", "DEVELOPER_REJECTED", "REJECTED", "METADATA_REJECTED",
            "INVALID_BINARY"}
LIVE = {"READY_FOR_SALE", "READY_FOR_DISTRIBUTION"}
DOWNLOAD_TYPES = {"1", "1F", "1T", "1E", "1EP", "1EU", "F1", "1-B", "F1-B"}
UPDATE_TYPES = {"7", "7F", "7T", "F7", "3F"}

toolset = Toolset(
    "appstore", "app-store-connect", "App Store Connect",
    "App Store Connect through the official API. Read tools are safe. Tools that change something "
    "(listing, version, submit, release, reply) are a dry run unless confirm=true; only pass confirm=true "
    "after the user explicitly approved that exact change. `app` accepts the numeric Apple ID, the bundle id "
    "or part of the app name.",
)
tool = toolset.tool

KEY_HINT = ("Create a key in App Store Connect > Users and Access > Integrations > App Store Connect API "
            "(see the README, 'App Store Connect setup').")

_token_cache: dict[str, tuple[str, float]] = {}
_http = requests.Session()


def _credentials() -> tuple[str, str, str]:
    fastlane_json = setting("ASC_API_KEY_JSON")
    if fastlane_json:
        d = json.loads(Path(fastlane_json).read_text())
        return d["key_id"], d["issuer_id"], d["key"]
    key_id = setting("ASC_KEY_ID", KEY_HINT)
    issuer = setting("ASC_ISSUER_ID", KEY_HINT)
    key = setting("ASC_PRIVATE_KEY")
    if key:
        return key_id, issuer, key.replace("\\n", "\n")
    candidates = [setting("ASC_PRIVATE_KEY_PATH")] if setting("ASC_PRIVATE_KEY_PATH") else [
        str(Path(d).expanduser() / f"AuthKey_{key_id}.p8")
        for d in ("./private_keys", "~/private_keys", "~/.private_keys", "~/.appstoreconnect/private_keys")]
    for c in candidates:
        if Path(c).is_file():
            return key_id, issuer, Path(c).read_text()
    raise ConsoleError(f"Private key for {key_id} not found. Set ASC_PRIVATE_KEY_PATH to the AuthKey_{key_id}.p8 file.")


def _token() -> str:
    cached = _token_cache.get("t")
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    key_id, issuer, key = _credentials()
    now = int(time.time())
    exp = now + 19 * 60  # Apple allows at most 20 minutes
    tok = jwt.encode({"iss": issuer, "iat": now, "exp": exp, "aud": "appstoreconnect-v1"}, key,
                     algorithm="ES256", headers={"kid": key_id, "typ": "JWT"})
    _token_cache["t"] = (tok, exp)
    return tok


def _asc(method: str, path: str, *, raw: bool = False, **kw):
    url = path if path.startswith("http") else BASE + path
    headers = {"Authorization": f"Bearer {_token()}", **kw.pop("headers", {})}
    return call(_http, method, url, headers=headers, raw=raw, **kw)


def _all(path: str, params: dict | None = None, limit: int = 0) -> tuple[list, list]:
    """GET every page of a collection (up to `limit` items). Returns (data, included)."""
    data, included, url = [], [], path
    while url:
        r = _asc("GET", url, params=params)
        params = None  # the next link already has them
        data += r.get("data", [])
        included += r.get("included", [])
        if limit and len(data) >= limit:
            break
        url = r.get("links", {}).get("next")
    return (data[:limit] if limit else data), included


def _rel_id(item: dict, rel: str) -> str | None:
    d = (item.get("relationships", {}).get(rel) or {}).get("data")
    return d.get("id") if isinstance(d, dict) else None


def _app(app: str) -> dict:
    """Resolve an app by Apple ID, bundle id or name. Returns the API resource."""
    app = app.strip()
    fields = {"fields[apps]": "name,bundleId,sku,primaryLocale"}
    if app.isdigit():
        return _asc("GET", f"/v1/apps/{app}", params=fields)["data"]
    found, _ = _all("/v1/apps", {"filter[bundleId]": app, **fields})
    found = [a for a in found if a["attributes"]["bundleId"] == app]
    if found:
        return found[0]
    apps, _ = _all("/v1/apps", {"limit": 200, **fields})
    matches = [a for a in apps if app.lower() in a["attributes"]["name"].lower()
               or app.lower() in a["attributes"]["bundleId"].lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ConsoleError(f"No app matches '{app}'. Use appstore_apps to list them.")
    raise ConsoleError(f"'{app}' matches several apps: "
                       + ", ".join(f"{a['attributes']['name']} ({a['id']})" for a in matches))


def _state(v: dict) -> str:
    a = v["attributes"]
    return a.get("appVersionState") or a.get("appStoreState") or a.get("state") or "?"


def _versions(app_id: str, platform: str = "") -> list[dict]:
    params = {"limit": 50, "fields[appStoreVersions]": "versionString,appStoreState,appVersionState,platform,"
              "createdDate,releaseType,build", "include": "build", "fields[builds]": "version"}
    if platform:
        params["filter[platform]"] = platform
    data, included = _all(f"/v1/apps/{app_id}/appStoreVersions", params, limit=50)
    builds = {b["id"]: b["attributes"].get("version") for b in included if b["type"] == "builds"}
    for v in data:
        v["_build"] = builds.get(_rel_id(v, "build"))
    return sorted(data, key=lambda v: v["attributes"].get("createdDate") or "", reverse=True)


def _pick_version(app_id: str, platform: str, which: str = "") -> dict:
    """which: '' = editable if any, else live; 'live'; 'editable'; or a version string."""
    vs = _versions(app_id, platform)
    if not vs:
        raise ConsoleError("This app has no App Store versions yet.")
    editable = [v for v in vs if _state(v) in EDITABLE]
    live = [v for v in vs if _state(v) in LIVE]
    if which == "editable" or (not which and editable):
        if not editable:
            raise ConsoleError("No editable version. Create one with appstore_create_version.")
        return editable[0]
    if which in ("", "live"):
        return (live or vs)[0]
    for v in vs:
        if v["attributes"]["versionString"] == which:
            return v
    raise ConsoleError(f"Version {which} not found. Versions: " + ", ".join(v["attributes"]["versionString"] for v in vs))


def _app_info(app_id: str) -> dict:
    infos, _ = _all(f"/v1/apps/{app_id}/appInfos")
    if not infos:
        raise ConsoleError("App has no appInfo.")
    not_live = [i for i in infos if _state(i) not in LIVE]
    return (not_live or infos)[0]


def _by_locale(items: list[dict]) -> dict[str, dict]:
    return {i["attributes"]["locale"]: i for i in items}


# ---------------------------------------------------------------- apps and versions

@tool()
def appstore_apps() -> str:
    """List every app in the App Store Connect account: Apple ID, bundle id, name and SKU."""
    apps, _ = _all("/v1/apps", {"limit": 200, "fields[apps]": "name,bundleId,sku"})
    return "\n".join(f"{a['id']}  {a['attributes']['bundleId']}  {a['attributes']['name']}  sku={a['attributes']['sku']}"
                     for a in apps) or "No apps."


@tool()
def appstore_versions(app: str, platform: str = "IOS", limit: int = 6) -> str:
    """Latest App Store versions with their state (e.g. READY_FOR_SALE, WAITING_FOR_REVIEW, REJECTED) and build.

    Args:
        app: Apple ID, bundle id or part of the name
        platform: IOS, MAC_OS, TV_OS or VISION_OS
        limit: how many versions to show
    """
    a = _app(app)
    vs = _versions(a["id"], platform)[:limit]
    out = [f"{a['attributes']['name']} ({a['id']}, {a['attributes']['bundleId']}):"]
    out += [f"  {v['attributes']['versionString']} {_state(v)} build={v['_build'] or '-'} "
            f"release={v['attributes'].get('releaseType') or '-'} created={(v['attributes'].get('createdDate') or '')[:10]}"
            for v in vs]
    return "\n".join(out)


@tool(write=True)
def appstore_create_version(app: str, version: str, platform: str = "IOS", release_type: str = "",
                            confirm: bool = False) -> str:
    """Create a new App Store version (the editable 'Prepare for Submission' version).

    Args:
        app: Apple ID, bundle id or part of the name
        version: version string, e.g. 1.4.0
        platform: IOS, MAC_OS, TV_OS or VISION_OS
        release_type: MANUAL, AFTER_APPROVAL or SCHEDULED (empty keeps Apple's default)
        confirm: true to create it
    """
    a = _app(app)
    vs = _versions(a["id"], platform)
    editable = [v for v in vs if _state(v) in EDITABLE]
    if editable:
        raise ConsoleError(f"There is already an editable version: {editable[0]['attributes']['versionString']} "
                           f"({_state(editable[0])}).")
    plan = f"{a['attributes']['name']}: new {platform} version {version}" + (f" ({release_type})" if release_type else "")
    if not confirm:
        latest = vs[0]["attributes"]["versionString"] if vs else "none"
        return f"{DRY_RUN}.\nPlan: {plan} (latest is {latest})"
    attrs = {"platform": platform, "versionString": version, **({"releaseType": release_type} if release_type else {})}
    r = _asc("POST", "/v1/appStoreVersions", json={"data": {
        "type": "appStoreVersions", "attributes": attrs,
        "relationships": {"app": {"data": {"type": "apps", "id": a["id"]}}}}})
    return f"Created {version} (id {r['data']['id']}), state {_state(r['data'])}."


# ---------------------------------------------------------------- listing

@tool()
def appstore_listing(app: str, locale: str = "en-US", version: str = "", platform: str = "IOS") -> str:
    """App Store listing for one locale: name, subtitle, description, keywords, promotional text, what's new.

    Args:
        app: Apple ID, bundle id or part of the name
        locale: e.g. en-US, es-MX, pt-BR
        version: '' (editable version if any, else live), 'live', 'editable' or a version string
        platform: IOS, MAC_OS, TV_OS or VISION_OS
    """
    a = _app(app)
    v = _pick_version(a["id"], platform, version)
    vlocs = _by_locale(_all(f"/v1/appStoreVersions/{v['id']}/appStoreVersionLocalizations")[0])
    info = _app_info(a["id"])
    ilocs = _by_locale(_all(f"/v1/appInfos/{info['id']}/appInfoLocalizations")[0])
    va = vlocs.get(locale, {}).get("attributes", {})
    ia = ilocs.get(locale, {}).get("attributes", {})
    if not va and not ia:
        return f"No {locale} localization. Locales: {', '.join(sorted(vlocs))}"

    def line(label, value, limit):
        return f"{label} ({len(value or '')}/{limit}): {value or ''}"

    return "\n".join([
        f"{a['attributes']['name']} · {v['attributes']['versionString']} ({_state(v)}) · {locale} · "
        f"locales: {', '.join(sorted(vlocs))}",
        line("Name", ia.get("name"), 30), line("Subtitle", ia.get("subtitle"), 30),
        line("Keywords", va.get("keywords"), 100), line("Promotional text", va.get("promotionalText"), 170),
        line("What's new", va.get("whatsNew"), 4000),
        f"Description ({len(va.get('description') or '')}/4000):\n{va.get('description') or ''}"])


@tool(write=True)
def appstore_update_listing(app: str, locale: str, name: str = "", subtitle: str = "", description: str = "",
                            keywords: str = "", promotional_text: str = "", whats_new: str = "",
                            platform: str = "IOS", confirm: bool = False) -> str:
    """Change listing texts for one locale. Empty fields are left as they are; a missing locale is created.

    Name, subtitle, description, keywords and what's new go to the editable version (they ship with the
    next review). Promotional text can change any time: with no editable version it goes to the live one.

    Args:
        app: Apple ID, bundle id or part of the name
        locale: e.g. en-US
        name: app name (max 30)
        subtitle: subtitle (max 30)
        description: description (max 4000)
        keywords: comma-separated keywords (max 100)
        promotional_text: promotional text (max 170)
        whats_new: release notes of the editable version (max 4000)
        platform: IOS, MAC_OS, TV_OS or VISION_OS
        confirm: true to save
    """
    vnew = {k: v for k, v in (("description", description), ("keywords", keywords),
                              ("promotionalText", promotional_text), ("whatsNew", whats_new)) if v}
    inew = {k: v for k, v in (("name", name), ("subtitle", subtitle)) if v}
    if not vnew and not inew:
        return "Nothing to change."
    check_limits(vnew, VERSION_LIMITS)
    check_limits(inew, INFO_LIMITS)
    a = _app(app)
    out, plan, v = [f"{a['attributes']['name']} {locale}:"], [], None
    if vnew:
        v = _pick_version(a["id"], platform, "" if set(vnew) == {"promotionalText"} else "editable")
        out[0] = f"{a['attributes']['name']} {v['attributes']['versionString']} ({_state(v)}) {locale}:"
        loc = _by_locale(_all(f"/v1/appStoreVersions/{v['id']}/appStoreVersionLocalizations")[0]).get(locale)
        plan.append(("version", loc, vnew))
        out.append(diff_lines(loc["attributes"] if loc else {}, vnew))
    if inew:
        info = _app_info(a["id"])
        if _state(info) not in EDITABLE:
            raise ConsoleError("Name and subtitle can only change while a new version is being prepared "
                               f"(app info is {_state(info)}). Create one with appstore_create_version.")
        loc = _by_locale(_all(f"/v1/appInfos/{info['id']}/appInfoLocalizations")[0]).get(locale)
        if not loc and "name" not in inew:
            raise ConsoleError(f"{locale} is a new locale: a name is required.")
        plan.append(("info", loc, inew, info["id"]))
        out.append(diff_lines(loc["attributes"] if loc else {}, inew))
    if not confirm:
        return f"{DRY_RUN}.\n" + "\n".join(out)
    for kind, loc, attrs, *extra in plan:
        typ, parent_rel, parent_type, parent_id = (
            ("appStoreVersionLocalizations", "appStoreVersion", "appStoreVersions", v["id"]) if kind == "version"
            else ("appInfoLocalizations", "appInfo", "appInfos", extra[0]))
        if loc:
            _asc("PATCH", f"/v1/{typ}/{loc['id']}", json={"data": {"type": typ, "id": loc["id"], "attributes": attrs}})
        else:
            _asc("POST", f"/v1/{typ}", json={"data": {
                "type": typ, "attributes": {"locale": locale, **attrs},
                "relationships": {parent_rel: {"data": {"type": parent_type, "id": parent_id}}}}})
    return "Saved.\n" + "\n".join(out)


# ---------------------------------------------------------------- reviews

@tool()
def appstore_reviews(app: str, limit: int = 20, unanswered_only: bool = False, territory: str = "",
                     rating: int = 0) -> str:
    """Latest App Store customer reviews (newest first). Each line ends with the id needed to reply.

    Args:
        app: Apple ID, bundle id or part of the name
        limit: maximum number of reviews (up to 200 per page)
        unanswered_only: only reviews without a published developer response
        territory: optional three-letter territory, e.g. USA, MEX
        rating: optional star filter, 1-5
    """
    a = _app(app)
    params = {"sort": "-createdDate", "limit": min(limit, 200), "include": "response",
              "fields[customerReviews]": "rating,title,body,reviewerNickname,createdDate,territory,response"}
    if unanswered_only:
        params["exists[publishedResponse]"] = "false"
    if territory:
        params["filter[territory]"] = territory.upper()
    if rating:
        params["filter[rating]"] = str(rating)
    data, _ = _all(f"/v1/apps/{a['id']}/customerReviews", params, limit=limit)
    out = [f"[{r['attributes']['rating']}★ {r['attributes'].get('territory', '')} "
           f"{(r['attributes'].get('createdDate') or '')[:10]}] {clip(r['attributes'].get('title'), 80)} — "
           f"{clip(r['attributes'].get('body'), 400)} · id={r['id']}{' · replied' if _rel_id(r, 'response') else ''}"
           for r in data]
    return "\n".join(out) or "No reviews match."


@tool(write=True)
def appstore_reply(review_id: str, text: str, confirm: bool = False) -> str:
    """Publish (or replace) the developer response to an App Store review. It is public.

    Args:
        review_id: id from appstore_reviews
        text: response text
        confirm: true to publish
    """
    if len(text) > 5970:
        raise ConsoleError(f"Response has {len(text)} characters (max 5970).")
    if not confirm:
        return f"{DRY_RUN}. Would respond to review {review_id}:\n{text}"
    _asc("POST", "/v1/customerReviewResponses", json={"data": {
        "type": "customerReviewResponses", "attributes": {"responseBody": text},
        "relationships": {"review": {"data": {"type": "customerReviews", "id": review_id}}}}})
    return "Response submitted (Apple shows it after a short moderation delay)."


# ---------------------------------------------------------------- builds, submission, release

def _builds(app_id: str, limit: int, version: str = "", build: str = "", platform: str = "IOS") -> list[dict]:
    params = {"filter[app]": app_id, "sort": "-uploadedDate", "limit": min(limit, 200),
              "include": "preReleaseVersion", "fields[preReleaseVersions]": "version,platform",
              "fields[builds]": "version,uploadedDate,processingState,expired,usesNonExemptEncryption,"
                                "preReleaseVersion",
              "filter[preReleaseVersion.platform]": platform}
    if version:
        params["filter[preReleaseVersion.version]"] = version
    if build:
        params["filter[version]"] = build
    data, included = _all("/v1/builds", params, limit=limit)
    pre = {p["id"]: p["attributes"].get("version") for p in included if p["type"] == "preReleaseVersions"}
    for b in data:
        b["_version"] = pre.get(_rel_id(b, "preReleaseVersion"), "?")
    return data


def _fmt_build(b: dict) -> str:
    at = b["attributes"]
    enc = at.get("usesNonExemptEncryption")
    return (f"{b['_version']} ({at.get('version')}) {at.get('processingState')}"
            f"{' EXPIRED' if at.get('expired') else ''} uploaded={(at.get('uploadedDate') or '')[:16]}"
            f" encryption={'unset' if enc is None else enc}")


@tool()
def appstore_builds(app: str, limit: int = 10, platform: str = "IOS") -> str:
    """Recent builds (TestFlight and App Store) with processing state and export-compliance flag.

    Args:
        app: Apple ID, bundle id or part of the name
        limit: how many builds
        platform: IOS, MAC_OS, TV_OS or VISION_OS
    """
    a = _app(app)
    bs = _builds(a["id"], limit, platform=platform)
    return "\n".join([f"{a['attributes']['name']}:"] + [f"  {_fmt_build(b)}" for b in bs]) if bs else "No builds."


@tool(write=True)
def appstore_submit(app: str, build: str = "", platform: str = "IOS", confirm: bool = False) -> str:
    """Attach a build to the editable version and submit it to App Review.

    Without confirm it only checks: editable version, chosen build, missing what's new, export compliance.

    Args:
        app: Apple ID, bundle id or part of the name
        build: build number (CFBundleVersion); empty picks the newest valid build of that version
        platform: IOS, MAC_OS, TV_OS or VISION_OS
        confirm: true to submit for review
    """
    a = _app(app)
    v = _pick_version(a["id"], platform, "editable")
    vs = v["attributes"]["versionString"]
    candidates = [b for b in _builds(a["id"], 20, version=vs, build=build, platform=platform)
                  if b["attributes"].get("processingState") == "VALID" and not b["attributes"].get("expired")]
    if not candidates:
        raise ConsoleError(f"No valid build {build or ''} for version {vs}. Check appstore_builds.")
    b = candidates[0]
    warnings = []
    if b["attributes"].get("usesNonExemptEncryption") is None:
        warnings.append("export compliance is unset for this build: answer it in App Store Connect or set "
                        "ITSAppUsesNonExemptEncryption in Info.plist, otherwise the submission is blocked")
    locs = _all(f"/v1/appStoreVersions/{v['id']}/appStoreVersionLocalizations")[0]
    if len(_versions(a["id"], platform)) > 1:
        missing = [loc["attributes"]["locale"] for loc in locs if not loc["attributes"].get("whatsNew")]
        if missing:
            warnings.append("what's new is empty in: " + ", ".join(missing))
    plan = f"{a['attributes']['name']} {vs} ({_state(v)}) with build {_fmt_build(b)}"
    if not confirm:
        return f"{DRY_RUN}.\nPlan: submit {plan}" + "".join(f"\nWarning: {w}" for w in warnings)
    _asc("PATCH", f"/v1/appStoreVersions/{v['id']}/relationships/build", json={"data": {"type": "builds", "id": b["id"]}})
    open_subs, _ = _all("/v1/reviewSubmissions", {"filter[app]": a["id"], "filter[platform]": platform,
                                                  "filter[state]": "READY_FOR_REVIEW"})
    if open_subs:
        sub_id = open_subs[0]["id"]
    else:
        sub_id = _asc("POST", "/v1/reviewSubmissions", json={"data": {
            "type": "reviewSubmissions", "attributes": {"platform": platform},
            "relationships": {"app": {"data": {"type": "apps", "id": a["id"]}}}}})["data"]["id"]
    items, _ = _all(f"/v1/reviewSubmissions/{sub_id}/items", {"include": "appStoreVersion"})
    if not any(_rel_id(i, "appStoreVersion") == v["id"] for i in items):
        _asc("POST", "/v1/reviewSubmissionItems", json={"data": {
            "type": "reviewSubmissionItems", "relationships": {
                "reviewSubmission": {"data": {"type": "reviewSubmissions", "id": sub_id}},
                "appStoreVersion": {"data": {"type": "appStoreVersions", "id": v["id"]}}}}})
    _asc("PATCH", f"/v1/reviewSubmissions/{sub_id}", json={"data": {
        "type": "reviewSubmissions", "id": sub_id, "attributes": {"submitted": True}}})
    return f"Submitted for review: {plan}."


@tool(write=True)
def appstore_release(app: str, platform: str = "IOS", confirm: bool = False) -> str:
    """Release an approved version that is waiting for manual release (PENDING_DEVELOPER_RELEASE).

    Args:
        app: Apple ID, bundle id or part of the name
        platform: IOS, MAC_OS, TV_OS or VISION_OS
        confirm: true to release it to the App Store
    """
    a = _app(app)
    pending = [v for v in _versions(a["id"], platform) if _state(v) == "PENDING_DEVELOPER_RELEASE"]
    if not pending:
        raise ConsoleError("No version is waiting for manual release.")
    v = pending[0]
    plan = f"{a['attributes']['name']} {v['attributes']['versionString']}"
    if not confirm:
        return f"{DRY_RUN}.\nPlan: release {plan} to the App Store."
    _asc("POST", "/v1/appStoreVersionReleaseRequests", json={"data": {
        "type": "appStoreVersionReleaseRequests",
        "relationships": {"appStoreVersion": {"data": {"type": "appStoreVersions", "id": v["id"]}}}}})
    return f"Release requested: {plan}."


# ---------------------------------------------------------------- sales

def parse_sales(tsv: str, acc: dict) -> None:
    """Add one daily SALES/SUMMARY report into acc[app] = {title, downloads, updates, iap, proceeds{cur: x}}."""
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t"):
        units = int(float(row.get("Units") or 0))
        ptype = (row.get("Product Type Identifier") or "").strip()
        parent = (row.get("Parent Identifier") or "").strip()
        key = parent or (row.get("SKU") or "").strip()
        app = acc.setdefault(key, {"title": "", "downloads": 0, "updates": 0, "iap": 0,
                                   "proceeds": defaultdict(float)})
        if not parent:
            app["title"] = row.get("Title") or app["title"]
        if ptype in DOWNLOAD_TYPES:
            app["downloads"] += units
        elif ptype in UPDATE_TYPES:
            app["updates"] += units
        elif parent:
            app["iap"] += units
        cur = row.get("Currency of Proceeds") or "?"
        app["proceeds"][cur] += units * float(row.get("Developer Proceeds") or 0)


@tool()
def appstore_sales(days: int = 7) -> str:
    """Downloads, in-app purchase units and proceeds per app for the last days (daily sales reports).

    Needs ASC_VENDOR_NUMBER (App Store Connect > Payments and Financial Reports) and a key with Sales access.
    Apple publishes each day's report the next day, so the newest day may be missing.

    Args:
        days: number of days back from yesterday
    """
    vendor = setting("ASC_VENDOR_NUMBER", "Find it in App Store Connect > Payments and Financial Reports.")
    start, end = date_range(days)
    acc: dict = {}
    got, missing = 0, []
    d = start
    while d <= end:
        r = _http.get(f"{BASE}/v1/salesReports", timeout=60, headers={
            "Authorization": f"Bearer {_token()}", "Accept": "application/a-gzip"}, params={
            "filter[frequency]": "DAILY", "filter[reportType]": "SALES", "filter[reportSubType]": "SUMMARY",
            "filter[vendorNumber]": vendor, "filter[reportDate]": d.isoformat()})
        if r.status_code == 404:
            missing.append(d.isoformat())
        elif r.status_code >= 400:
            raise ConsoleError(f"HTTP {r.status_code} on sales report {d}: {r.text[:300]}")
        else:
            parse_sales(gzip.decompress(r.content).decode("utf-8"), acc)
            got += 1
        d += timedelta(days=1)
    out = [f"App Store sales {start} to {end} ({got} daily reports" + (f", none for {', '.join(missing)}" if missing else "")
           + "):"]
    for _key, app in sorted(acc.items(), key=lambda x: -x[1]["downloads"]):
        money = ", ".join(f"{v:.2f} {c}" for c, v in app["proceeds"].items() if v)
        out.append(f"  {app['title'] or _key}: {app['downloads']} downloads, {app['updates']} updates, "
                   f"{app['iap']} in-app units" + (f", proceeds {money}" if money else ""))
    totals: dict[str, float] = defaultdict(float)
    for app in acc.values():
        for c, v in app["proceeds"].items():
            totals[c] += v
    out.append("Total proceeds: " + (", ".join(f"{v:.2f} {c}" for c, v in totals.items() if v) or "0"))
    return "\n".join(out)


# ---------------------------------------------------------------- public search

@tool()
def appstore_search_rank(app: str, keywords: list[str], country: str = "us") -> str:
    """Approximate position of an iOS app in App Store search (public iTunes Search API, top 200). No key needed.

    Args:
        app: numeric Apple ID or bundle id
        keywords: search terms to check
        country: two-letter country code
    """
    out = []
    for q in keywords:
        r = call(_http, "GET", "https://itunes.apple.com/search",
                 params={"term": q, "country": country, "entity": "software", "limit": 200}).get("results", [])
        pos = next((i + 1 for i, x in enumerate(r) if app in (str(x.get("trackId")), x.get("bundleId"))), None)
        out.append(f"  «{q}» ({country.upper()}): {pos or 'not in top 200'} · top: "
                   + ", ".join(clip(x.get("trackName"), 22) for x in r[:3]))
    return f"{app}:\n" + "\n".join(out)
