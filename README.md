# app-console-mcp

**Google Play Console, App Store Connect and AdMob from your terminal or from Claude — no browser.**

[Español](README.es.md)

Driving store consoles through a browser agent burns tokens on screenshots and page snapshots and breaks
whenever the UI changes. This project talks to the official APIs instead and answers in a few lines of plain
text. Every tool exists twice, with identical output:

- as an **MCP tool** for Claude Code, Claude Desktop or any MCP client, and
- as a **CLI command** (`appconsole ...`) for you, your scripts and CI.

It ships as three Claude Code plugins (Google Play, App Store Connect, AdMob), each with its MCP server and a
skill that teaches Claude the safe workflows.

```text
$ appconsole play vitals com.example.app
com.example.app (data up to 2026-10-19):
crash: 0.31% 28-day user-perceived (ok, threshold 1.09%)
  newest versions (last 28 d): v42 0.12% (35% of users) · v41 0.44% (60% of users)
anr: 0.52% 28-day user-perceived (ABOVE THRESHOLD, threshold 0.47%)
  newest versions (last 28 d): v42 0.21% (35% of users) · v41 0.70% (60% of users)

$ appconsole admob report --days 7 --by format --currency USD
AdMob 2026-10-20 to 2026-10-26 by format: 128.40 USD (+9% vs previous 7 d: 117.80), 61210 impressions
  interstitial: 71.10 USD, 9340 impr, eCPM 7.61, match 88%, show 31%
  banner: 39.75 USD, 47880 impr, eCPM 0.83, match 95%, show 52%
```

## What it can do

| Google Play (`play`) | App Store Connect (`appstore`) | AdMob (`admob`) |
|---|---|---|
| `apps` list apps the key can see | `apps` list apps | `accounts` publisher accounts |
| `listing` / `update-listing` title, short, full description, new languages | `listing` / `update-listing` name, subtitle, keywords, description, promo text, what's new | `apps` apps and ad unit ids |
| `tracks` releases per track | `versions` versions and review state | `report` earnings, eCPM, match/show rate by app, ad unit, country, format, platform, date, month or ad source, vs. previous period |
| `upload` .aab/.apk to any track (staged or draft) | `create-version` new editable version | |
| `promote` between tracks | `builds` processing state, export compliance | |
| `rollout` change %, complete, halt, resume | `submit` attach build + submit to App Review | |
| `reviews` / `reply` | `release` release an approved manual-release version | |
| `vitals` crash and ANR rates (Google's 28-day numbers) | `reviews` / `reply` | |
| `products` subscriptions, base plans, prices | `sales` downloads, IAP units, proceeds per app | |
| `search-rank` public search position | `search-rank` public search position | |

**Safe by default.** Every tool that changes something is a dry run unless you pass `confirm=true`
(`--confirm` in the CLI). Google Play dry runs build the change in a draft edit and let Google validate it
before discarding it, so you see real errors before anything is published. MCP tools carry
`readOnlyHint`/`destructiveHint` annotations so clients can auto-approve reads and ask before writes.

## Install

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) (it fetches Python and the dependencies).

### Claude Code plugins (recommended)

```text
/plugin marketplace add Alekla0126/app-console-mcp
/plugin install google-play@app-console
/plugin install app-store-connect@app-console
/plugin install admob@app-console
```

Install only the ones you use. Each plugin asks for its credentials (paths and ids; you can leave them empty and
use `appconsole config` instead), adds its MCP server and skill, and puts `appconsole` on the PATH of Claude's
Bash tool.

### Any MCP client (Claude Desktop, Cursor, Codex, VS Code…)

```json
{
  "mcpServers": {
    "google-play": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Alekla0126/app-console-mcp", "appconsole", "mcp", "play"],
      "env": { "GOOGLE_PLAY_SERVICE_ACCOUNT": "/path/to/service-account.json" }
    },
    "app-store-connect": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Alekla0126/app-console-mcp", "appconsole", "mcp", "appstore"]
    },
    "admob": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Alekla0126/app-console-mcp", "appconsole", "mcp", "admob"]
    }
  }
}
```

Or with the Claude Code CLI:
`claude mcp add google-play -- uvx --from git+https://github.com/Alekla0126/app-console-mcp appconsole mcp play`

### CLI only

```bash
uv tool install git+https://github.com/Alekla0126/app-console-mcp
appconsole --help
appconsole play reviews com.example.app --unanswered-only
```

## Credentials

Settings are read from environment variables first, then from `~/.config/appconsole/config.json`
(`appconsole config set KEY VALUE`, stored with `600` permissions). Only file paths and ids are stored; keys stay
where you keep them. Run `appconsole doctor` to check all three services.

### Google Play setup

1. In a Google Cloud project enable the **Google Play Android Developer API** and the
   **Google Play Developer Reporting API**.
