#!/usr/bin/env python3
r"""Elevated relay for Doener's explorer bridge.

WHY THIS EXISTS
Doener.exe ships a requireAdministrator manifest, so `\\.\pipe\doener_explorer`
only accepts an equally elevated caller. An unelevated tool gets errno 13 on
every request. Elevating the tool per question means a UAC prompt per question,
which turns a five-step investigation into five interruptions.

So: elevate ONCE. This process sits in the background holding the elevation, and
anything running as the same user drops a request file and reads the answer.

    # once, accepts a UAC prompt:
    python relay.py --serve

    # afterwards, from anything, no prompt:
    python relay.py children root=localplayer path=PlayerGui

WHAT IT WILL NOT DO
`find` is refused here rather than left to each caller to remember. It walks
every instance in the place and has taken the client down three times; the relay
is the one place that sees every request, so it is the right place to enforce it.

THE TRADE-OFF, PLAINLY
While the relay is up, any process running as this user can reach an elevated
pipe through it. The bridge is read-only against Roblox, but `read`, `scan` and
`dump` will read that process's memory, so this is a real widening of what an
unelevated program can do on this machine. It is a development tool: start it
when you need it, and stop it with --stop when you are done.
"""
import io
import os
import sys
import time
import uuid

PIPE = r'\\.\pipe\doener_explorer'
END = '<<<DOENER_END>>>'

# Under the user's own profile, not a world-writable temp root - it keeps the
# request queue reachable only by this account.
DIR = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')),
                   'Doner', 'bridge')
STOP = os.path.join(DIR, 'stop')

BANNED = {'find'}


def pipe_call(line, timeout=25.0, retries=8):
    """One request, one reply. The pipe holds a listener for exactly one
    exchange, so it is opened and closed per call."""
    last = None
    for _ in range(retries):
        try:
            with open(PIPE, 'r+b', buffering=0) as f:
                f.write((line + '\n').encode('utf-8'))
                out, dl = [], time.time() + timeout
                while time.time() < dl:
                    chunk = f.read(4096)
                    if not chunk:
                        break
                    out.append(chunk.decode('utf-8', 'replace'))
                    if END in ''.join(out):
                        return ''.join(out).split(END)[0]
                return ''.join(out).strip()
        except OSError as e:
            last = e
            if isinstance(e, PermissionError) or getattr(e, 'errno', None) == 13:
                return ('ACCESS DENIED - this relay is not elevated. '
                        'Start it with: python relay.py --serve  (accept the UAC prompt)')
            time.sleep(0.12)
    return 'NO ANSWER from %s (%s) - is Doener running and attached?' % (PIPE, last)


def serve():
    os.makedirs(DIR, exist_ok=True)
    for f in os.listdir(DIR):                     # a stale queue is not a backlog
        try:
            os.remove(os.path.join(DIR, f))
        except OSError:
            pass

    print('doener relay serving from', DIR, flush=True)
    while True:
        if os.path.exists(STOP):
            os.remove(STOP)
            print('stopped', flush=True)
            return
        for name in sorted(os.listdir(DIR)):
            if not name.endswith('.req'):
                continue
            req = os.path.join(DIR, name)
            try:
                cmd = io.open(req, encoding='utf-8').read().strip()
            except OSError:
                continue
            try:
                os.remove(req)
            except OSError:
                pass
            if not cmd:
                continue

            verb = cmd.split()[0].lower()
            answer = ('refused: %s walks the whole instance tree and has crashed '
                      'the client' % verb) if verb in BANNED else pipe_call(cmd)

            tmp = os.path.join(DIR, name[:-4] + '.tmp')
            io.open(tmp, 'w', encoding='utf-8', newline='\n').write(answer)
            os.replace(tmp, os.path.join(DIR, name[:-4] + '.res'))
        time.sleep(0.05)


def ask(cmd, timeout=40.0):
    """Client side. Returns the relay's answer, or raises if it never came."""
    if not os.path.isdir(DIR):
        raise SystemExit('no relay running - start one with: python relay.py --serve')
    tag = uuid.uuid4().hex
    req = os.path.join(DIR, tag + '.req')
    res = os.path.join(DIR, tag + '.res')
    tmp = req + '.tmp'
    io.open(tmp, 'w', encoding='utf-8', newline='\n').write(cmd)
    os.replace(tmp, req)                          # atomic: never a half-written request

    dl = time.time() + timeout
    while time.time() < dl:
        if os.path.exists(res):
            out = io.open(res, encoding='utf-8').read()
            try:
                os.remove(res)
            except OSError:
                pass
            return out
        time.sleep(0.04)
    raise SystemExit('relay did not answer in %.0fs - is it still running?' % timeout)


if __name__ == '__main__':
    args = sys.argv[1:]
    if args and args[0] == '--serve':
        serve()
    elif args and args[0] == '--stop':
        os.makedirs(DIR, exist_ok=True)
        io.open(STOP, 'w').write('')
        print('stop requested')
    else:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        print(ask(' '.join(args) or 'ping'))
