"""
Download data from a private GitHub repo by fetching a single tarball archive.
One HTTP request instead of thousands — avoids GitHub API rate limits entirely.
Configure via env: GITHUB_TOKEN, GITHUB_DATA_REPO, GITHUB_DATA_BRANCH, GITHUB_DATA_SUBDIR, DATA_DIR, REQUIRE_PRIVATE_DATA.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote


# Folder-based sources that have been consolidated into single JSON files.
# These directories are skipped during extraction even if still present in the archive.
# Paths are relative to GITHUB_DATA_SUBDIR (e.g. "data").
SKIP_FOLDER_SOURCES: set[str] = {
    "heilige_schrift_1917",
    "canisiusbijbel",
    "commentaries/dachsel",
}


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


def _should_skip(file_rel: str, skip_dirs: set[str]) -> bool:
    """Return True if file_rel is inside one of the skip_dirs."""
    for skip in skip_dirs:
        if file_rel == skip or file_rel.startswith(skip + "/"):
            return True
    return False


def _download_tarball_bytes(repo: str, branch: str, token: str | None) -> bytes:
    """
    Fetch the repo tarball via the GitHub API.
    GitHub returns a 302 redirect to a CDN URL. We follow that redirect
    without auth headers (the CDN URL is pre-signed and doesn't need them,
    and sending an Authorization header to S3 can cause a 400 error).
    """
    api_url = f"https://api.github.com/repos/{repo}/tarball/{quote(branch)}"

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None  # prevent auto-follow so we can strip auth

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(api_url, headers=github_headers(token))

    download_url: str | None = None
    try:
        with opener.open(req, timeout=30) as resp:
            # No redirect — read directly (unlikely for GitHub tarballs)
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 307, 308):
            raise
        download_url = e.headers.get("Location")
        if not download_url:
            raise RuntimeError("GitHub redirect missing Location header")

    # Fetch the actual tarball from the CDN without auth headers
    cdn_req = urllib.request.Request(download_url, headers={"User-Agent": "bijbelapi-data-sync"})
    with urllib.request.urlopen(cdn_req, timeout=300) as resp:
        content_length = int(resp.headers.get("Content-Length", 0))
        if content_length:
            mb = content_length / 1024 / 1024
            print(f"[data-sync] Downloading {mb:.1f} MB …")
        else:
            print("[data-sync] Downloading archive …")
        return resp.read()


def download_and_extract(
    repo: str,
    branch: str,
    subdir: str,
    token: str | None,
    dest_dir: Path,
    skip_dirs: set[str],
) -> int:
    """
    Download the repo as a tarball and extract JSON files from subdir into dest_dir.
    Returns count of extracted files.
    """
    tarball = _download_tarball_bytes(repo, branch, token)
    print(f"[data-sync] Extracting files …")

    subdir_norm = subdir.strip("/")
    prefix = subdir_norm + "/" if subdir_norm else ""

    count = 0
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue

            # GitHub tarballs have a generated top-level dir, e.g.
            # "AlexLamper-bijbelapi-data-abc1234/data/statenvertaling.json"
            # Strip it to get the repo-relative path.
            parts = member.name.split("/", 1)
            if len(parts) < 2:
                continue
            repo_rel = parts[1]

            # Must be within our target subdir
            if prefix and not repo_rel.startswith(prefix):
                continue

            file_rel = repo_rel[len(prefix):]  # path relative to subdir
            if not file_rel:
                continue

            # Skip consolidated folder sources
            if _should_skip(file_rel, skip_dirs):
                continue

            # Only JSON files
            if not (file_rel.endswith(".json") or file_rel.endswith(".json.gz")):
                continue

            dest = dest_dir / file_rel
            dest.parent.mkdir(parents=True, exist_ok=True)

            f = tar.extractfile(member)
            if f:
                dest.write_bytes(f.read())
                print(f"[data-sync] extract {repo_rel} -> {dest}")
                count += 1

    return count


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
        count = download_and_extract(repo, branch, subdir, token, data_dir, SKIP_FOLDER_SOURCES)
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

    print(f"[data-sync] klaar: {count} bestand(en).")
    if count == 0 and require:
        print("[data-sync] ERROR: geen JSON gedownload terwijl REQUIRE_PRIVATE_DATA=true")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
