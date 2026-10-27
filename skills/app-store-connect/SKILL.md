---
name: app-store-connect
description: Work with App Store Connect from Claude without a browser — app versions and review state, listing texts (name, subtitle, keywords, description, promotional text, what's new), creating a version, attaching a build and submitting to App Review, releasing an approved version, builds/TestFlight state, customer reviews and responses, daily sales and App Store search rank. Use when the user mentions App Store Connect, iOS release, App Review, TestFlight build, keywords field, promotional text, App Store reviews or iOS downloads.
---

# App Store Connect

Use the `app-store-connect` MCP tools (`appstore_*`). If they are not loaded, run the same thing with the CLI:
`appconsole appstore <command> ...` (on PATH when this plugin is enabled), or
`uvx --from git+https://github.com/Alekla0126/app-console-mcp appconsole appstore <command> ...`.
`app` accepts the numeric Apple ID, the bundle id or part of the name. Do not open the browser for anything
these tools cover.

## Rules

1. Tools that change something (`appstore_update_listing`, `appstore_create_version`, `appstore_submit`,
   `appstore_release`, `appstore_reply`) are a **dry run** unless `confirm=true`. Run the dry run, show the user
   the diff or plan, and call with `confirm=true` only after the user approves that exact change.
2. Submitting to review and releasing are visible to Apple and to users: always ask first.
3. Before writing a promise in the listing or what's new, check the feature exists in the app's code.
4. Never print or commit the .p8 key.

## Tools

| Need | Tool (CLI) |
|---|---|
| List apps | `appstore_apps` (`apps`) |
| Versions and their state | `appstore_versions app` (`versions`) |
| Read a listing | `appstore_listing app locale` (`listing`) |
| Change listing texts | `appstore_update_listing` (`update-listing`) — name 30, subtitle 30, keywords 100, promo 170, description/what's new 4000 |
| New version | `appstore_create_version app 1.2.0` (`create-version`) |
| Builds and processing state | `appstore_builds app` (`builds`) |
| Attach build + submit to review | `appstore_submit app [build]` (`submit`) |
| Release an approved manual-release version | `appstore_release app` (`release`) |
| Reviews / respond | `appstore_reviews app` (`reviews`), `appstore_reply review_id text` (`reply`) |
| Downloads and proceeds | `appstore_sales days` (`sales`, needs ASC_VENDOR_NUMBER) |
| Public search position | `appstore_search_rank app_id keywords...` (`search-rank`) |

## Workflows

**Ship an iOS update** (the binary is uploaded by Xcode, fastlane or `xcrun altool`; these tools take it from there)
1. `appstore_versions` — is there an editable version? If not, `appstore_create_version` (dry run → confirm).
2. `appstore_update_listing whats_new=...` for each locale (dry run shows the diff).
3. `appstore_builds` until the build is `VALID`.
4. `appstore_submit` dry run: it reports the chosen build and warnings (export compliance unset, empty what's
   new). Fix them, then `confirm=true`.
5. With manual release, after approval: `appstore_release`.

**Keywords / ASO pass**: `appstore_listing`, then `appstore_search_rank` for candidate keywords, propose a
new 100-character keyword field (comma-separated, no spaces after commas, no words already in name/subtitle),
then `appstore_update_listing keywords=...`. Promotional text can change any time without review.

**Reviews**: `appstore_reviews unanswered_only=true rating=1` (and 2), draft short helpful responses, get approval,
`appstore_reply`.

## What the API cannot do here (needs App Store Connect in the browser)

Creating the app record, App Privacy labels, age rating questionnaire, screenshots and previews (not in this
version), pricing and in-app purchase setup, and answering App Review messages in the Resolution Center.

## Setup problems

`appconsole doctor` checks every service. Missing key: set `ASC_KEY_ID`, `ASC_ISSUER_ID` and
`ASC_PRIVATE_KEY_PATH` (or `ASC_API_KEY_JSON` for a fastlane api_key.json) with `appconsole config set`.
401 means a wrong key id / issuer id pair or a revoked key; 403 means the key's role is too low (App Manager for
releases, Sales/Finance for `appstore_sales`).
