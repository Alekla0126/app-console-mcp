---
name: google-play
description: Work with Google Play Console from Claude without a browser — store listing texts, uploading an .aab, promoting between tracks, staged rollouts, reviews and replies, Android vitals (crash/ANR), screenshots and graphics, users and app permissions (invite a service account), the Data safety form, subscriptions and public search rank. Use when the user mentions Google Play, Play Console, Android release, AAB upload, rollout, track, Play reviews, ANR or crash rate, Play screenshots, Play Console users or permissions, data safety, or "publish to Play".
---

# Google Play Console

Use the `google-play` MCP tools (`play_*`). If they are not loaded, run the same thing with the CLI through Bash:
`appconsole play <command> ...` (on PATH when this plugin is enabled), or
`uvx --from git+https://github.com/Alekla0126/app-console-mcp appconsole play <command> ...`.
`appconsole play <command> --help` shows every option. Outputs are short text on purpose; do not open the browser
for anything these tools cover.

## Rules

1. Tools that change something (`play_update_listing`, `play_upload`, `play_promote`, `play_rollout`,
   `play_reply`, `play_upload_images`, `play_grant`, `play_revoke`, `play_data_safety`) are a **dry run** unless
   `confirm=true`. Always run the dry run first, show the user the plan or diff it returns, and only call again
   with `confirm=true` after the user approves that exact change.
2. Replies to reviews are public and signed as the developer. Draft them, show them, wait for approval.
3. Before writing a promise in a listing or release notes, check the feature exists in the app's code.
4. Never print or commit the service account key.

## Tools

| Need | Tool (CLI) |
|---|---|
| Which apps the key can see | `play_apps` (`apps`) |
| Read a listing | `play_listing package language` (`listing`) |
| Change title / short / full description | `play_update_listing` (`update-listing`) — limits 30 / 80 / 4000 |
| What is on each track | `play_tracks` (`tracks`) |
| Upload .aab/.apk and release | `play_upload package file track` (`upload`) |
| Move a release between tracks | `play_promote package from_track to_track` (`promote`) |
| Change / finish / halt a staged rollout | `play_rollout` (`rollout`) |
| Reviews of the last 7 days | `play_reviews` (`reviews`), reply with `play_reply` (max 350 chars) |
| Crash and ANR rates | `play_vitals` (`vitals`) — bad behavior above 1.09 % crashes or 0.47 % ANR |
| Subscriptions and products | `play_products` (`products`) |
| Public search position | `play_search_rank package keywords...` (`search-rank`) |
| Graphics per listing language | `play_images` (`images`), upload with `play_upload_images` (`upload-images`) |
| Users and service accounts | `play_users` (`users`); give app permissions with `play_grant` (invites if new), remove with `play_revoke` |
| Data safety form | `play_data_safety package csv_file` (`data-safety`) with Play Console's CSV export |

## Workflows

**Ship an Android release**
1. `play_tracks` to see what is live.
2. `play_upload` with `track="internal"` (dry run → confirm). Never-published apps need `draft=true`.
3. When the user is happy, `play_promote from_track="internal" to_track="production" rollout_percent=10`
   (dry run validates with Google → confirm).
4. Later `play_rollout rollout_percent=50`, then `rollout_percent=100`. If vitals worsen, `play_rollout halt=true`.

**Weekly health check**: `play_vitals` and `play_reviews unanswered_only=true` for each app, then report what
needs attention (threshold breaches, 1–2★ reviews) in a few lines.

**New app's service account**: `play_grant email=<sa>@<project>.iam.gserviceaccount.com package=<pkg>
permissions=deploy` (dry run → confirm). The key that calls it needs "Manage users and permissions" once;
`play_users` needs `GOOGLE_PLAY_DEVELOPER_ID` (the number after /developers/ in Play Console addresses).

**Same data safety on similar apps**: export one app's CSV from Play Console once, then `play_data_safety` for each
app that collects the same data.

**Localize a listing**: `play_listing` in en-US, translate within the limits, `play_update_listing` per language
(dry run shows the diff and Google's validation).

## What the API cannot do (tell the user it needs Play Console)

- "Publish" changes when managed publishing is on, or "Send for review" when Google requires it by hand
  (the tools say so when it happens).
- Creating a new app, content rating, ads and advertising ID declarations, target audience, app access, custom
  store listings, store listing experiments, and replying to reviews older than about a week.
- Data safety can be sent (CSV) but not read back.

## Setup problems

`appconsole doctor` checks every service. Missing key: `appconsole config set GOOGLE_PLAY_SERVICE_ACCOUNT /path/key.json`.
403 errors mean the service account lacks that permission in Play Console > Users and permissions, or the
Google Play Android Developer API / Play Developer Reporting API is not enabled in its Cloud project.
