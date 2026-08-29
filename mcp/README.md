# Döner MCP

Lets an MCP client (Claude Code, or anything else that speaks MCP over stdio)
run Luau inside Roblox and inspect the live game through a running Döner.

`server.py` is the whole thing: one file, Python 3 standard library only. No
install, no dependencies, no checkout of this repo. Copy it anywhere.

## Setup

1. Have Python 3 on PATH (`python --version`).
2. Save `server.py` somewhere permanent.
3. Add it to your MCP client's config:

```json
{
  "mcpServers": {
    "doener": {
      "command": "python",
      "args": ["C:\\path\\to\\server.py"]
    }
  }
}
```

For Claude Code that file is `.mcp.json` in the project directory, or
`~/.claude.json` for a global one. Restart the client afterwards.

## Before it can do anything

| Needed | Why |
| --- | --- |
| Roblox running | there is nothing to attach to otherwise |
| `Döner.exe` running | it serves `\\.\pipe\doener_explorer` |
| `DonerExec.exe` running | it serves `\\.\pipe\doener_exec` |
| **MCP client running as administrator** | `DonerExec.exe` is elevated, and Windows only lets an equally elevated process open its pipe |

That last row is the one that bites. Without it every `exec_*` call answers
`access denied`, and no amount of retrying changes it — the fix is to start the
client elevated.

Everything is local. The server opens two named pipes on the same machine and
makes no network requests.

## Tools

| Tool | What it does |
| --- | --- |
| `exec_status` | Is the helper alive and attached? Returns `OK attached=0\|1` |
| `exec_attach` | Attach to the running Roblox client. Safe when already attached |
| `exec_run` | Run Luau. Returns OK or the error — **not** the script's output |
| `exec_log` | Drain the console: Roblox output plus whatever the script printed |
| `explorer` | One raw command to the explorer bridge |

The usual order is `exec_status` → `exec_attach` → `exec_run` → `exec_log`.
Always finish with `exec_log`: `exec_run` reports whether the script *compiled
and started*, so one that returns OK can still have printed an error.

### `explorer` commands

`ping`, `game_info`, `players`, `children <ref>`, `tree <ref>`.

**Never `find`.** It walks every instance in the place — 60k in a mid-sized game
— and has crashed the client. Walk down with `children` / `tree` instead.

## When it fails

| Message | Meaning |
| --- | --- |
| `access denied on \\.\pipe\...` | The client is not elevated. See above |
| `no answer from \\.\pipe\...` | Nothing is listening: Döner is closed, or not attached yet |
| `ERR empty script` | `exec_run` was called with no source |

The server retries briefly on its own for the genuinely transient case (a pipe
between two back-to-back calls holds a listener open for exactly one exchange),
so a message that reaches you has already survived that.
