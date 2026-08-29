#!/usr/bin/env python3
r"""Doener MCP server - drive a running Doener from an MCP client.

Standalone: one file, Python 3 standard library only. No install, no
checkout, no repo. Drop it anywhere and point an MCP client at it:

    {
      "mcpServers": {
        "doener": { "command": "python", "args": ["<path to>/server.py"] }
      }
    }

It speaks to two named pipes that Doener opens on this machine:

    \\.\pipe\doener_exec       DonerExec.exe   STATUS / ATTACH / EXEC / KILL / LOG
    \\.\pipe\doener_explorer   Doener.exe      the explorer bridge

Both must be running locally; nothing here reaches over a network.

DonerExec.exe ships a requireAdministrator manifest, so its pipe is only
reachable from an equally elevated process - the MCP client has to run as
administrator or every exec_* call answers "access denied".

stdout carries protocol messages ONLY. Diagnostics go to stderr.
"""
import base64
import json
import sys
import time

EXEC_PIPE = r'\\.\pipe\doener_exec'
EXPL_PIPE = r'\\.\pipe\doener_explorer'
EXPL_END = '<<<DOENER_END>>>'


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ── pipe transport ──────────────────────────────────────────────────────────
def pipe_call(path, line, read_until=None, timeout=20.0, retries=12):
    """One request, one reply. Opens and closes per call, which is what both
    servers expect - each holds a listener open for exactly one exchange.

    ENOENT/busy means no instance is armed this instant, which happens
    legitimately between back-to-back calls, so it retries briefly.
    """
    last = None
    for _ in range(retries):
        try:
            with open(path, 'r+b', buffering=0) as f:
                f.write((line + '\n').encode('utf-8'))
                out = []
                deadline = time.time() + timeout
                while time.time() < deadline:
                    chunk = f.read(4096)
                    if not chunk:
                        break
                    out.append(chunk.decode('utf-8', 'replace'))
                    joined = ''.join(out)
                    if read_until is None:
                        if '\n' in joined:
                            return joined.split('\n')[0]
                    elif read_until in joined:
                        return joined.split(read_until)[0]
                return ''.join(out).strip()
        except OSError as e:
            last = e
            # Access denied: the far end is elevated and we are not. Not
            # transient, so say so immediately instead of retrying 12 times.
            # Python surfaces this as errno 13, NOT winerror 5, on an open() of
            # a pipe path - checking only winerror let the real reason hide
            # behind a generic "no answer".
            if isinstance(e, PermissionError) or getattr(e, 'errno', None) == 13                or getattr(e, 'winerror', None) == 5:
                raise RuntimeError(
                    'access denied on %s - DonerExec runs elevated, so Claude Code '
                    'has to be running as administrator to reach it' % path)
            time.sleep(0.05)
    raise RuntimeError('no answer from %s (%s)' % (path, last))


def exec_call(verb, timeout=20.0):
    return pipe_call(EXEC_PIPE, verb, timeout=timeout)


def exec_log():
    """LOG returns queued lines terminated by a lone '.'"""
    try:
        with open(EXEC_PIPE, 'r+b', buffering=0) as f:
            f.write(b'LOG\n')
            out = []
            deadline = time.time() + 8
            while time.time() < deadline:
                chunk = f.read(4096)
                if not chunk:
                    break
                out.append(chunk.decode('utf-8', 'replace'))
                if ''.join(out).rstrip().endswith('\n.') or ''.join(out).strip() == '.':
                    break
            text = ''.join(out)
    except OSError as e:
        if isinstance(e, PermissionError) or getattr(e, 'errno', None) == 13            or getattr(e, 'winerror', None) == 5:
            return 'access denied - run Claude Code as administrator'
        return 'pipe error: %s' % e
    lines = [l for l in text.splitlines() if l.strip() and l.strip() != '.']
    return '\n'.join(lines) if lines else '(log empty)'


