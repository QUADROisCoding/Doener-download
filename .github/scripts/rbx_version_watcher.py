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

FILE_EXE      = "Döner.exe"
FILE_RELEASES = "Döner_updates_log.json"

URL_SUPPORTED = "https://offsets.imtheo.lol/roblox/version"
URL_CURRENT   = "https://clientsettingscdn.roblox.com/v2/client-version/WindowsPlayer"

INTERVAL     = 60
HTTP_TIMEOUT = 20
HTTP_RETRIES = 3

UA = "Doener-VersionWatcher/1.0 (+https://github.com/QUADROisCoding/Doener-download)"

CACHE_PATH = Path(os.environ.get("DOENER_CACHE", str(Path.home() / ".doener_versions.json")))

TOKEN_FILES = [
    Path(__file__).with_name("token.txt"),
    Path("/etc/doener/github_token"),
    Path.home() / ".doener_gh_token",
]

DISCORD_WEBHOOK          = os.environ.get("DISCORD_WEBHOOK", "").strip()
DISCORD_ROLE_ID          = os.environ.get("DISCORD_ROLE_ID", "").strip()
DISCORD_DOWNLOAD_CHANNEL = os.environ.get("DISCORD_DOWNLOAD_CHANNEL_ID", "").strip()

PRODUCT = "Döner"

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
    ap = argparse.ArgumentParser(description="Roblox version watcher for the Doener loader")
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
