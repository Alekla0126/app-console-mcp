"""Shared plumbing: settings, the tool registry, HTTP calls and small text helpers.

Every tool is a plain function that returns compact text. The same function is exposed as an MCP tool
and as a CLI command, so what Claude sees and what you see in the terminal is identical.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import requests

CONFIG_DIR = Path(os.environ.get("APPCONSOLE_HOME") or Path.home() / ".config" / "appconsole")
CONFIG_FILE = CONFIG_DIR / "config.json"


class ConsoleError(Exception):
    """An error whose message is meant to be shown to the user as-is."""


# ---------------------------------------------------------------- settings

def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as err:
        raise ConsoleError(f"{CONFIG_FILE} is not valid JSON: {err}") from err


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
    CONFIG_FILE.chmod(0o600)


def setting(name: str, hint: str = "") -> str:
    """Environment variable first, then ~/.config/appconsole/config.json. Empty means unset.

    With a hint, a missing setting raises ConsoleError explaining how to set it.
    """
    value = os.environ.get(name, "").strip()
    if value.startswith("${"):  # an unfilled placeholder from a plugin config counts as unset
        value = ""
    value = value or str(load_config().get(name, "")).strip()
    if not value and hint:
        raise ConsoleError(f"{name} is not set. {hint}\n"
                           f"Set it as an environment variable or run: appconsole config set {name} <value>")
    return os.path.expanduser(value)


# ---------------------------------------------------------------- tool registry

class Toolset:
    """A group of tools that becomes one MCP server and one CLI command group."""

    def __init__(self, key: str, server_name: str, title: str, instructions: str):
        self.key = key  # CLI group and tool name prefix, e.g. "play"
        self.server_name = server_name
        self.title = title
        self.instructions = instructions
        self.tools: list[Callable[..., str]] = []

    def tool(self, *, write: bool = False):
        """Register a tool. write=True marks it as changing something (dry run unless confirm=True)."""
        def deco(fn: Callable[..., str]) -> Callable[..., str]:
            if not fn.__name__.startswith(self.key + "_"):
                raise ValueError(f"{fn.__name__} must start with {self.key}_")
            fn.writes = write
            self.tools.append(fn)
            return fn
        return deco


# ---------------------------------------------------------------- HTTP

_RETRY_STATUS = {429, 500, 502, 503, 504}


def api_error(r: requests.Response) -> str:
    """The useful part of an error body from Google or Apple."""
    try:
        j = r.json()
    except ValueError:
        return r.text.strip()[:400] or r.reason
    if isinstance(j, list) and j:  # some Google APIs wrap the error in a list
        j = j[0]
    if isinstance(j, dict) and isinstance(j.get("error"), dict):
        return j["error"].get("message") or json.dumps(j["error"])[:400]
    if isinstance(j, dict) and isinstance(j.get("errors"), list):
        return "; ".join(f"{e.get('title', '')}: {e.get('detail', '')}".strip(": ") for e in j["errors"])[:600]
    return json.dumps(j)[:400]


def call(session, method: str, url: str, *, raw: bool = False, **kw):
    """HTTP call with retries on network errors, 429 and (for reads) 5xx. Returns parsed JSON."""
    kw.setdefault("timeout", 60)
    idempotent = method.upper() in ("GET", "DELETE", "PUT")
    for attempt in range(4):
        try:
            r = session.request(method, url, **kw)
        except (requests.ConnectionError, requests.Timeout) as err:
            if attempt == 3:
                raise ConsoleError(f"Network error calling {urlsplit(url).netloc}: {err}") from err
            time.sleep(1.5 * 2 ** attempt)
            continue
        retry = r.status_code == 429 or (idempotent and r.status_code in _RETRY_STATUS)
        if retry and attempt < 3:
            time.sleep(1.5 * 2 ** attempt)
            continue
        break
    if r.status_code >= 400:
        path = urlsplit(url).path
        raise ConsoleError(f"HTTP {r.status_code} on {method} {path}: {api_error(r)}")
    if raw:
        return r
    return r.json() if r.content else {}


# ---------------------------------------------------------------- text helpers

def clip(text: str | None, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def check_limits(values: dict[str, str], limits: dict[str, int]) -> None:
    too_long = [f"{k} has {len(v)} characters (max {limits[k]})" for k, v in values.items()
                if k in limits and len(v) > limits[k]]
    if too_long:
        raise ConsoleError("Nothing was changed: " + "; ".join(too_long))


def _around_change(old: str, new: str, width: int = 90) -> tuple[str, str]:
    """Short excerpts of old and new that start near the first character that differs."""
    old, new = " ".join((old or "").split()), " ".join(new.split())
    i = next((n for n, (a, b) in enumerate(zip(old, new, strict=False)) if a != b), min(len(old), len(new)))
    start = max(0, i - 25) if max(len(old), len(new)) > width else 0
    prefix = "…" if start else ""
    return prefix + clip(old[start:], width), prefix + clip(new[start:], width)


def diff_lines(before: dict, after: dict) -> str:
    lines = []
    for k, v in after.items():
        old, new = _around_change(before.get(k) or "", v)
        lines.append(f"- {k}: «{old}» → «{new}» ({len(v)} chars)")
    return "\n".join(lines)


def date_range(days: int, end: date | None = None) -> tuple[date, date]:
    """The last `days` full days, ending yesterday unless `end` is given."""
    if days < 1:
        raise ConsoleError("days must be at least 1")
    end = end or date.today() - timedelta(days=1)
    return end - timedelta(days=days - 1), end


def pct_change(now: float, before: float) -> str:
    if not before:
        return "n/a"
    return f"{(now - before) / before * 100:+.0f}%"


DRY_RUN = "DRY RUN (nothing changed; call again with confirm=true to apply)"