# ── the prompt ──────────────────────────────────────────────────────────────
# Returned from `initialize`. It covers HOW TO USE these tools and nothing else:
# no build notes, no repo layout, no history. Whoever runs this file may never
# have seen the project it came from.
INSTRUCTIONS = """Drives a running Doener (a Roblox external) on this machine.

ORDER OF OPERATIONS
1. exec_status  confirm the helper is alive. attached=0 means step 2 is needed.
2. exec_attach  attach to the running Roblox client. Safe when already attached.
3. exec_run     run Luau. Returns OK or the error, NOT the script's output.
4. exec_log     read what the script printed. Always call this after exec_run:
                a script that returned OK can still have printed an error.

REQUIREMENTS
Roblox and Doener must already be running. The exec_* tools additionally need
this MCP client to be running as administrator, because DonerExec.exe is
elevated and its pipe refuses unelevated callers. "access denied" means exactly
that and will not resolve on retry.

USING explorer
Pass one bridge command: ping, game_info, players, children <ref>, tree <ref>.
NEVER pass find. It walks every instance in the place and has crashed the
client. To locate something, walk down with children/tree instead.

WHEN A CALL FAILS
"no answer from ..." means nothing is listening: Doener is closed, or not
attached yet. Say so rather than retrying in a loop - these tools already retry
internally for the transient case."""

# ── tools ───────────────────────────────────────────────────────────────────
TOOLS = [
    {
        'name': 'exec_status',
        'description': 'Is the executor helper alive and attached to Roblox? Returns "OK attached=0|1".',
        'inputSchema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'exec_attach',
        'description': 'Attach the executor to the running Roblox client. Safe to call when already attached.',
        'inputSchema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'exec_run',
        'description': ('Run Luau inside Roblox and return OK or an error. Use exec_log '
                        'afterwards to read anything the script printed.'),
        'inputSchema': {
            'type': 'object',
            'properties': {'script': {'type': 'string', 'description': 'Luau source to execute.'}},
            'required': ['script'],
        },
    },
    {
        'name': 'exec_log',
        'description': 'Drain the executor console: Roblox output plus anything the last script printed.',
        'inputSchema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'explorer',
        'description': ('Raw explorer bridge command against Doener.exe, e.g. "ping", "game_info", '
                        '"players". Do NOT use "find" - it walks 60k instances and has crashed the client.'),
        'inputSchema': {
            'type': 'object',
            'properties': {'command': {'type': 'string', 'description': 'Command line, e.g. "game_info".'}},
            'required': ['command'],
        },
    },
]


def call_tool(name, args):
    if name == 'exec_status':
        return exec_call('STATUS', timeout=5)
    if name == 'exec_attach':
        return exec_call('ATTACH', timeout=40)
    if name == 'exec_run':
        script = args.get('script') or ''
        if not script.strip():
            return 'ERR empty script'
        b64 = base64.b64encode(script.encode('utf-8')).decode('ascii')
        return exec_call('EXEC ' + b64, timeout=30)
    if name == 'exec_log':
        return exec_log()
    if name == 'explorer':
        return pipe_call(EXPL_PIPE, args.get('command') or 'ping',
                         read_until=EXPL_END, timeout=15)
    return 'unknown tool: %s' % name


# ── JSON-RPC over stdio ─────────────────────────────────────────────────────
def send(obj):
    sys.stdout.write(json.dumps(obj) + '\n')
    sys.stdout.flush()


def main():
    log('doener mcp: ready')
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            continue

        mid = msg.get('id')
        method = msg.get('method')

        if method == 'initialize':
            send({'jsonrpc': '2.0', 'id': mid, 'result': {
                'protocolVersion': '2024-11-05',
                'capabilities': {'tools': {}},
                'serverInfo': {'name': 'doener', 'version': '1.1.0'},
                'instructions': INSTRUCTIONS,
            }})
        elif method == 'tools/list':
            send({'jsonrpc': '2.0', 'id': mid, 'result': {'tools': TOOLS}})
        elif method == 'tools/call':
            params = msg.get('params') or {}
            try:
                text = call_tool(params.get('name'), params.get('arguments') or {})
                is_err = False
            except Exception as e:              # a dead pipe is a result, not a crash
                text, is_err = str(e), True
            send({'jsonrpc': '2.0', 'id': mid, 'result': {
                'content': [{'type': 'text', 'text': str(text)}],
                'isError': is_err,
            }})
        elif mid is not None:
            send({'jsonrpc': '2.0', 'id': mid,
                  'error': {'code': -32601, 'message': 'method not found: %s' % method}})


if __name__ == '__main__':
    main()
