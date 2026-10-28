"""Google Play Console: listings, releases and tracks, reviews, vitals, products and search rank.

Auth: a Google Cloud service account JSON key that was invited in Play Console
(Users and permissions) with the permissions the tools need.
"""
from __future__ import annotations

import csv
import io
import os
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

from .core import DRY_RUN, ConsoleError, Toolset, api_error, call, check_limits, clip, diff_lines, setting

API = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications"
UPLOAD_API = "https://androidpublisher.googleapis.com/upload/androidpublisher/v3/applications"
REPORTING = "https://playdeveloperreporting.googleapis.com/v1beta1"
DEVELOPERS = "https://androidpublisher.googleapis.com/androidpublisher/v3/developers"
SCOPE_PUBLISHER = "https://www.googleapis.com/auth/androidpublisher"
SCOPE_REPORTING = "https://www.googleapis.com/auth/playdeveloperreporting"
LISTING_LIMITS = {"title": 30, "shortDescription": 80, "fullDescription": 4000}
RELEASE_NOTES_LIMIT = 500
REPLY_LIMIT = 350
BAD_BEHAVIOR = {"crash": 1.09, "anr": 0.47}  # Google's overall user-perceived thresholds, in %
IMAGE_TYPES = ["phoneScreenshots", "sevenInchScreenshots", "tenInchScreenshots", "tvScreenshots", "wearScreenshots",
               "icon", "featureGraphic", "tvBanner"]
SINGLE_IMAGES = {"icon", "featureGraphic", "tvBanner"}
IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
APP_PERMISSIONS = ["CAN_ACCESS_APP", "CAN_VIEW_FINANCIAL_DATA", "CAN_MANAGE_PERMISSIONS", "CAN_REPLY_TO_REVIEWS",
                   "CAN_MANAGE_PUBLIC_APKS", "CAN_MANAGE_TRACK_APKS", "CAN_MANAGE_TRACK_USERS", "CAN_MANAGE_PUBLIC_LISTING",
                   "CAN_MANAGE_DRAFT_APPS", "CAN_MANAGE_ORDERS", "CAN_MANAGE_APP_CONTENT", "CAN_VIEW_NON_FINANCIAL_DATA",
                   "CAN_VIEW_APP_QUALITY", "CAN_MANAGE_DEEPLINKS"]
_READ = ["CAN_ACCESS_APP", "CAN_VIEW_NON_FINANCIAL_DATA", "CAN_VIEW_APP_QUALITY"]
PERMISSION_PRESETS = {
    "read": _READ,
    # what a CI/deploy service account needs: releases on every track, the store listing and testers
    "deploy": _READ + ["CAN_MANAGE_TRACK_APKS", "CAN_MANAGE_PUBLIC_APKS", "CAN_MANAGE_DRAFT_APPS",
                       "CAN_MANAGE_PUBLIC_LISTING", "CAN_MANAGE_TRACK_USERS"],
    "full": APP_PERMISSIONS,
}

toolset = Toolset(
    "play", "google-play", "Google Play Console",
    "Google Play Console through the official APIs. Read tools are safe. Tools that change something "
    "(listing, upload, promote, rollout, reply) are a dry run unless confirm=true; only pass confirm=true "
    "after the user explicitly approved that exact change. `package` is the Android package name.",
)
tool = toolset.tool

SA_HINT = ("Point it to a service account JSON key that has access in Play Console > Users and permissions "
           "(see the README, 'Google Play setup').")


@lru_cache(maxsize=4)
def _session(scope: str = SCOPE_PUBLISHER) -> AuthorizedSession:
    path = setting("GOOGLE_PLAY_SERVICE_ACCOUNT") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not path:
        setting("GOOGLE_PLAY_SERVICE_ACCOUNT", SA_HINT)  # raises with the hint
    if not Path(path).is_file():
        raise ConsoleError(f"Service account file not found: {path}")
    creds = service_account.Credentials.from_service_account_file(path, scopes=[scope])
    return AuthorizedSession(creds)


def _api(method: str, path: str, **kw):
    return call(_session(), method, f"{API}/{path}", **kw)


