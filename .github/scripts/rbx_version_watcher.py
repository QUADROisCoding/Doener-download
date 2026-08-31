#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import quote

REPO   = "QUADROisCoding/Doener-download"
BRANCH = "main"

FILE_SUPPORTED = "supported_rbx_version.txt"
FILE_CURRENT   = "current_rbx_version.txt"

FILE_EXE      = "Dopamine.exe"
FILE_RELEASES = "Döner_updates_log.json"   # keeps its old name on purpose:
# this is the release history, not branding. The watcher is its only writer and
# announces only entries it wrote - renaming it without moving the file in the
# same commit would start an empty log and re-announce versions that already
# went out. Rename it and the file together, or leave it.

URL_SUPPORTED = "https://offsets.imtheo.lol/roblox/version"
URL_CURRENT   = "https://clientsettingscdn.roblox.com/v2/client-version/WindowsPlayer"

INTERVAL     = 60
HTTP_TIMEOUT = 20
HTTP_RETRIES = 3

UA = "Dopamine-VersionWatcher/1.0 (+https://github.com/QUADROisCoding/Doener-download)"

CACHE_PATH = Path(os.environ.get("DOENER_CACHE", str(Path.home() / ".doener_versions.json")))

TOKEN_FILES = [
    Path(__file__).with_name("token.txt"),
    Path("/etc/doener/github_token"),
    Path.home() / ".doener_gh_token",
]

DISCORD_WEBHOOK          = os.environ.get("DISCORD_WEBHOOK", "").strip()
DISCORD_ROLE_ID          = os.environ.get("DISCORD_ROLE_ID", "").strip()
DISCORD_DOWNLOAD_CHANNEL = os.environ.get("DISCORD_DOWNLOAD_CHANNEL_ID", "").strip()

# Two ways to get the build itself into the download channel, because a webhook
# can only ever post to the channel it was created for - the announcement
# webhook cannot reach a second channel no matter what id we hand it.
#
#   DISCORD_DOWNLOAD_WEBHOOK - a webhook created ON the download channel.
#                              Simplest: no bot, no permissions to grant.
#   DISCORD_BOT_TOKEN        - a bot in the guild, which can post anywhere it
#                              has Send Messages + Attach Files. This is the one
#                              that actually uses DISCORD_DOWNLOAD_CHANNEL_ID.
#
# The webhook wins if both are set, being the cheaper path.
DISCORD_DOWNLOAD_WEBHOOK = os.environ.get("DISCORD_DOWNLOAD_WEBHOOK", "").strip()
DISCORD_BOT_TOKEN        = os.environ.get("DISCORD_BOT_TOKEN", "").strip()

# Discord's attachment ceiling for a server with no boosts. Level 2 raises it to
# 50 MiB and level 3 to 100 MiB, but assuming the floor means a build that grows
# past it degrades to a link instead of silently failing to post.
DISCORD_MAX_UPLOAD = 10 * 1024 * 1024

RAW_EXE_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{quote(FILE_EXE)}"

PRODUCT = "Dopamine"

log = logging.getLogger("watcher")


