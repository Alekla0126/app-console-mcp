"""`appconsole`: every MCP tool as a terminal command, plus `mcp`, `config`, `doctor` and `admob login`.

    appconsole play reviews com.example.app --limit 5
    appconsole appstore listing "My App" --locale es-MX
    appconsole admob report --days 28 --by app,format --currency USD
    appconsole mcp play            # run the Google Play MCP server on stdio
"""
from __future__ import annotations

import argparse
import inspect
import os
import re
import sys
import typing

from . import __version__, admob, appstore, play
from .core import CONFIG_FILE, ConsoleError, load_config, save_config

TOOLSETS = {t.key: t for t in (play.toolset, appstore.toolset, admob.toolset)}


def parse_doc(doc: str) -> tuple[str, str, dict[str, str]]:
    """Split a Google-style docstring into (summary, body without Args, {arg: help})."""
    summary = doc.strip().splitlines()[0] if doc.strip() else ""
    body, _, args_block = doc.partition("\nArgs:\n")
    helps: dict[str, str] = {}
    current = None
    for line in args_block.splitlines():
        m = re.match(r"^\s{2,8}(\w+): (.*)$", line)
        if m:
            current = m.group(1)
            helps[current] = m.group(2).strip()
        elif current and line.strip():
            helps[current] += " " + line.strip()
    return summary, body.strip(), helps


def _add_command(sub, fn, prefix: str) -> None:
    summary, body, helps = parse_doc((inspect.getdoc(fn) or "").replace("%", "%%"))  # argparse formats help with %
    name = fn.__name__.removeprefix(prefix).replace("_", "-")
    p = sub.add_parser(name, help=summary, description=body, formatter_class=argparse.RawDescriptionHelpFormatter)
    hints = typing.get_type_hints(fn)
    for pname, param in inspect.signature(fn).parameters.items():
        t = hints.get(pname, str)
        is_list = typing.get_origin(t) is list
        base = typing.get_args(t)[0] if is_list else t
        h = helps.get(pname, "")
        if param.default is inspect.Parameter.empty:
            p.add_argument(pname, type=base, nargs="+" if is_list else None, help=h)
        elif t is bool:
            p.add_argument("--" + pname.replace("_", "-"), dest=pname, action="store_true", help=h)
        else:
            shown = f" (default: {param.default})" if param.default not in ("", 0) else ""
            p.add_argument("--" + pname.replace("_", "-"), dest=pname, type=base, default=param.default,
                           nargs="+" if is_list else None, help=h + shown)
    p.set_defaults(_fn=fn)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="appconsole", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"appconsole {__version__}")
    groups = parser.add_subparsers(dest="group", required=True, metavar="{play,appstore,admob,mcp,config,doctor}")
    for key, ts in TOOLSETS.items():
        g = groups.add_parser(key, help=f"{ts.title} commands")
        cmds = g.add_subparsers(dest="cmd", required=True)
        for fn in ts.tools:
            _add_command(cmds, fn, key + "_")
        if key == "admob":
            login = cmds.add_parser("login", help="Authorize AdMob once with your own OAuth desktop client")
            login.add_argument("--client-secrets", required=True, help="OAuth client JSON (type: Desktop app)")
            login.add_argument("--no-browser", action="store_true", help="Print the URL instead of opening it")
            login.set_defaults(_fn=lambda client_secrets, no_browser: admob.login(client_secrets, no_browser))

    m = groups.add_parser("mcp", help="Run an MCP server on stdio")
    m.add_argument("service", choices=list(TOOLSETS))

    c = groups.add_parser("config", help=f"Read or change {CONFIG_FILE}")
    c.add_argument("action", choices=["show", "set", "unset"])
    c.add_argument("key", nargs="?")
    c.add_argument("value", nargs="?")

    groups.add_parser("doctor", help="Check which services are configured and reachable")
    return parser


def _config(action: str, key: str | None, value: str | None) -> str:
    cfg = load_config()
    if action == "show":
        return "\n".join(f"{k} = {v}" for k, v in sorted(cfg.items())) or f"{CONFIG_FILE} is empty."
    if not key:
        raise ConsoleError("Give a key, e.g. GOOGLE_PLAY_SERVICE_ACCOUNT")
    if action == "set":
        if value is None:
            raise ConsoleError("Give a value.")
        cfg[key] = value
    else:
        cfg.pop(key, None)
    save_config(cfg)
    return f"{action} {key} in {CONFIG_FILE}"


def doctor() -> str:
    checks = [("Google Play", play.play_apps), ("App Store Connect", appstore.appstore_apps),
              ("AdMob", admob.admob_accounts)]
    out = []
    for name, fn in checks:
        try:
            lines = fn().splitlines()
            out.append(f"✓ {name}: {len(lines)} item(s), e.g. {lines[0] if lines else '-'}")
        except Exception as err:
            out.append(f"✗ {name}: {str(err).splitlines()[0]}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.group == "mcp":
            from .server import serve
            serve(TOOLSETS[args.service])
            return 0
        if args.group == "config":
            print(_config(args.action, args.key, args.value))
        elif args.group == "doctor":
            print(doctor())
        else:
            kwargs = {k: v for k, v in vars(args).items() if k not in ("group", "cmd", "_fn")}
            print(args._fn(**kwargs))
        return 0
    except ConsoleError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as err:  # auth refresh failures and the like: one line unless debugging
        if os.environ.get("APPCONSOLE_DEBUG"):
            raise
        print(f"error: {type(err).__name__}: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