class _Edit:
    """A Play edit (a draft of app changes). Deleted on exit unless committed."""

    def __init__(self, package: str):
        self.package = package
        self.id = ""
        self.committed = False

    def __enter__(self):
        self.id = _api("POST", f"{self.package}/edits", json={})["id"]
        return self

    def __exit__(self, *exc):
        if not self.committed:
            try:
                _api("DELETE", f"{self.package}/edits/{self.id}")
            except ConsoleError:
                pass  # edits expire on their own

    def __call__(self, method: str, path: str = "", **kw):
        return _api(method, f"{self.package}/edits/{self.id}{'/' + path if path else ''}", **kw)

    def validate(self):
        return _api("POST", f"{self.package}/edits/{self.id}:validate")

    def commit(self) -> str:
        """Commit; if Google refuses to send it for review automatically, save it without sending."""
        try:
            _api("POST", f"{self.package}/edits/{self.id}:commit")
            self.committed = True
            return "Committed and sent to Google for review (or published if no review is needed)."
        except ConsoleError as err:
            if "changesNotSentForReview" not in str(err):
                raise
        _api("POST", f"{self.package}/edits/{self.id}:commit", params={"changesNotSentForReview": "true"})
        self.committed = True
        return ("Saved in Play Console but NOT sent for review: Google requires that step by hand "
                "(Publishing overview > Send for review), usually because managed publishing is on.")


def _fmt_release(r: dict) -> str:
    frac = f" {r['userFraction'] * 100:g}%" if r.get("userFraction") else ""
    langs = [n["language"] for n in r.get("releaseNotes", [])]
    notes = ",".join(langs) if len(langs) <= 3 else f"{len(langs)} languages"
    return (f"{r.get('name') or '(no name)'} codes={','.join(r.get('versionCodes', [])) or '-'} "
            f"{r.get('status')}{frac}" + (f" notes:{notes}" if notes else ""))


def _release_notes(text: str, language: str) -> list[dict]:
    if not text:
        return []
    if len(text) > RELEASE_NOTES_LIMIT:
        raise ConsoleError(f"Release notes have {len(text)} characters (max {RELEASE_NOTES_LIMIT}).")
    return [{"language": language, "text": text}]


def _status(rollout_percent: float, draft: bool) -> dict:
    if draft:
        return {"status": "draft"}
    if not 0 < rollout_percent <= 100:
        raise ConsoleError("rollout_percent must be > 0 and <= 100.")
    if rollout_percent < 100:
        return {"status": "inProgress", "userFraction": round(rollout_percent / 100, 4)}
    return {"status": "completed"}


# ---------------------------------------------------------------- apps and listings

@tool()
def play_apps() -> str:
    """List the apps this service account can see (package name and title).

    Uses the Play Developer Reporting API, which must be enabled in the key's Google Cloud project.
    """
    apps, token = [], None
    while True:
        r = call(_session(SCOPE_REPORTING), "GET", f"{REPORTING}/apps:search",
                 params={"pageSize": 1000, **({"pageToken": token} if token else {})})
        apps += r.get("apps", [])
        token = r.get("nextPageToken")
        if not token:
            break
    return "\n".join(f"{a['packageName']}  {a.get('displayName', '')}" for a in apps) or "No apps visible to this key."


@tool()
def play_listing(package: str, language: str = "en-US") -> str:
    """Store listing saved in Play Console for one language (title, short and full description).

    Args:
        package: Android package name, e.g. com.example.app
        language: BCP-47 code as used by Play, e.g. en-US, es-419, pt-BR
    """
    with _Edit(package) as e:
        languages = [x["language"] for x in e("GET", "listings").get("listings", [])]
        if language not in languages:
            return f"{package} has no {language} listing. Languages: {', '.join(languages)}"
        x = e("GET", f"listings/{language}")
    return (f"{package} · {language} · languages: {', '.join(languages)}\n"
            f"Title ({len(x.get('title', ''))}/30): {x.get('title', '')}\n"
            f"Short ({len(x.get('shortDescription', ''))}/80): {x.get('shortDescription', '')}\n"
            f"Full ({len(x.get('fullDescription', ''))}/4000):\n{x.get('fullDescription', '')}")