def http(url: str, *, method: str = "GET", body: Optional[bytes] = None,
         headers: Optional[dict] = None) -> Tuple[int, bytes]:
    h = {"User-Agent": UA, "Accept": "*/*", "Cache-Control": "no-cache"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def http_retry(url: str, **kw) -> Tuple[int, bytes]:
    last = None
    for attempt in range(HTTP_RETRIES):
        try:
            status, data = http(url, **kw)
            if status < 500:
                return status, data
            last = RuntimeError(f"HTTP {status} from {url}")
        except Exception as e:
            last = e
        if attempt < HTTP_RETRIES - 1:
            wait = (2 ** attempt) + random.uniform(0, 0.7)
            log.debug("retry %d for %s in %.1fs (%s)", attempt + 1, url, wait, last)
            time.sleep(wait)
    raise last if last else RuntimeError(f"unknown error for {url}")


def download_channel() -> str:
    return f"<#{DISCORD_DOWNLOAD_CHANNEL}>" if DISCORD_DOWNLOAD_CHANNEL else "#download"


def discord_notify(lines: list, *, ping: bool = False, dry_run: bool = False) -> None:
    if not DISCORD_WEBHOOK:
        return

    content = "\n".join(l for l in lines if l is not None)
    if ping and DISCORD_ROLE_ID:
        content = f"<@&{DISCORD_ROLE_ID}> {content}"

    payload = {
        "content": content,
        "username": PRODUCT,
        "allowed_mentions": {
            "roles": [DISCORD_ROLE_ID] if (ping and DISCORD_ROLE_ID) else [],
            "parse": [],
        },
    }

    if dry_run:
        log.info("[dry-run] would post to Discord:\n%s", content)
        return

    body = json.dumps(payload).encode("utf-8")
    for attempt in range(2):
        try:
            status, data = http(DISCORD_WEBHOOK, method="POST", body=body,
                                headers={"Content-Type": "application/json"})
            if status in (200, 204):
                log.info("Discord: posted (%d chars)", len(content))
                return
            if status == 429 and attempt == 0:
                try:
                    wait = float(json.loads(data.decode("utf-8")).get("retry_after", 1))
                except Exception:
                    wait = 1.0
                log.warning("Discord rate-limited, waiting %.1fs", min(wait, 10))
                time.sleep(min(wait, 10))
                continue
            log.warning("Discord replied with HTTP %d", status)
            return
        except Exception as e:
            log.warning("Discord unreachable: %s", e)
            return


def _multipart(payload: dict, filename: str, blob: bytes):
    """Build a Discord v10 attachment body.

    Discord wants the message as a `payload_json` field whose `attachments`
    array declares each file by index, and the bytes as `files[N]`. Sending the
    file without the matching `attachments` entry is accepted but drops the
    filename, so both halves are always written together here.
    """
    boundary = "doener" + os.urandom(16).hex()
    sep      = ("--" + boundary + "\r\n").encode()
    out      = bytearray()

    out += sep
    out += b'Content-Disposition: form-data; name="payload_json"\r\n'
    out += b"Content-Type: application/json\r\n\r\n"
    out += json.dumps(payload).encode("utf-8")
    out += b"\r\n"

    out += sep
    out += ('Content-Disposition: form-data; name="files[0]"; filename="%s"\r\n'
            % filename.replace('"', "")).encode("utf-8")
    out += b"Content-Type: application/octet-stream\r\n\r\n"
    out += blob
    out += b"\r\n"

    out += ("--" + boundary + "--\r\n").encode()
    return bytes(out), "multipart/form-data; boundary=" + boundary


def _download_target():
    """Where the build gets posted, and the headers that route it there."""
    if DISCORD_DOWNLOAD_WEBHOOK:
        return DISCORD_DOWNLOAD_WEBHOOK, {}
    if DISCORD_BOT_TOKEN and DISCORD_DOWNLOAD_CHANNEL:
        return (f"https://discord.com/api/v10/channels/{DISCORD_DOWNLOAD_CHANNEL}/messages",
                {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"})
    return None, {}


def post_build(blob: Optional[bytes], filename: str, content: str,
               *, dry_run: bool = False) -> bool:
    """Post the build to the download channel, as a file when that is possible.

    Falls back to a plain link rather than posting nothing: an oversized build,
    a missing token or a rejected upload should still leave people with a way to
    get the update.
    """
    url, extra = _download_target()
    if not url:
        log.warning("no download route set - add DISCORD_DOWNLOAD_WEBHOOK or "
                    "DISCORD_BOT_TOKEN so the build can be posted")
        return False

    attach = blob is not None and len(blob) <= DISCORD_MAX_UPLOAD
    if blob is not None and not attach:
        log.warning("%s is %s, over Discord's %s limit - posting a link instead",
                    filename, human_size(len(blob)), human_size(DISCORD_MAX_UPLOAD))
        content = content + "\n" + RAW_EXE_URL
    elif blob is None:
        content = content + "\n" + RAW_EXE_URL

    payload = {"content": content, "allowed_mentions": {"parse": []}}
    if DISCORD_DOWNLOAD_WEBHOOK:
        payload["username"] = PRODUCT
    if attach:
        payload["attachments"] = [{"id": 0, "filename": filename}]

    if dry_run:
        log.info("[dry-run] would post to the download channel%s:\n%s",
                 " with " + filename if attach else " (link only)", content)
        return True

    if attach:
        body, ctype = _multipart(payload, filename, blob)
    else:
        body, ctype = json.dumps(payload).encode("utf-8"), "application/json"

    for attempt in range(2):
        try:
            status, data = http(url, method="POST", body=body,
                                headers={**extra, "Content-Type": ctype})
            if status in (200, 204):
                log.info("Discord: build posted to the download channel%s",
                         " (" + human_size(len(blob)) + ")" if attach else " as a link")
                return True
            if status == 429 and attempt == 0:
                try:
                    wait = float(json.loads(data.decode("utf-8")).get("retry_after", 1))
                except Exception:
                    wait = 1.0
                log.warning("Discord rate-limited, waiting %.1fs", min(wait, 10))
                time.sleep(min(wait, 10))
                continue
            # 413 means the server's real ceiling is lower than we assumed, so
            # retry once as a link rather than losing the release post.
            if status == 413 and attach:
                log.warning("Discord rejected the attachment (413) - retrying as a link")
                return post_build(None, filename, content, dry_run=dry_run)
            log.warning("Discord replied with HTTP %d - %s", status, data[:200])
            return False
        except Exception as e:
            log.warning("Discord unreachable: %s", e)
            return False
    return False


def human_size(n: int) -> str:
    if not n:
        return "?"
    mb = n / (1024 * 1024)
    return f"{mb:.1f} MB" if mb >= 1 else f"{n / 1024:.0f} KB"


def fetch_supported() -> str:
    status, data = http_retry(URL_SUPPORTED)
    if status != 200:
        raise RuntimeError(f"offsets.imtheo.lol replied with HTTP {status}")
    guid = data.decode("utf-8", "replace").strip()
    if not guid.startswith("version-"):
        raise RuntimeError(f"unexpected reply from offsets.imtheo.lol: {guid[:80]!r}")
    return guid


def fetch_current() -> Tuple[str, str]:
    status, data = http_retry(URL_CURRENT)
    if status != 200:
        raise RuntimeError(f"clientsettingscdn replied with HTTP {status}")
    j = json.loads(data.decode("utf-8", "replace"))
    guid  = str(j.get("clientVersionUpload", "")).strip()
    human = str(j.get("version", "")).strip()
    if not guid.startswith("version-"):
        raise RuntimeError(f"clientVersionUpload missing/invalid: {j!r}")
    return guid, human


def load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("cache %s not readable (%s) - starting empty", CACHE_PATH, e)
        return {}


def save_cache(cache: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, indent=2, ensure_ascii=False), "utf-8")
        tmp.replace(CACHE_PATH)
    except Exception as e:
        log.warning("cache %s not writable: %s", CACHE_PATH, e)


class GitHub:

    API = "https://api.github.com"

    def __init__(self, repo: str, branch: str, token: Optional[str], dry_run: bool):
        self.repo    = repo
        self.branch  = branch
        self.token   = token
        self.dry_run = dry_run
        self.state: dict[str, Tuple[Optional[str], Optional[str]]] = {}

    def _headers(self) -> dict:
        h = {"Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def list_dir(self) -> list:
        url = f"{self.API}/repos/{self.repo}/contents/?ref={self.branch}"
        status, data = http_retry(url, headers=self._headers())
        if status != 200:
            raise RuntimeError(f"GET /: HTTP {status} - {data[:200]!r}")
        return json.loads(data.decode("utf-8"))

    def load(self, path: str) -> Tuple[Optional[str], Optional[str]]:
        url = f"{self.API}/repos/{self.repo}/contents/{quote(path)}?ref={self.branch}"
        status, data = http_retry(url, headers=self._headers())
        if status == 404:
            log.info("%s does not exist yet - will be created", path)
            self.state[path] = (None, None)
            return None, None
        if status != 200:
            raise RuntimeError(f"GET {path}: HTTP {status} - {data[:200]!r}")
        j = json.loads(data.decode("utf-8"))
        text = base64.b64decode(j.get("content", "")).decode("utf-8", "replace")
        self.state[path] = (j.get("sha"), text)
        return j.get("sha"), text

    def blob(self, sha: str) -> bytes:
        """Exact bytes behind one blob sha.

        By sha rather than by path: raw.githubusercontent is CDN-cached for a
        few minutes, so fetching by name right after a push can hand back the
        build we are replacing. The sha is the one check_releases just saw.
        """
        url = f"{self.API}/repos/{self.repo}/git/blobs/{sha}"
        status, data = http_retry(url, headers={**self._headers(),
                                                "Accept": "application/vnd.github.raw"})
        if status != 200:
            raise RuntimeError(f"GET blob {sha[:10]}: HTTP {status} - {data[:200]!r}")
        return data

    def put(self, path: str, text: str, message: str) -> bool:
        sha, cur = self.state.get(path, (None, None))
        if cur == text:
            return False

        if self.dry_run:
            log.info("[dry-run] would write %s: %r", path, text)
            self.state[path] = (sha, text)
            return True

        if not self.token:
            raise RuntimeError("no GITHUB_TOKEN set - cannot write")

        payload = {
            "message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "branch":  self.branch,
        }
        if sha:
            payload["sha"] = sha

        url = f"{self.API}/repos/{self.repo}/contents/{quote(path)}"
        status, data = http_retry(url, method="PUT",
                                  body=json.dumps(payload).encode("utf-8"),
                                  headers={**self._headers(),
                                           "Content-Type": "application/json"})

        if status in (409, 422):
            log.warning("%s: stale sha (HTTP %d) - reloading and retrying", path, status)
            self.load(path)
            sha, cur = self.state.get(path, (None, None))
            if cur == text:
                return False
            if sha:
                payload["sha"] = sha
            else:
                payload.pop("sha", None)
            status, data = http_retry(url, method="PUT",
                                      body=json.dumps(payload).encode("utf-8"),
                                      headers={**self._headers(),
                                               "Content-Type": "application/json"})

        if status not in (200, 201):
            raise RuntimeError(f"PUT {path}: HTTP {status} - {data[:300]!r}")

        j = json.loads(data.decode("utf-8"))
        self.state[path] = (j["content"]["sha"], text)
        return True


def read_token() -> Optional[str]:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok.strip()
    for p in TOKEN_FILES:
        try:
            t = p.read_text("utf-8").strip()
            if t:
                log.info("token read from %s", p)
                return t
        except OSError:
            continue
    return None


def build_content(guid: str, human: str) -> str:
    return f"{guid}\n{human}\n" if human else f"{guid}\n"


def first_line(text: Optional[str]) -> str:
    return (text or "").strip().splitlines()[0].strip() if (text or "").strip() else ""


def next_version(prev: Optional[str]) -> str:
    if not prev:
        return "v0.1"
    m = re.match(r"v(\d+)\.(\d+)\s*$", prev.strip())
    if not m:
        return "v0.1"
    return f"v{m.group(1)}.{int(m.group(2)) + 1}"


def check_releases(gh: GitHub) -> None:
    exe = next((e for e in gh.list_dir() if e.get("name") == FILE_EXE), None)
    if not exe:
        log.warning("%s is not in the repo", FILE_EXE)
        return
    sha, size = exe.get("sha"), exe.get("size", 0)

    _, raw = gh.state.get(FILE_RELEASES, (None, None))
    if raw is None:
        _, raw = gh.load(FILE_RELEASES)

    try:
        doc = json.loads(raw) if raw and raw.strip() else {}
    except ValueError:
        log.warning("%s is not valid JSON - rebuilding", FILE_RELEASES)
        doc = {}

    releases = doc.get("releases") or []
    if releases and releases[-1].get("sha") == sha:
        return

    version = next_version(releases[-1].get("version") if releases else None)
    first   = not releases
    releases.append({
        "version":   version,
        "sha":       sha,
        "size":      size,
        "published": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    })
    doc["latest"]   = version
    doc["releases"] = releases

    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    if gh.put(FILE_RELEASES, text, f"release {version}"):
        log.info("-> new build logged: %s (sha %s)", version, sha[:10])
        if not first:
            sup_guid = first_line(gh.state.get(FILE_SUPPORTED, (None, None))[1])
            discord_notify([
                f"Download the new update at {download_channel()}  **{PRODUCT} {version}**",
                "```diff",
                f"+ New build available. ({version}, {human_size(size)})",
                "```",
                (f"{PRODUCT} supports the roblox version 〘⚒️〙{sup_guid}" if sup_guid else None),
            ], ping=True, dry_run=gh.dry_run)

            # The announcement points at the download channel, so put the build
            # there straight after. Order matters: the pointer should not land
            # before the thing it points at.
            #
            # A failure here is logged and swallowed - the release is already
            # committed and announced, and losing that to an upload problem
            # would be worse than a channel that is one post behind.
            blob = None
            try:
                blob = gh.blob(sha)
            except Exception as e:
                log.warning("could not read %s for upload: %s", FILE_EXE, e)
            try:
                post_build(blob, FILE_EXE,
                           f"**{PRODUCT} {version}**  -  {human_size(size)}",
                           dry_run=gh.dry_run)
            except Exception as e:
                log.warning("build upload failed: %s", e)


def check_once(gh: GitHub, cache: dict) -> None:
    guid_map = cache.setdefault("guid_map", {})

    prev_sup = first_line(gh.state.get(FILE_SUPPORTED, (None, None))[1])
    prev_cur = first_line(gh.state.get(FILE_CURRENT,   (None, None))[1])

    cur_guid, cur_human = fetch_current()
    sup_guid            = fetch_supported()

    if cur_human and guid_map.get(cur_guid) != cur_human:
        guid_map[cur_guid] = cur_human
        save_cache(cache)

    sup_human = guid_map.get(sup_guid, "")

    match = "MATCH" if sup_guid == cur_guid else "MISMATCH"
    log.info("supported=%s (%s)  current=%s (%s)  -> %s",
             sup_guid, sup_human or "?", cur_guid, cur_human or "?", match)

    wrote = False
    if gh.put(FILE_SUPPORTED, build_content(sup_guid, sup_human),
              f"supported: {sup_human or sup_guid}"):
        log.info("-> %s updated", FILE_SUPPORTED)
        wrote = True
    if gh.put(FILE_CURRENT, build_content(cur_guid, cur_human),
              f"current: {cur_human or cur_guid}"):
        log.info("-> %s updated", FILE_CURRENT)
        wrote = True
    if not wrote:
        log.debug("no roblox change")

    in_sync = (sup_guid == cur_guid)

    if prev_cur and cur_guid != prev_cur:
        if in_sync:
            discord_notify([
                "```diff",
                f"+ Roblox updated to {cur_human or cur_guid}. Offsets already match.",
                "```",
                f"{PRODUCT} supports the roblox version 〘⚒️〙{sup_guid}",
            ], dry_run=gh.dry_run)
        else:
            discord_notify([
                "```diff",
                f"- Roblox updated to {cur_human or cur_guid} ({cur_guid}).",
                f"- Offsets are not ready yet, {PRODUCT} stays on the old version.",
                "```",
                f"{PRODUCT} supports the roblox version 〘⚒️〙{sup_guid}",
            ], dry_run=gh.dry_run)

    if prev_sup and sup_guid != prev_sup:
        if in_sync:
            discord_notify([
                "```diff",
                f"+ Updated to the latest version. ({sup_guid})",
                "```",
                f"{PRODUCT} supports the roblox version 〘⚒️〙{sup_guid}",
            ], ping=True, dry_run=gh.dry_run)
        else:
            discord_notify([
                "```diff",
                f"+ New offsets are live. ({sup_guid})",
                f"- Roblox has moved on to {cur_human or cur_guid} already.",
                "```",
                f"{PRODUCT} supports the roblox version 〘⚒️〙{sup_guid}",
            ], dry_run=gh.dry_run)

    check_releases(gh)


def main() -> int:
    ap = argparse.ArgumentParser(description="Roblox version watcher for the Dopamine loader")
    ap.add_argument("--interval", type=int, default=INTERVAL,
                    help=f"seconds between checks (default: {INTERVAL})")
    ap.add_argument("--once", action="store_true", help="check once, then exit")
    ap.add_argument("--dry-run", action="store_true", help="write nothing to GitHub")
    ap.add_argument("--test-discord", action="store_true",
                    help="post one test message to the webhook and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout)

    if args.test_discord:
        if not DISCORD_WEBHOOK:
            log.error("DISCORD_WEBHOOK is not set")
            return 2
        discord_notify([
            "```diff",
            "+ Test message. The watcher can post in this channel.",
            "```",
        ])
        return 0

    if DISCORD_WEBHOOK:
        log.info("Discord alerts active%s", ", role ping on" if DISCORD_ROLE_ID else "")
    else:
        log.info("DISCORD_WEBHOOK not set - the watcher will stay silent")

    token = read_token()
    if not token and not args.dry_run:
        log.error("no token found. Set GITHUB_TOKEN or create %s.",
                  " / ".join(str(p) for p in TOKEN_FILES))
        return 2

    cache = load_cache()
    gh = GitHub(REPO, BRANCH, token, args.dry_run)

    for path in (FILE_SUPPORTED, FILE_CURRENT, FILE_RELEASES):
        try:
            gh.load(path)
        except Exception as e:
            log.error("could not load initial state of %s: %s", path, e)
            return 3

    if args.once:
        try:
            check_once(gh, cache)
            return 0
        except Exception as e:
            log.error("run failed: %s", e)
            return 1

    log.info("watcher running. interval %ds, repo %s@%s%s",
             args.interval, REPO, BRANCH, "  [DRY-RUN]" if args.dry_run else "")

    fails = 0
    while True:
        try:
            check_once(gh, cache)
            fails = 0
        except KeyboardInterrupt:
            log.info("stopped")
            return 0
        except Exception as e:
            fails += 1
            log.error("run failed (%d in a row): %s", fails, e)
            time.sleep(min(300, 10 * fails))
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            log.info("stopped")
            return 0


if __name__ == "__main__":
    sys.exit(main())