2. Create a service account (IAM & Admin > Service accounts) and a JSON key for it.
3. In Play Console > **Users and permissions** > Invite new users, add the service account's email and grant
   what you need: *View app information* (read and vitals), *Manage store presence* (listings), *Release apps to
   testing tracks* / *Release to production* (uploads and rollouts), *Reply to reviews*.
4. `appconsole config set GOOGLE_PLAY_SERVICE_ACCOUNT /path/to/key.json`

### App Store Connect setup

1. App Store Connect > **Users and Access** > Integrations > **App Store Connect API** > Team Keys > generate a
   key. Role *App Manager* covers everything here except sales, which needs *Sales* or *Finance*.
2. Download `AuthKey_<KEY_ID>.p8` (Apple lets you download it only once) and note the Key ID and the Issuer ID.
3. Configure:

```bash
appconsole config set ASC_KEY_ID ABC123XYZ
appconsole config set ASC_ISSUER_ID 00000000-0000-0000-0000-000000000000
appconsole config set ASC_PRIVATE_KEY_PATH ~/.appstoreconnect/private_keys/AuthKey_ABC123XYZ.p8
appconsole config set ASC_VENDOR_NUMBER 12345678   # optional, for `appstore sales`
```

Already use fastlane? Point `ASC_API_KEY_JSON` to your `api_key.json` instead. If the `.p8` file sits in
`~/.appstoreconnect/private_keys` or `~/private_keys`, the path is optional.

### AdMob setup

AdMob does not accept service accounts, only a Google user, so you authorize once:

1. In a Google Cloud project enable the **AdMob API**.
2. Configure the OAuth consent screen (External; add yourself as a test user) and set its publishing status to
   **In production** — refresh tokens of apps left in *Testing* expire after 7 days. For your own use you will see
   an "unverified app" warning; that is expected.
3. Create an OAuth client ID of type **Desktop app** and download its JSON.
4. `appconsole admob login --client-secrets client_secret.json` (opens the browser once; the token is saved in
   `~/.config/appconsole/admob-token.json`).

Alternative: gcloud application-default credentials with the AdMob scopes —
`gcloud auth application-default login --scopes=https://www.googleapis.com/auth/admob.readonly,https://www.googleapis.com/auth/admob.report,https://www.googleapis.com/auth/cloud-platform`.
With several publisher accounts set `ADMOB_PUBLISHER_ID=pub-...`.

### All settings

| Setting | Used by |
|---|---|
| `GOOGLE_PLAY_SERVICE_ACCOUNT` | Google Play (falls back to `GOOGLE_APPLICATION_CREDENTIALS`) |
| `ASC_KEY_ID`, `ASC_ISSUER_ID`, `ASC_PRIVATE_KEY_PATH` or `ASC_PRIVATE_KEY` | App Store Connect |
| `ASC_API_KEY_JSON` | App Store Connect, fastlane-format key file instead of the three above |
| `ASC_VENDOR_NUMBER` | `appstore sales` |
| `ADMOB_PUBLISHER_ID`, `ADMOB_TOKEN_FILE` | AdMob, optional |
| `APPCONSOLE_HOME` | config directory, default `~/.config/appconsole` |
| `APPCONSOLE_DEBUG=1` | show full tracebacks in the CLI |

## What the official APIs cannot do

These still need the website, and the tools say so when you hit them:

- **Google Play**: the *Publish* button when managed publishing is on (and *Send for review* when Google demands
  it by hand), creating apps, data safety / content rating / ads declarations, custom store listings and listing
  experiments, replying to reviews older than about a week.
- **App Store Connect**: creating the app record, App Privacy labels, age rating, pricing and in-app purchase
  setup, Resolution Center messages. Screenshots are possible through the API but not implemented yet.
- **AdMob**: creating ad units and mediation groups (closed beta API), privacy & messaging, app-ads.txt, payments.

## Development

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/pytest && .venv/bin/ruff check src tests
claude plugin validate .
```

`bin/appconsole` and the plugins run the package with `uvx --from <repo>`, which caches the build; after editing
the sources use the `.venv` or add `--refresh-package app-console-mcp`. For a release, bump the version in
`pyproject.toml`, `src/app_console_mcp/__init__.py` and the three entries of `.claude-plugin/marketplace.json`.

A tool is one function decorated with `@tool()` (or `@tool(write=True)` with a `confirm` argument) in
`src/app_console_mcp/{play,appstore,admob}.py`. Its signature and Google-style docstring become both the MCP
schema and the CLI options. Keep outputs short: they are read by a model.

Issues and pull requests are welcome.

## License

MIT. Not affiliated with Google or Apple. Google Play, AdMob and App Store Connect are trademarks of their owners.