@tool(write=True)
def play_update_listing(package: str, language: str, title: str = "", short_description: str = "",
                        full_description: str = "", confirm: bool = False) -> str:
    """Change the title, short or full description of one listing language (creates the language if new).

    Without confirm it is a dry run: Google validates the texts inside a draft edit that is then discarded.

    Args:
        package: Android package name
        language: listing language, e.g. en-US
        title: new title (max 30), empty keeps the current one
        short_description: new short description (max 80), empty keeps it
        full_description: new full description (max 4000), empty keeps it
        confirm: true to save and send to review
    """
    new = {k: v for k, v in (("title", title), ("shortDescription", short_description),
                             ("fullDescription", full_description)) if v}
    if not new:
        return "Nothing to change."
    check_limits(new, LISTING_LIMITS)
    with _Edit(package) as e:
        existing = [x["language"] for x in e("GET", "listings").get("listings", [])]
        if language in existing:
            before = e("GET", f"listings/{language}")
            e("PATCH", f"listings/{language}", json=new)
        else:
            if "title" not in new:
                raise ConsoleError(f"{language} is a new listing language: a title is required.")
            before = {}
            e("PUT", f"listings/{language}", json={"language": language, **new})
        e.validate()
        changes = diff_lines(before, new)
        if not confirm:
            return f"{DRY_RUN}. Google accepted the texts for {package} {language}.\n{changes}"
        return f"{e.commit()}\n{package} {language}:\n{changes}"


# ---------------------------------------------------------------- releases

@tool()
def play_tracks(package: str) -> str:
    """Releases on every track (production, beta, alpha, internal, custom): name, version codes, status, rollout %."""
    with _Edit(package) as e:
        tracks = e("GET", "tracks").get("tracks", [])
    out = [f"{package}:"]
    for t in tracks:
        releases = t.get("releases", [])
        out += [f"  {t['track']}: {_fmt_release(r)}" for r in releases] or [f"  {t['track']}: (empty)"]
    out.append("Note: the API cannot see whether approved changes are waiting for 'Publish' (managed publishing).")
    return "\n".join(out)


@tool(write=True)
def play_upload(package: str, file: str, track: str = "internal", release_notes: str = "",
                language: str = "en-US", rollout_percent: float = 100, draft: bool = False,
                release_name: str = "", confirm: bool = False) -> str:
    """Upload an .aab (or .apk) and release it on a track.

    Without confirm nothing is uploaded: it checks the file and shows the plan.
    Apps that were never published only accept draft releases (draft=true).

    Args:
        package: Android package name
        file: path to the .aab or .apk
        track: internal, alpha, beta, production or a custom track name
        release_notes: "What's new" text for `language` (max 500)
        language: language of the release notes
        rollout_percent: below 100 starts a staged rollout (production)
        draft: create the release as a draft instead of rolling it out
        release_name: optional name shown in Play Console (defaults to the version name)
        confirm: true to upload and commit
    """
    path = Path(file).expanduser()
    if not path.is_file():
        raise ConsoleError(f"File not found: {path}")
    kind = {".aab": "bundles", ".apk": "apks"}.get(path.suffix.lower())
    if not kind:
        raise ConsoleError("The file must be an .aab or .apk")
    release = {**_status(rollout_percent, draft), "releaseNotes": _release_notes(release_notes, language)}
    if release_name:
        release["name"] = release_name
    plan = (f"{path.name} ({path.stat().st_size / 1e6:.1f} MB) → {package} track '{track}' as "
            f"{release['status']}{' ' + str(rollout_percent) + '%' if release['status'] == 'inProgress' else ''}")
    if not confirm:
        return f"{DRY_RUN}.\nPlan: {plan}"
    with _Edit(package) as e:
        start = _session().post(
            f"{UPLOAD_API}/{package}/edits/{e.id}/{kind}", params={"uploadType": "resumable"},
            headers={"X-Upload-Content-Type": "application/octet-stream",
                     "X-Upload-Content-Length": str(path.stat().st_size)}, timeout=60)
        if start.status_code >= 400 or "Location" not in start.headers:
            raise ConsoleError(f"Upload could not start ({start.status_code}): {start.text[:300]}")
        with path.open("rb") as fh:
            done = _session().put(start.headers["Location"], data=fh, timeout=1800,
                                  headers={"Content-Type": "application/octet-stream"})
        if done.status_code >= 400:
            raise ConsoleError(f"Upload failed ({done.status_code}): {done.text[:300]}")
        code = str(done.json()["versionCode"])
        release["versionCodes"] = [code]
        e("PUT", f"tracks/{track}", json={"track": track, "releases": [release]})
        result = e.commit()
    return f"Uploaded version code {code}. {plan}\n{result}"


