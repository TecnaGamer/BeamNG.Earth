#!/usr/bin/env python3
"""Tiny CLI for BeamNG's built-in MCP server (Options > Advanced > MCP).

  mcp.py lua 'return 1+1'             run GE Lua, print result
  mcp.py luafile path/to/probe.lua    run a local Lua file's contents in GE
  mcp.py call <tool> '<json args>'    call any MCP tool
  mcp.py list                         list tools
"""
import json
import time
import sys
import urllib.request

import os

URL = os.environ.get("BEAMNG_MCP_URL")


def get_mcp_url(timeout=0.6):
    global URL
    if URL:
        return URL
    candidates = [29292, 29293, 29294]
    env_port = os.environ.get("BEAMNG_MCP_PORT")
    if env_port:
        try:
            candidates.insert(0, int(env_port))
        except ValueError:
            pass
    for port in candidates:
        candidate_url = f"http://127.0.0.1:{port}/mcp"
        try:
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
            req = urllib.request.Request(candidate_url, body, {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status == 200:
                    URL = candidate_url
                    return URL
        except Exception:
            continue
    URL = "http://127.0.0.1:29292/mcp"
    return URL


def rpc(method, params=None, timeout=120):
    url = get_mcp_url()
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8", "replace"), strict=False)
    if "error" in d:
        raise SystemExit(f"RPC error: {d['error']}")
    return d["result"]


def call(tool, args=None):
    res = rpc("tools/call", {"name": tool, "arguments": args or {}})
    out = []
    for c in res.get("content", []):
        out.append(c.get("text") if c.get("type") == "text" else f"<{c.get('type')} content>")
    text = "\n".join(x for x in out if x is not None)
    if res.get("isError"):
        raise SystemExit(f"TOOL ERROR: {text}")
    return text


def quiet_ui():
    """For automated runs: our welcome page not shown this game session (it stays unseen for the player), and
    closed if it is open (the UI back to play). Best effort."""
    try:
        call("run_lua", {"code": "local ok, s = pcall(require, 'beamngEarth/settings') "
                                 "if ok and s then s.welcomeSuppressed = true end return 'ok'"})
        call("set_ui_state", {"route": "play"})
        time.sleep(0.6)         # (the page fades out over a few frames)
    except Exception:
        pass


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    cmd = argv[0]
    if cmd == "list":
        for t in rpc("tools/list")["tools"]:
            print(t["name"])
    elif cmd == "lua":
        print(call("run_lua", {"code": argv[1]}))
    elif cmd == "luafile":
        with open(argv[1]) as f:
            print(call("run_lua", {"code": f.read()}))
    elif cmd == "call":
        print(call(argv[1], json.loads(argv[2]) if len(argv) > 2 else {}))
    elif cmd == "schema":
        for t in rpc("tools/list")["tools"]:
            if t["name"] == argv[1]:
                print(json.dumps(t, indent=1))
    else:
        raise SystemExit(f"unknown command {cmd}")


if __name__ == "__main__":
    main(sys.argv[1:])
