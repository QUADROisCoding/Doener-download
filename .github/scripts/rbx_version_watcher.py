#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rbx_version_watcher.py - 24/7 Version-Watcher fuer den Doener-Loader.

Pollt im Intervall zwei Quellen:

  supported : https://offsets.imtheo.lol/roblox/version
              -> Klartext, z.B. "version-ddf02245bdbb428c"
              Das ist die Roblox-Version, fuer die theo's Offsets aktuell gelten.

  current   : https://clientsettingscdn.roblox.com/v2/client-version/WindowsPlayer
              -> JSON, "clientVersionUpload" = GUID, "version" = 0.730.23.7300792

Bei Aenderung werden die Werte per GitHub Contents-API ins Repo geschrieben:

  supported_rbx_version.txt
  current_rbx_version.txt

Dateiformat (2 Zeilen, Zeile 2 optional):

  version-ddf02245bdbb428c
  0.730.23.7300792

Zeile 1 ist der Vergleichs-Key (GUID), Zeile 2 die menschenlesbare Version.
Fuer "supported" liefert die Offsets-Seite nur den GUID - die lesbare Version
kommt aus einem lokalen Cache, der bei jedem Poll mit dem aktuellen
GUID->Version-Paar gefuettert wird. Nach der ersten Roblox-Aktualisierung
kennt der Watcher die Zuordnung also selbst.

Nur Python-Stdlib, kein pip noetig.

Token:  Umgebungsvariable GITHUB_TOKEN
        oder /etc/doener/github_token
        oder ~/.doener_gh_token
        (Fine-grained PAT reicht: nur dieses Repo, Contents: Read and write)

Aufruf:
    python3 rbx_version_watcher.py                 # Dauerbetrieb
    python3 rbx_version_watcher.py --once --dry-run # einmal pruefen, nichts schreiben