@tool(write=True)
def play_promote(package: str, from_track: str, to_track: str, rollout_percent: float = 100,
                 release_notes: str = "", language: str = "en-US", confirm: bool = False) -> str:
    """Promote the latest release of one track to another (e.g. internal → production), keeping its notes.

    Without confirm the change is built and validated by Google in a draft edit, then discarded.

    Args:
        package: Android package name
        from_track: source track, e.g. internal
        to_track: target track, e.g. production
        rollout_percent: below 100 starts a staged rollout
        release_notes: replace the release notes for `language` (empty keeps the source notes)
        language: language of release_notes
        confirm: true to commit
    """
    with _Edit(package) as e:
        src = e("GET", f"tracks/{from_track}").get("releases", [])
        src = [r for r in src if r.get("versionCodes") and r.get("status") in ("completed", "inProgress", "draft")]
        if not src:
            raise ConsoleError(f"Track '{from_track}' has no release to promote.")
        best = max(src, key=lambda r: max(int(c) for c in r["versionCodes"]))
        release = {**_status(rollout_percent, False), "versionCodes": best["versionCodes"]}
        notes = best.get("releaseNotes", [])
        if release_notes:
            notes = [n for n in notes if n["language"] != language] + _release_notes(release_notes, language)
        if notes:
            release["releaseNotes"] = notes
        if best.get("name"):
            release["name"] = best["name"]
        e("PUT", f"tracks/{to_track}", json={"track": to_track, "releases": [release]})
        e.validate()
        plan = f"{from_track} → {to_track}: {_fmt_release(release)}"
        if not confirm:
            return f"{DRY_RUN}. Google validated it.\n{plan}"
        return f"{e.commit()}\n{plan}"


@tool(write=True)
def play_rollout(package: str, track: str = "production", rollout_percent: float = 0, halt: bool = False,
                 resume: bool = False, confirm: bool = False) -> str:
    """Change a staged rollout: set a new percentage, finish it (100), halt it or resume a halted one.

    Args:
        package: Android package name
        track: track with the staged rollout, usually production
        rollout_percent: new percentage; 100 completes the rollout
        halt: stop the rollout for new users
        resume: resume a halted rollout at rollout_percent (or its previous percentage)
        confirm: true to commit
    """
    if sum([bool(rollout_percent), halt, resume and not rollout_percent]) != 1:
        raise ConsoleError("Give exactly one of: rollout_percent, halt, resume.")
    with _Edit(package) as e:
        t = e("GET", f"tracks/{track}")
        releases = t.get("releases", [])
        staged = [r for r in releases if r.get("status") in ("inProgress", "halted")]
        if not staged:
            raise ConsoleError(f"No staged rollout on '{track}'. Releases: "
                               + ("; ".join(_fmt_release(r) for r in releases) or "none"))
        r = staged[0]
        before = _fmt_release(r)
        if halt:
            r["status"] = "halted"
        else:
            pct = rollout_percent or r.get("userFraction", 0) * 100
            r.update(_status(pct, False))
            if r["status"] == "completed":
                r.pop("userFraction", None)
                # a finished rollout replaces the previous completed release; drafts can stay
                releases = [x for x in releases if x is r or x.get("status") == "draft"]
        e("PUT", f"tracks/{track}", json={"track": track, "releases": releases})
        e.validate()
        plan = f"{track}: {before}  →  {_fmt_release(r)}"
        if not confirm:
            return f"{DRY_RUN}. Google validated it.\n{plan}"
        return f"{e.commit()}\n{plan}"


# ---------------------------------------------------------------- reviews

def _ts(seconds) -> str:
    return datetime.fromtimestamp(int(seconds), timezone.utc).strftime("%Y-%m-%d") if seconds else "?"


