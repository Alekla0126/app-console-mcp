---
name: admob
description: Work with AdMob from Claude without a browser — earnings, eCPM, match, show rate and CTR by app, ad unit, country, format, app version, SDK version or why ads are limited, compared with the previous period; mediation reports by ad source and mediation group; app promotion campaign reports (installs, cost, CPI); the list of apps, ad units, ad sources and adapters; and, where Google enabled the account, creating apps and ad units, ad unit mappings, mediation groups and mediation A/B experiments. Use when the user asks about AdMob, ad revenue, eCPM, fill or match rate, mediation, waterfall, bidding, ad unit ids, or a new app's ads.
---

# AdMob

Use the `admob` MCP tools (`admob_*`). If they are not loaded, run the same thing with the CLI:
`appconsole admob <command> ...` (on PATH when this plugin is enabled), or
`uvx --from git+https://github.com/Alekla0126/app-console-mcp appconsole admob <command> ...`.
Do not open the AdMob website for anything these tools cover.

## Rules

1. Changes (`admob_create_app`, `admob_create_ad_unit`, `admob_map_ad_unit`, `admob_create_mediation_group`,
   `admob_update_mediation_group`, `admob_ab_experiment`) are a **dry run** unless `confirm=true`. Show the user
   the body the dry run prints and call with `confirm=true` only after they approve that exact change. AdMob has
   no server-side validation, so the dry run checks only what can be checked locally.
2. Mediation groups are created disabled unless `enable=true`; say so.
3. Never invent ad source, adapter or mapping ids: read them with `admob_ad_sources` and `admob_ad_unit_mappings`.

## Tools

| Need | Tool (CLI) | Access |
|---|---|---|
| Accounts, currency, time zone | `admob_accounts` (`accounts`) | open |
| Apps and their ad unit ids | `admob_apps search` (`apps --search`) | open |
| Earnings report | `admob_report days by currency` (`report --days 28 --by app,format --currency USD`) | open |
| Campaign report (installs, cost, CPI) | `admob_campaign_report` (`campaign-report`) | open |
| Ad sources and adapter settings | `admob_ad_sources search adapters` (`ad-sources --search meta --adapters`) | open |
| Mediation groups and their lines | `admob_mediation_groups` (`mediation-groups`) | limited |
| An ad unit's mappings | `admob_ad_unit_mappings` (`ad-unit-mappings`) | limited |
| Create app / ad unit | `admob_create_app`, `admob_create_ad_unit` | limited |
| Map an ad unit to a network | `admob_map_ad_unit ad_unit adapter id=value...` | limited |
| Create / change a mediation group | `admob_create_mediation_group`, `admob_update_mediation_group` | limited |
| Mediation A/B test | `admob_ab_experiment group start|stop` | limited |

`by` in `admob_report` accepts `app, ad_unit, country, format, platform, date, week, month, ad_type, app_version,
sdk_version, os_version, serving_restriction`, and the mediation groupings `ad_source, ad_source_instance,
mediation_group` (they switch to the mediation report). Days end yesterday in the account's reporting time zone.

**Limited** means Google must enable the publisher for the AdMob API's limited-access methods, and the token must
have the `admob.monetization` scope (`appconsole admob login --monetization --client-secrets <file>`). Until
then those tools answer with what to ask Google; tell the user instead of retrying.

## Workflows

**"How much did ads make?"**: `admob_report days=7 currency="USD"`, then `by="format"` or `by="country"` to explain
it. Give the total, the trend and the one or two rows that explain it.

**Diagnose low revenue**: low match rate = little demand; low show rate = ads loaded but not shown (requested too
early or too often); `by="serving_restriction"` shows limited ads; `by="app_version"` or `by="sdk_version"`
finds a bad release. A CTR far above a few percent usually means accidental clicks, a policy risk: point to the
ad placement.

**New app's ads** (limited): `admob_create_app` (linked with `app_store_id`) → `admob_create_ad_unit` per placement
→ give the user the app id and ad unit ids for the code. For mediation: `admob_ad_sources search=... adapters=true`
→ `admob_map_ad_unit` per ad unit and network → `admob_create_mediation_group` with lines like
`5450213213286189855@live,<source>@1.20` → enable it once ads are verified.

**Test a waterfall change** (limited): `admob_ab_experiment group action=start treatment_lines=... traffic_percent=50`,
compare with `admob_report by="mediation_group,ad_source"`, then `action=stop winner=a|b`.

## What has no API at all (needs the AdMob website)

Privacy & messaging (GDPR, US states, IDFA messages), blocking controls, the policy center, payments, app-ads.txt
status and user management.

## Setup problems

AdMob does not accept service accounts. Run `appconsole admob login --client-secrets <oauth-desktop-client.json>`
once (add `--monetization` for changes), or use gcloud application-default credentials with the AdMob scopes (see
the README). With several publisher accounts set `ADMOB_PUBLISHER_ID`. `appconsole doctor` checks every service.
