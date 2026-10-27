---
name: admob
description: Read AdMob from Claude without a browser — earnings, impressions, eCPM, match and show rate by app, ad unit, country, format, platform, date or ad source, compared with the previous period; and the list of apps and ad unit ids. Use when the user asks about AdMob, ad revenue, eCPM, fill or match rate, which app or format earns most, or wants to check ad unit ids before a release.
---

# AdMob

Use the `admob` MCP tools (`admob_*`). If they are not loaded, run the same thing with the CLI:
`appconsole admob <command> ...` (on PATH when this plugin is enabled), or
`uvx --from git+https://github.com/Alekla0126/app-console-mcp appconsole admob <command> ...`.
All AdMob tools are read-only. Do not open the AdMob website for anything these tools cover.

## Tools

| Need | Tool (CLI) |
|---|---|
| Accounts, currency, time zone | `admob_accounts` (`accounts`) |
| Apps and their ad unit ids | `admob_apps search` (`apps --search`) |
| Earnings report | `admob_report days by currency` (`report --days 28 --by app,format --currency USD`) |

`by` accepts a comma-separated mix of `app, ad_unit, country, format, platform, date, month, ad_source`
(`ad_source` switches to the mediation report). Days end yesterday in the account's reporting time zone; use
`end_date` for another window. The header line always has the total and the change against the previous period
of the same length.

## Workflows

**"How much did ads make?"**: `admob_report days=7 currency="USD"` then, if asked why, `by="format"` or
`by="country"`. Report the total, the trend and the one or two rows that explain it.

**Before a release**: `admob_apps search="<app name>"` and compare the ad unit ids with the ones in the app's
code; flag any id that does not exist or belongs to another app/platform. Apps in `ACTION_REQUIRED` or not linked
to a store limit ad serving.

**Diagnose low revenue**: low match rate means little demand or bad targeting; low show rate means ads are
loaded but not shown (the app requests too early or too often); compare eCPM by country and format.

## What the API cannot do (needs the AdMob website)

Creating ad units or mediation groups (Google keeps that API in closed beta), privacy & messaging (GDPR/UMP),
app-ads.txt, payments, and linking an app to its store listing.

## Setup problems

AdMob does not accept service accounts. Run `appconsole admob login --client-secrets <oauth-desktop-client.json>`
once (it opens the browser for consent a single time), or use gcloud application-default credentials with the
AdMob scopes (see the README). With several publisher accounts set `ADMOB_PUBLISHER_ID`.
`appconsole doctor` checks every service.