@tool()
def play_reviews(package: str, limit: int = 20, unanswered_only: bool = False, translate_to: str = "") -> str:
    """Recent Google Play reviews with text (the API only returns about the last 7 days).

    Each line ends with the review id needed to reply.

    Args:
        package: Android package name
        limit: maximum number of reviews
        unanswered_only: hide reviews that already have a developer reply
        translate_to: optional language code to translate the reviews into, e.g. en
    """
    out, token = [], None
    while len(out) < limit:
        params = {"maxResults": 100, **({"token": token} if token else {}),
                  **({"translationLanguage": translate_to} if translate_to else {})}
        r = _api("GET", f"{package}/reviews", params=params)
        for rv in r.get("reviews", []):
            c = rv["comments"][0]["userComment"]
            replied = any("developerComment" in x for x in rv["comments"])
            if unanswered_only and replied:
                continue
            out.append(f"[{c.get('starRating')}★ {c.get('reviewerLanguage', '?')} v{c.get('appVersionName', '?')} "
                       f"{_ts(c.get('lastModified', {}).get('seconds'))}] {clip(c.get('text'), 400)} "
                       f"· id={rv['reviewId']}{' · replied' if replied else ''}")
        token = r.get("tokenPagination", {}).get("nextPageToken")
        if not token:
            break
    return "\n".join(out[:limit]) or f"{package}: no reviews with text in the last 7 days."


@tool(write=True)
def play_reply(package: str, review_id: str, text: str, confirm: bool = False) -> str:
    """Reply publicly to a Google Play review as the developer (max 350 characters; replaces an earlier reply).

    Args:
        package: Android package name
        review_id: id from play_reviews
        text: reply text
        confirm: true to publish the reply
    """
    if len(text) > REPLY_LIMIT:
        raise ConsoleError(f"Reply has {len(text)} characters (max {REPLY_LIMIT}).")
    if not confirm:
        return f"{DRY_RUN}. Would reply to {review_id} on {package}:\n{text}"
    _api("POST", f"{package}/reviews/{review_id}:reply", json={"replyText": text})
    return "Reply published."


# ---------------------------------------------------------------- vitals

def _dt(d: date) -> dict:
    return {"year": d.year, "month": d.month, "day": d.day, "timeZone": {"id": "America/Los_Angeles"}}


def _metric(row: dict, name: str) -> float:
    for m in row.get("metrics", []):
        if m["metric"] == name:
            return float(m.get("decimalValue", {}).get("value", 0) or 0)
    return 0.0


@tool()
def play_vitals(package: str, days: int = 28) -> str:
    """Android vitals: user-perceived crash and ANR rates (28-day, as Google judges them) and the worst versions.

    Google's bad-behavior thresholds: crashes 1.09 %, ANR 0.47 %.

    Args:
        package: Android package name
        days: window for the per-version breakdown
    """
    sess = _session(SCOPE_REPORTING)
    base = f"{REPORTING}/apps/{package}"
    out, end = [], None
    for kind, metric_set, metric in (("crash", "crashRateMetricSet", "userPerceivedCrashRate"),
                                     ("anr", "anrRateMetricSet", "userPerceivedAnrRate")):
        meta = call(sess, "GET", f"{base}/{metric_set}")
        fresh = next((f["latestEndTime"] for f in meta.get("freshnessInfo", {}).get("freshnesses", [])
                      if f.get("aggregationPeriod") == "DAILY"), None)
        if not fresh:
            out.append(f"{kind}: no data yet")
            continue
        end = date(fresh["year"], fresh["month"], fresh["day"])
        overall = call(sess, "POST", f"{base}/{metric_set}:query", json={
            "timelineSpec": {"aggregationPeriod": "DAILY", "startTime": _dt(end - timedelta(days=1)),
                             "endTime": _dt(end)},
            "metrics": [f"{metric}28dUserWeighted", "distinctUsers"]}).get("rows", [])
        rate = _metric(overall[-1], f"{metric}28dUserWeighted") * 100 if overall else 0
        flag = "ABOVE THRESHOLD" if rate > BAD_BEHAVIOR[kind] else "ok"
        rows = call(sess, "POST", f"{base}/{metric_set}:query", json={
            "timelineSpec": {"aggregationPeriod": "DAILY", "startTime": _dt(end - timedelta(days=days)),
                             "endTime": _dt(end)},
            "dimensions": ["versionCode"], "metrics": [metric, "distinctUsers"], "pageSize": 10000}).get("rows", [])
        acc: dict[str, list[float]] = {}
        for row in rows:
            d = row["dimensions"][0]
            vc = d.get("stringValue") or d.get("int64Value") or "?"
            users = _metric(row, "distinctUsers")
            a = acc.setdefault(vc, [0.0, 0.0])
            a[0] += _metric(row, metric) * users
            a[1] += users
        all_users = sum(u for _, u in acc.values()) or 1
        newest = sorted(acc.items(), key=lambda x: -int(x[0]) if x[0].isdigit() else 0)[:5]
        versions = " · ".join(f"v{vc} {w / u * 100:.2f}% ({u / all_users * 100:.0f}% of users)"
                              for vc, (w, u) in newest if u)
        out.append(f"{kind}: {rate:.2f}% 28-day user-perceived ({flag}, threshold {BAD_BEHAVIOR[kind]}%)"
                   + (f"\n  newest versions (last {days} d): {versions}" if versions else ""))
    return f"{package} (data up to {end or '?'}):\n" + "\n".join(out)


