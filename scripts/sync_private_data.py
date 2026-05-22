"""
Download .json (and .json.gz) data from a private GitHub repo (Contents API) into DATA_DIR.
Configure via env: GITHUB_TOKEN, GITHUB_DATA_REPO, GITHUB_DATA_BRANCH, GITHUB_DATA_SUBDIR, DATA_DIR, REQUIRE_PRIVATE_DATA.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote


def env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def github_headers(token: str | None) -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "bijbelapi-data-sync",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def github_request_json(url: str, token: str | None) -> dict | list:
    req = urllib.request.Request(url, headers=github_headers(token))
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def github_list_dir(repo: str, branch: str, dir_path: str, token: str | None) -> list[dict]:
    """
    List directory entries via GitHub Contents API.
    dir_path: path within repo, e.g. "data/commentaries"
    """
    parts = [quote(p, safe="") for p in dir_path.split("/") if p]
    encoded = "/".join(parts)
    if encoded:
        url = f"https://api.github.com/repos/{repo}/contents/{encoded}?ref={quote(branch)}"
    else:
        url = f"https://api.github.com/repos/{repo}/contents?ref={quote(branch)}"
    payload = github_request_json(url, token)
    if isinstance(payload, list):
        return payload
    return []


# Folder-based sources that have been consolidated into single JSON files.
# The sync script skips these directories so only the consolidated file is downloaded.
# Paths are relative to GITHUB_DATA_SUBDIR (e.g. "data").
SKIP_FOLDER_SOURCES: set[str] = {
    "heilige_schrift_1917",
    "canisiusbijbel",
    "commentaries/dachsel",
}


def github_collect_json_files(
    repo: str,
    branch: str,
    subdir: str,
    token: str | None,
    skip_dirs: set[str] | None = None,
) -> list[str]:
    """
    Recursively collect JSON file paths beneath `subdir`.
    Returns repo-relative file paths, e.g. data/commentaries/matthew_henry_nl.json
    skip_dirs: set of directory paths relative to subdir to skip entirely.
    """
    skip_dirs = set(skip_dirs or [])
    prefix = (subdir.strip("/") + "/") if subdir.strip("/") else ""
    queue: list[str] = [subdir] if subdir else [""]
    out: list[str] = []
    seen: set[str] = set()
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        entries = github_list_dir(repo, branch, current, token)
        for item in entries:
            item_type = item.get("type")
            item_path = str(item.get("path", "")).strip("/")
            if not item_path:
                continue
            if item_type == "dir":
                rel = item_path[len(prefix):] if prefix and item_path.startswith(prefix) else item_path
                if rel in skip_dirs:
                    print(f"[data-sync] skip folder (consolidated): {item_path}")
                    continue
                queue.append(item_path)
                continue
            if item_type != "file":
                continue
            if item_path.endswith(".json") or item_path.endswith(".json.gz"):
                out.append(item_path)
    return out


def github_fetch_raw_file(repo: str, branch: str, file_path: str, token: str | None) -> bytes:
    """
    Fetch file bytes via Contents API (works for private repos where download_url is null).
    file_path: path within repo, e.g. data/basisbijbel.json
    """
    # Encode each path segment; keep slashes
    parts = [quote(p, safe="") for p in file_path.split("/") if p]
    encoded = "/".join(parts)
    url = f"https://api.github.com/repos/{repo}/contents/{encoded}?ref={quote(branch)}"
    h = dict(github_headers(token))
    h["Accept"] = "application/vnd.github.raw"
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=300) as resp:
        return resp.read()


def main() -> int:
    repo = os.getenv("GITHUB_DATA_REPO", "").strip()
    branch = os.getenv("GITHUB_DATA_BRANCH", "main").strip()
    subdir = os.getenv("GITHUB_DATA_SUBDIR", "").strip().strip("/")
    data_dir = Path(os.getenv("DATA_DIR", str(Path.cwd() / "private-data"))).resolve()
    token = os.getenv("GITHUB_TOKEN", "").strip() or None
    require = env_bool("REQUIRE_PRIVATE_DATA", False)

    if not repo:
        print("[data-sync] SKIP: GITHUB_DATA_REPO niet gezet — geen synchronisatie.")
        return 0

    data_dir.mkdir(parents=True, exist_ok=True)

    path_segment = f"/{subdir}" if subdir else ""
    print(f"[data-sync] Ophalen inhoud van {repo}@{branch}{path_segment or '/'} …")

    if not token:
        print("[data-sync] WAARSCHUWING: GITHUB_TOKEN ontbreekt — private repo's falen waarschijnlijk.")

    try:
        files = github_collect_json_files(repo, branch, subdir, token, skip_dirs=SKIP_FOLDER_SOURCES)
    except urllib.error.HTTPError as e:
        print(f"[data-sync] ERROR: GitHub HTTP {e.code}: {e.reason}")
        if require:
            return 1
        return 0
    except Exception as e:
        print(f"[data-sync] ERROR: {e}")
        if require:
            return 1
        return 0

    if not files:
        print("[data-sync] WAARSCHUWING: geen JSON-bestanden gevonden in opgegeven subdir.")

    downloaded = 0
    subdir_norm = subdir.strip("/")
    prefix = f"{subdir_norm}/" if subdir_norm else ""
    for rel_path in files:
        rel_path_norm = rel_path.strip("/")
        if prefix and rel_path_norm.startswith(prefix):
            target_rel = rel_path_norm[len(prefix):]
        else:
            target_rel = rel_path_norm
        dest = data_dir / target_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"[data-sync] download {rel_path} -> {dest}")
        try:
            body = github_fetch_raw_file(repo, branch, rel_path_norm, token)
            dest.write_bytes(body)
            downloaded += 1
        except Exception as e:
            print(f"[data-sync] ERROR bij {rel_path_norm}: {e}")
            if require:
                return 1

    print(f"[data-sync] klaar: {downloaded} bestand(en).")
    if downloaded == 0 and require:
        print("[data-sync] ERROR: geen JSON gedownload terwijl REQUIRE_PRIVATE_DATA=true")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