"""

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

# --------------------------------------------------------------------------- Konfiguration
REPO   = "QUADROisCoding/Doener-download"
BRANCH = "main"

FILE_SUPPORTED = "supported_rbx_version.txt"
FILE_CURRENT   = "current_rbx_version.txt"

# Zweite Aufgabe: neue Doener-Builds erkennen und protokollieren
FILE_EXE       = "Döner.exe"
FILE_RELEASES  = "Döner_updates_log.json"

URL_SUPPORTED = "https://offsets.imtheo.lol/roblox/version"
URL_CURRENT   = "https://clientsettingscdn.roblox.com/v2/client-version/WindowsPlayer"

INTERVAL     = 60          # Sekunden zwischen zwei Checks
HTTP_TIMEOUT = 20
HTTP_RETRIES = 3

UA = "Doener-VersionWatcher/1.0 (+https://github.com/QUADROisCoding/Doener-download)"

CACHE_PATH  = Path(os.environ.get("DOENER_CACHE", str(Path.home() / ".doener_versions.json")))

# Der Token wird - in dieser Reihenfolge - gesucht in:
#   1. Umgebungsvariable GITHUB_TOKEN / GH_TOKEN
#   2. token.txt direkt neben diesem Script   (per .gitignore geschuetzt)
#   3. /etc/doener/github_token   (systemd-Setup auf dem Pi)
#   4. ~/.doener_gh_token
# So laeuft das Script ohne env-Gefrickel, sobald token.txt daneben liegt -
# der Wert bleibt aber ausserhalb des Codes und wird nie mitcommittet.
TOKEN_FILES = [
    Path(__file__).with_name("token.txt"),
    Path("/etc/doener/github_token"),
    Path.home() / ".doener_gh_token",
]

log = logging.getLogger("watcher")


# --------------------------------------------------------------------------- HTTP
def http(url: str, *, method: str = "GET", body: Optional[bytes] = None,
         headers: Optional[dict] = None) -> Tuple[int, bytes]:
    """Ein HTTP-Request. Gibt (status, body) zurueck - auch bei 4xx/5xx."""
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
    """http() mit Backoff. Wirft erst nach HTTP_RETRIES Fehlversuchen."""
    last = None
    for attempt in range(HTTP_RETRIES):
        try:
            status, data = http(url, **kw)
            # 5xx ist ein Kandidat fuer einen Retry, 4xx nicht.
            if status < 500:
                return status, data
            last = RuntimeError(f"HTTP {status} von {url}")
        except Exception as e:                                  # noqa: BLE001
            last = e
        if attempt < HTTP_RETRIES - 1:
            wait = (2 ** attempt) + random.uniform(0, 0.7)
            log.debug("Retry %d fuer %s in %.1fs (%s)", attempt + 1, url, wait, last)
            time.sleep(wait)
    raise last if last else RuntimeError(f"unbekannter Fehler bei {url}")


# --------------------------------------------------------------------------- Quellen
def fetch_supported() -> str:
    """GUID der Version, fuer die die Offsets gelten."""
    status, data = http_retry(URL_SUPPORTED)
    if status != 200:
        raise RuntimeError(f"offsets.imtheo.lol antwortete mit HTTP {status}")
    guid = data.decode("utf-8", "replace").strip()
    if not guid.startswith("version-"):
        raise RuntimeError(f"unerwartete Antwort von offsets.imtheo.lol: {guid[:80]!r}")
    return guid


def fetch_current() -> Tuple[str, str]:
    """(GUID, lesbare Version) der aktuellen Roblox-Windows-Version."""
    status, data = http_retry(URL_CURRENT)
    if status != 200:
        raise RuntimeError(f"clientsettingscdn antwortete mit HTTP {status}")
    j = json.loads(data.decode("utf-8", "replace"))
    guid  = str(j.get("clientVersionUpload", "")).strip()
    human = str(j.get("version", "")).strip()
    if not guid.startswith("version-"):
        raise RuntimeError(f"clientVersionUpload fehlt/ungueltig: {j!r}")
    return guid, human


# --------------------------------------------------------------------------- GUID -> Version Cache
def load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:                                      # noqa: BLE001
        log.warning("Cache %s nicht lesbar (%s) - starte leer", CACHE_PATH, e)
        return {}


def save_cache(cache: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, indent=2, ensure_ascii=False), "utf-8")
        tmp.replace(CACHE_PATH)                                  # atomar
    except Exception as e:                                       # noqa: BLE001
        log.warning("Cache %s nicht schreibbar: %s", CACHE_PATH, e)


# --------------------------------------------------------------------------- GitHub
class GitHub:
    """Minimaler Contents-API-Client. Haelt sha + Inhalt im Speicher."""

    API = "https://api.github.com"

    def __init__(self, repo: str, branch: str, token: Optional[str], dry_run: bool):
        self.repo    = repo
        self.branch  = branch
        self.token   = token
        self.dry_run = dry_run
        self.state: dict[str, Tuple[Optional[str], Optional[str]]] = {}  # path -> (sha, content)

    def _headers(self) -> dict:
        h = {"Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def list_dir(self) -> list:
        """Wurzelverzeichnis auflisten. Liefert je Eintrag u.a. name, sha und size.
        Das ist der guenstigste Weg an den Blob-SHA der 10-MB-Exe zu kommen."""
        url = f"{self.API}/repos/{self.repo}/contents/?ref={self.branch}"
        status, data = http_retry(url, headers=self._headers())
        if status != 200:
            raise RuntimeError(f"GET /: HTTP {status} - {data[:200]!r}")
        return json.loads(data.decode("utf-8"))

    def load(self, path: str) -> Tuple[Optional[str], Optional[str]]:
        """Aktuellen Stand einer Datei holen. (sha, text) - (None, None) wenn es sie nicht gibt."""
        url = f"{self.API}/repos/{self.repo}/contents/{quote(path)}?ref={self.branch}"
        status, data = http_retry(url, headers=self._headers())
        if status == 404:
            log.info("%s existiert noch nicht - wird angelegt", path)
            self.state[path] = (None, None)
            return None, None
        if status != 200:
            raise RuntimeError(f"GET {path}: HTTP {status} - {data[:200]!r}")
        j = json.loads(data.decode("utf-8"))
        text = base64.b64decode(j.get("content", "")).decode("utf-8", "replace")
        self.state[path] = (j.get("sha"), text)
        return j.get("sha"), text

    def put(self, path: str, text: str, message: str) -> bool:
        """Schreibt, wenn sich der Inhalt geaendert hat. True = es wurde committet."""
        sha, cur = self.state.get(path, (None, None))
        if cur == text:
            return False

        if self.dry_run:
            log.info("[dry-run] wuerde %s schreiben: %r", path, text)
            self.state[path] = (sha, text)
            return True

        if not self.token:
            raise RuntimeError("kein GITHUB_TOKEN gesetzt - Schreiben nicht moeglich")

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

        # 409/422: unser sha ist veraltet (jemand hat dazwischen gepusht) -> neu laden, einmal wiederholen
        if status in (409, 422):
            log.warning("%s: sha veraltet (HTTP %d) - lade neu und versuche erneut", path, status)
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


# --------------------------------------------------------------------------- Token
def read_token() -> Optional[str]:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok.strip()
    for p in TOKEN_FILES:
        try:
            t = p.read_text("utf-8").strip()
            if t:
                log.info("Token aus %s gelesen", p)
                return t
        except OSError:
            continue
    return None


# --------------------------------------------------------------------------- Logik
def build_content(guid: str, human: str) -> str:
    return f"{guid}\n{human}\n" if human else f"{guid}\n"


def next_version(prev: Optional[str]) -> str:
    """v0.1 -> v0.2 -> ... -> v0.9 -> v0.10 (Minor wird einfach hochgezaehlt)."""
    if not prev:
        return "v0.1"
    m = re.match(r"v(\d+)\.(\d+)\s*$", prev.strip())
    if not m:
        return "v0.1"
    return f"v{m.group(1)}.{int(m.group(2)) + 1}"


def check_releases(gh: GitHub) -> None:
    """Erkennt an der Blob-SHA, ob eine neue Doener.exe hochgeladen wurde, und
    haengt in dem Fall einen Eintrag an das Release-Log an.

    Die SHA ist inhaltsbasiert - der Dateiname bleibt ja immer gleich."""
    exe = next((e for e in gh.list_dir() if e.get("name") == FILE_EXE), None)
    if not exe:
        log.warning("%s liegt nicht im Repo", FILE_EXE)
        return
    sha, size = exe.get("sha"), exe.get("size", 0)

    _, raw = gh.state.get(FILE_RELEASES, (None, None))
    if raw is None:
        _, raw = gh.load(FILE_RELEASES)

    try:
        doc = json.loads(raw) if raw and raw.strip() else {}
    except ValueError:
        log.warning("%s ist kein gueltiges JSON - wird neu aufgebaut", FILE_RELEASES)
        doc = {}

    releases = doc.get("releases") or []
    if releases and releases[-1].get("sha") == sha:
        return                                    # unveraendert

    version = next_version(releases[-1].get("version") if releases else None)
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
        log.info("-> neue Doener-Version protokolliert: %s (sha %s)", version, sha[:10])


def check_once(gh: GitHub, cache: dict) -> None:
    """Ein Durchlauf: Quellen lesen, bei Aenderung nach GitHub schreiben."""
    guid_map = cache.setdefault("guid_map", {})

    cur_guid, cur_human = fetch_current()
    sup_guid            = fetch_supported()

    # Zuordnung GUID -> lesbare Version merken; so kennen wir sie spaeter
    # auch fuer eine "supported"-Version, die nicht mehr die aktuelle ist.
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
        log.info("-> %s aktualisiert", FILE_SUPPORTED)
        wrote = True
    if gh.put(FILE_CURRENT, build_content(cur_guid, cur_human),
              f"current: {cur_human or cur_guid}"):
        log.info("-> %s aktualisiert", FILE_CURRENT)
        wrote = True
    if not wrote:
        log.debug("keine Roblox-Aenderung")

    check_releases(gh)


def main() -> int:
    ap = argparse.ArgumentParser(description="Roblox-Version-Watcher fuer den Doener-Loader")
    ap.add_argument("--interval", type=int, default=INTERVAL,
                    help=f"Sekunden zwischen zwei Checks (Standard: {INTERVAL})")
    ap.add_argument("--once", action="store_true", help="nur einmal pruefen, dann beenden")
    ap.add_argument("--dry-run", action="store_true", help="nichts nach GitHub schreiben")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout)

    token = read_token()
    if not token and not args.dry_run:
        log.error("Kein Token gefunden. GITHUB_TOKEN setzen oder %s anlegen.",
                  " / ".join(str(p) for p in TOKEN_FILES))
        return 2

    cache = load_cache()
    gh = GitHub(REPO, BRANCH, token, args.dry_run)

    # Startzustand der beiden Dateien einmalig holen -> danach nur noch
    # GitHub-Requests, wenn sich wirklich etwas geaendert hat.
    for path in (FILE_SUPPORTED, FILE_CURRENT, FILE_RELEASES):
        try:
            gh.load(path)
        except Exception as e:                                   # noqa: BLE001
            log.error("Startzustand von %s nicht ladbar: %s", path, e)
            return 3

    if args.once:
        try:
            check_once(gh, cache)
            return 0
        except Exception as e:                                   # noqa: BLE001
            log.error("Durchlauf fehlgeschlagen: %s", e)
            return 1

    log.info("Watcher laeuft. Intervall %ds, Repo %s@%s%s",
             args.interval, REPO, BRANCH, "  [DRY-RUN]" if args.dry_run else "")

    fails = 0
    while True:
        try:
            check_once(gh, cache)
            fails = 0
        except KeyboardInterrupt:
            log.info("beendet")
            return 0
        except Exception as e:                                   # noqa: BLE001
            fails += 1
            log.error("Durchlauf fehlgeschlagen (%d in Folge): %s", fails, e)
            # Bei wiederholten Fehlern langsamer werden, aber nie aufgeben.
            time.sleep(min(300, 10 * fails))
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            log.info("beendet")
            return 0


if __name__ == "__main__":
    sys.exit(main())