# ---------------------------------------------------------------- products

def _money(p: dict) -> str:
    if not p:
        return "?"
    return f"{int(p.get('units', 0) or 0) + int(p.get('nanos', 0) or 0) / 1e9:g} {p.get('currencyCode', '')}"


@tool()
def play_products(package: str, region: str = "US") -> str:
    """Subscriptions (with base plans and price in one region) and one-time in-app products.

    Args:
        package: Android package name
        region: region code for prices, e.g. US, MX
    """
    out = []
    subs = _api("GET", f"{package}/subscriptions", params={"pageSize": 200}).get("subscriptions", [])
    for s in subs:
        title = next((x.get("title") for x in s.get("listings", [])), "")
        out.append(f"sub {s['productId']} «{title}»")
        for bp in s.get("basePlans", []):
            period = (bp.get("autoRenewingBasePlanType") or bp.get("prepaidBasePlanType") or {}).get(
                "billingPeriodDuration", "")
            price = next((_money(rc.get("price")) for rc in bp.get("regionalConfigs", [])
                          if rc.get("regionCode") == region), "not offered in " + region)
            out.append(f"   · {bp['basePlanId']} {period} {bp.get('state')} {price}")
    try:
        one = _api("GET", f"{package}/oneTimeProducts", params={"pageSize": 200}).get("oneTimeProducts", [])
        for p in one:
            title = next((x.get("title") for x in p.get("listings", [])), "")
            out.append(f"one-time {p['productId']} «{title}» ({len(p.get('purchaseOptions', []))} purchase options)")
    except ConsoleError:
        one = _api("GET", f"{package}/inappproducts").get("inappproduct", [])
        for p in one:
            price = p.get("defaultPrice", {})
            out.append(f"one-time {p['sku']} {p.get('status')} "
                       f"{int(price.get('priceMicros', 0)) / 1e6:g} {price.get('currency', '')}")
    return "\n".join(out) or f"{package} has no subscriptions or in-app products."


# ---------------------------------------------------------------- store listing graphics

@tool()
def play_images(package: str, language: str = "en-US") -> str:
    """How many images each graphic type has in one listing language (screenshots, icon, feature graphic, TV banner).

    Args:
        package: Android package name
        language: listing language, e.g. en-US
    """
    with _Edit(package) as e:
        counts = {t: len(e("GET", f"listings/{language}/{t}").get("images", [])) for t in IMAGE_TYPES}
    return f"{package} {language}: " + ", ".join(f"{t} {n}" for t, n in counts.items())


@tool(write=True)
def play_upload_images(package: str, language: str, image_type: str, files: list[str], replace: bool = False,
                       confirm: bool = False) -> str:
    """Upload screenshots or graphics to one listing language, in the order given.

    Without confirm the images go into a draft edit that Google validates and then discards.

    Args:
        package: Android package name
        language: listing language, e.g. en-US
        image_type: phoneScreenshots, sevenInchScreenshots, tenInchScreenshots, tvScreenshots, wearScreenshots,
            icon, featureGraphic or tvBanner
        files: PNG or JPEG files (one for icon, featureGraphic and tvBanner; up to 8 screenshots per type)
        replace: delete the current screenshots of that type first (otherwise new ones go after them)
        confirm: true to save and send to review
    """
    if image_type not in IMAGE_TYPES:
        raise ConsoleError(f"image_type must be one of {', '.join(IMAGE_TYPES)}.")
    paths = [Path(f).expanduser() for f in files]
    missing = [str(x) for x in paths if not x.is_file()]
    if missing:
        raise ConsoleError(f"Not found: {', '.join(missing)}")
    wrong = [x.name for x in paths if x.suffix.lower() not in IMAGE_MIME]
    if wrong:
        raise ConsoleError(f"Only PNG or JPEG: {', '.join(wrong)}")
    if image_type in SINGLE_IMAGES and len(paths) != 1:
        raise ConsoleError(f"{image_type} takes exactly one image.")
    with _Edit(package) as e:
        listing = f"listings/{language}/{image_type}"
        before = len(e("GET", listing).get("images", []))
        if replace and before and image_type not in SINGLE_IMAGES:
            e("DELETE", listing)
        for x in paths:
            r = _session().post(f"{UPLOAD_API}/{package}/edits/{e.id}/{listing}", params={"uploadType": "media"},
                                data=x.read_bytes(), headers={"Content-Type": IMAGE_MIME[x.suffix.lower()]}, timeout=300)
            if r.status_code >= 400:
                raise ConsoleError(f"{x.name} was refused ({r.status_code}): {api_error(r)}")
        after = len(e("GET", listing).get("images", []))
        e.validate()
        plan = f"{package} {language} {image_type}: {before} → {after} images ({', '.join(x.name for x in paths)})"
        if not confirm:
            return f"{DRY_RUN}. Google accepted the images.\n{plan}"
        return f"{e.commit()}\n{plan}"


# ---------------------------------------------------------------- users and permissions

USERS_HINT = ("The service account can't manage users: in Play Console > Users and permissions give it "
              "\"Manage users and permissions\" (once, for the account or for the apps it should manage).")


def _users_call(method: str, path: str = "", **kw):
    dev = setting("GOOGLE_PLAY_DEVELOPER_ID", "It is the number after /developers/ in any Play Console address.")
    try:
        return call(_session(), method, f"{DEVELOPERS}/{dev}/users{path}", **kw)
    except ConsoleError as err:
        if "HTTP 403" in str(err):
            raise ConsoleError(f"{err}\n{USERS_HINT}") from err
        raise


def _users() -> list[dict]:
    return _users_call("GET", params={"pageSize": -1}).get("users", [])  # the API doesn't paginate users


def _short(perms: list[str]) -> str:
    return ",".join(x.removeprefix("CAN_").removesuffix("_GLOBAL").lower() for x in perms) or "-"


def _permissions(spec: str) -> list[str]:
    """'deploy' or 'read,reply_to_reviews' or 'CAN_MANAGE_ORDERS' → the API's app-level permission names."""
    out: list[str] = []
    for item in (x.strip() for x in spec.split(",") if x.strip()):
        upper = item.upper()
        names = PERMISSION_PRESETS.get(item.lower()) or [upper if upper.startswith("CAN_") else "CAN_" + upper]
        bad = [n for n in names if n not in APP_PERMISSIONS]
        if bad:
            raise ConsoleError(f"Unknown permission {item!r}. Presets: {', '.join(PERMISSION_PRESETS)}; or any of "
                               f"{_short(APP_PERMISSIONS)}")
        out += [n for n in names if n not in out]
    if not out:
        raise ConsoleError("Give permissions: a preset (read, deploy, full) or names, comma-separated.")
    return out


@tool()
def play_users(package: str = "") -> str:
    """Users and service accounts of the Play developer account, with account-wide permissions and per-app grants.

    Needs GOOGLE_PLAY_DEVELOPER_ID and a key with "Manage users and permissions".

    Args:
        package: only people with access to this app
    """
    out = []
    for u in _users():
        grants = {g["packageName"]: g.get("appLevelPermissions", []) for g in u.get("grants", [])}
        account = u.get("developerAccountPermissions", [])
        if package and package not in grants and not account:
            continue
        apps = (f"{package}: {_short(grants[package])}" if package in grants else
                f"{len(grants)} apps" if not package else "no app grant")
        out.append(f"{u['email']} [{u.get('accessState', '?').lower()}] account: {_short(account)} · {apps}")
    return "\n".join(out) or "No users match."


@tool(write=True)
def play_grant(email: str, package: str, permissions: str = "deploy", confirm: bool = False) -> str:
    """Give a person or service account permissions on one app, inviting them to the account first if needed.

    Args:
        email: their Google account or service account email
        package: Android package name
        permissions: preset read, deploy (releases on every track, listing, testers) or full, or permission names
            comma-separated (e.g. reply_to_reviews,view_financial_data)
        confirm: true to save
    """
    perms = _permissions(permissions)
    user = next((u for u in _users() if u["email"].lower() == email.lower()), None)
    who = quote(email, safe="@")
    if not user:
        body = {"email": email, "grants": [{"packageName": package, "appLevelPermissions": perms}]}
        plan = f"Invite {email} with {package}: {_short(perms)}"
        if not confirm:
            return f"{DRY_RUN}.\n{plan}"
        _users_call("POST", json=body)
        return f"Invited. {plan} (a person has to accept the email invitation; a service account works right away)."
    current = next((g for g in user.get("grants", []) if g["packageName"] == package), None)
    plan = f"{email} on {package}: {_short(current['appLevelPermissions']) if current else 'none'} → {_short(perms)}"
    if not confirm:
        return f"{DRY_RUN}.\n{plan}"
    if current:
        _users_call("PATCH", f"/{who}/grants/{package}", params={"updateMask": "appLevelPermissions"},
                    json={"appLevelPermissions": perms})
    else:
        _users_call("POST", f"/{who}/grants", json={"packageName": package, "appLevelPermissions": perms})
    return f"Saved. {plan}"


@tool(write=True)
def play_revoke(email: str, package: str = "", confirm: bool = False) -> str:
    """Remove someone's access to one app, or (without package) remove them from the developer account.

    Args:
        email: their email
        package: the app to remove them from; empty removes them from the whole account
        confirm: true to remove
    """
    user = next((u for u in _users() if u["email"].lower() == email.lower()), None)
    if not user:
        raise ConsoleError(f"{email} isn't in the developer account.")
    who = quote(email, safe="@")
    if package and not any(g["packageName"] == package for g in user.get("grants", [])):
        raise ConsoleError(f"{email} has no grant on {package}.")
    plan = f"Remove {email} from {package}" if package else f"Remove {email} from the developer account (every app)"
    if not confirm:
        return f"{DRY_RUN}.\n{plan}"
    _users_call("DELETE", f"/{who}/grants/{package}" if package else f"/{who}")
    return f"Done. {plan}"


# ---------------------------------------------------------------- data safety

@tool(write=True)
def play_data_safety(package: str, csv_file: str, confirm: bool = False) -> str:
    """Replace an app's Data safety answers with a CSV in Play Console's format.

    Get the CSV once from Play Console (App content > Data safety > Export to CSV), edit it, and reuse it for similar
    apps. Google offers no way to read the current answers back through the API.

    Args:
        package: Android package name
        csv_file: path to the CSV
        confirm: true to send it
    """
    path = Path(csv_file).expanduser()
    if not path.is_file():
        raise ConsoleError(f"File not found: {path}")
    text = path.read_text(encoding="utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2 or not any("Question ID" in cell for cell in rows[0]):
        raise ConsoleError("This doesn't look like Play Console's Data safety export (the first row should have "
                           "'Question ID (machine readable)').")
    answered = sum(1 for r in rows[1:] if len(r) > 2 and r[2].strip())
    plan = f"{package}: Data safety from {path.name} ({len(rows) - 1} rows, {answered} answered)"
    if not confirm:
        return f"{DRY_RUN}. Google checks the answers only when they are sent.\n{plan}"
    _api("POST", f"{package}/dataSafety", json={"safetyLabels": text})
    return f"Saved. {plan}"


# ---------------------------------------------------------------- public search

@tool()
def play_search_rank(package: str, keywords: list[str], country: str = "us", language: str = "en") -> str:
    """Position of an app in public Google Play search (top 30) for several keywords. No credentials needed.

    Args:
        package: Android package name
        keywords: search terms to check
        country: two-letter country code
        language: two-letter language code
    """
    from google_play_scraper import app as gp_app
    from google_play_scraper import search

    try:
        title = gp_app(package, lang=language, country=country)["title"]
    except Exception as err:  # the scraper raises NotFoundError and others
        raise ConsoleError(f"{package} is not public on Google Play {country.upper()}: {err}") from err
    out = [f"{title} ({package}) in {country.upper()}:"]
    for q in keywords:
        try:
            results = search(q, lang=language, country=country, n_hits=30)
        except Exception as err:
            out.append(f"  «{q}»: error {err}")
            continue
        pos = next((i + 1 for i, x in enumerate(results)
                    if x.get("appId") == package or (x.get("title") or "") == title), None)
        leaders = ", ".join(clip(x.get("title"), 24) for x in results[:3])
        out.append(f"  «{q}»: {pos or 'not in top 30'} · top: {leaders}")
    return "\n".join(out)
