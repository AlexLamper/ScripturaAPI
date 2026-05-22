# main.py
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi import Security, Depends
from fastapi.security import APIKeyHeader
from bson import ObjectId

import billing_mongo as billing_db
import json
import os
import re
import gzip
import random
import hashlib
import math
from datetime import date, datetime, timezone
from xml.sax.saxutils import escape as xml_escape
from typing import Any, Optional
from dotenv import load_dotenv
import stripe
import uuid
from parsing.book_normalization import resolve_book_name_for_data

# SlowAPI imports voor rate limiting
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

import logging

# Configure logging for analytics
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# Define Base Directory for absolute paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# .env = gedeelde secrets; .env.local = lokale overrides (o.a. Stripe test) — niet committen
load_dotenv(os.path.join(BASE_DIR, ".env"))
load_dotenv(os.path.join(BASE_DIR, ".env.local"), override=True)
DEFAULT_DATA_DIR = os.path.join(BASE_DIR, "data")
# Sync script (Render) schrijft standaard naar private-data onder project root
PRIVATE_DATA_FALLBACK = os.path.join(BASE_DIR, "private-data")
DATA_DIR = os.getenv("DATA_DIR", DEFAULT_DATA_DIR)

app = FastAPI(
    title="BijbelAPI",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# SlowAPI limiter setup
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# API key headers must be defined before route declarations
api_key_header = APIKeyHeader(name="x-api-key")
optional_api_key_header = APIKeyHeader(name="x-api-key", auto_error=False)

# Custom error handler for better error messages
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request, exc):
    logging.warning(f"HTTPException: {exc.status_code} {exc.detail} - {request.url}")
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.status_code, "message": exc.detail},
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    logging.warning(f"ValidationError: {exc.errors()} - {request.url}")
    return JSONResponse(
        status_code=422,
        content={"error": 422, "message": "Validatiefout", "details": exc.errors()},
    )

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID_PRO_MONTHLY = os.getenv("STRIPE_PRICE_ID_PRO_MONTHLY", "")
STRIPE_PRICE_ID_PRO_YEARLY = os.getenv("STRIPE_PRICE_ID_PRO_YEARLY", "")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:8081")
BILLING_ENFORCED = os.getenv("BILLING_ENFORCED", "false").lower() == "true"
DEBUG_BILLING = os.getenv("DEBUG_BILLING", "false").lower() == "true"
# Uitgebreide billing-/Stripe-stappen in logs (Render: zet aan tijdens testen; geen volledige secrets).
BILLING_TRACE_LOG = os.getenv("BILLING_TRACE_LOG", "false").lower() == "true"
# Optioneel: log elke geslaagde API-request met Pro-key (veel logvolume; alleen tijdelijk aanzetten).
BILLING_TRACE_REQUESTS = os.getenv("BILLING_TRACE_REQUESTS", "false").lower() == "true"
# Optioneel diagnostisch endpoint voor Mongo-connectiviteit — standaard uit (niet voor publiek gebruik).
EXPOSE_MONGO_STATUS = os.getenv("EXPOSE_MONGO_STATUS", "false").lower() == "true"


def _billing_should_trace() -> bool:
    return BILLING_TRACE_LOG or DEBUG_BILLING


def _mask_secret(value: str, visible: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= visible * 2:
        return "*" * len(value)
    return f"{value[:visible]}...{value[-visible:]}"


def _mask_email_for_log(email: Optional[str]) -> str:
    if not email or "@" not in email:
        return "(geen)"
    local, _, domain = email.partition("@")
    if len(local) <= 1:
        return f"*@{domain}"
    return f"{local[0]}***@{domain}"


def _mask_api_key_for_log(key: Optional[str]) -> str:
    if not key:
        return "(geen)"
    if len(key) <= 8:
        return "***"
    return f"{key[:6]}…{key[-4:]}"


def billing_trace(msg: str, **kwargs: Any) -> None:
    if not _billing_should_trace():
        return
    if kwargs:
        tail = " ".join(f"{k}={v}" for k, v in kwargs.items())
        logging.info(f"[billing-trace] {msg} | {tail}")
    else:
        logging.info(f"[billing-trace] {msg}")


def public_base_url() -> str:
    """Canonical origin for sitemap, robots, OG URLs (override with CANONICAL_PUBLIC_URL)."""
    return (os.getenv("CANONICAL_PUBLIC_URL") or os.getenv("APP_BASE_URL") or "https://bijbelapi.com").rstrip("/")


# (path, priority 0..1, changefreq) — keep tight; no infinite API query URLs
_SITEMAP_ENTRIES = (
    ("/", "1.0", "weekly"),
    ("/docs", "0.85", "weekly"),
    ("/privacy.html", "0.35", "yearly"),
    ("/bron.html", "0.45", "yearly"),
)

_HTML_INCLUDE_PATTERN = re.compile(r"<!--\s*include:\s*([a-zA-Z0-9_./-]+)\s*-->")


def _expand_html_includes(html: str, site_dir: str, *, depth: int = 0) -> str:
    """
    Inline <!-- include: path/relative/to/site/dir.html --> markers (recursive).
    Paths must stay under site_dir; used for splitting large pages like index.html.
    """
    if depth > 16:
        raise HTTPException(status_code=500, detail="HTML-include keten te diep")
    site_root = os.path.abspath(site_dir)

    def _replace(match: re.Match[str]) -> str:
        rel = match.group(1).strip().replace("\\", "/")
        if not rel.endswith(".html") or rel.startswith("/") or ".." in rel.split("/"):
            raise HTTPException(status_code=500, detail="Ongeldige HTML-include")
        full_path = os.path.abspath(os.path.join(site_root, *rel.split("/")))
        if not full_path.startswith(site_root + os.sep):
            raise HTTPException(status_code=500, detail="HTML-include buiten site-map")
        if not os.path.isfile(full_path):
            raise HTTPException(status_code=500, detail=f"Ontbrekende partial: {rel}")
        with open(full_path, encoding="utf-8") as inc_f:
            inner = inc_f.read()
        return _expand_html_includes(inner, site_dir, depth=depth + 1)

    return _HTML_INCLUDE_PATTERN.sub(_replace, html)


def _inject_canonical_html(relative_path: str) -> HTMLResponse:
    path = os.path.join(BASE_DIR, "site", relative_path)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Pagina niet gevonden")
    with open(path, encoding="utf-8") as f:
        html = f.read()
    site_dir = os.path.join(BASE_DIR, "site")
    if "<!-- include:" in html:
        html = _expand_html_includes(html, site_dir)
    html = html.replace("{{CANONICAL_ORIGIN}}", public_base_url())
    html = html.replace("{{FREE_TIER_DAILY_LIMIT}}", f"{FREE_TIER_DAILY_LIMIT:,}".replace(",", "."))
    html = html.replace("{{PRO_TIER_DAILY_LIMIT}}", f"{PRO_TIER_DAILY_LIMIT:,}".replace(",", "."))
    return HTMLResponse(content=html, media_type="text/html; charset=utf-8")


FREE_TIER_DAILY_LIMIT = int(os.getenv("FREE_TIER_DAILY_LIMIT", "50"))
PRO_TIER_DAILY_LIMIT = int(os.getenv("PRO_TIER_DAILY_LIMIT", "100000"))
stripe.api_key = STRIPE_SECRET_KEY
FREE_TIER_USAGE: dict[tuple[str, str], int] = {}
PRO_TIER_USAGE: dict[tuple[str, str], int] = {}
FREE_TIER_MINUTE_USAGE: dict[tuple[str, int, str], int] = {}
PRO_TIER_MINUTE_USAGE: dict[tuple[str, int, str], int] = {}
PRO_MINUTE_LIMIT_MULTIPLIER = max(2, int(os.getenv("PRO_MINUTE_LIMIT_MULTIPLIER", "30")))

FREE_ROUTE_LIMITS_PER_MINUTE: dict[str, int] = {
    "/api/random": 20,
    "/api/verse": 30,
    "/api/passage": 10,
    "/api/books": 30,
    "/api/chapters": 30,
    "/api/verses": 30,
    "/api/search": 10,
    "/api/daytext": 5,
    "/api/chapter": 20,
    "/api/commentary": 20,
    "/api/parse/reference": 20,
    "/api/parse/references": 10,
}

# CORS settings
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static files from /site
app.mount("/site", __import__("fastapi.staticfiles", fromlist=["StaticFiles"]).StaticFiles(directory=os.path.join(BASE_DIR, "site")), name="site")
app.mount("/public", __import__("fastapi.staticfiles", fromlist=["StaticFiles"]).StaticFiles(directory=os.path.join(BASE_DIR, "public")), name="public")

# Simple analytics middleware (log endpoint and IP)
@app.middleware("http")
async def log_requests(request: Request, call_next):
    ip = request.client.host
    path = request.url.path
    logging.info(f"Request: {ip} {path}")
    response = await call_next(request)
    return response

# --- Multi-version support for Bible texts ---
DEFAULT_TRANSLATION = "sv"
SUPPORTED_TRANSLATIONS = {"sv", "hs1917", "canisius"}
LEGACY_TRANSLATIONS = {"asv", "kjv"}
ENGLISH_COMMENTARY_KEYS = {"matthew-henry"}
TRANSLATION_ALIASES = {
    "statenvertaling": "sv",
    "staten vertaling": "sv",
    "stve": "sv",
    "heilige-schrift": "hs1917",
    "heilige-schrift-1917": "hs1917",
    "heilige_schrift_1917": "hs1917",
    "canisiusbijbel": "canisius",
}
COMMENTARY_SOURCE_ALIASES = {
    "matthew-henry": "matthew_henry_nl",
    "matthew-henry-nl": "matthew_henry_nl",
    "dachsel": "dachsel",
}
FOLDER_TRANSLATIONS = {
    "heilige_schrift_1917": {
        "key": "hs1917",
        "meta": {
            "name": "De Heilige Schrift (1917)",
            "shortname": "hs1917",
            "module": "hs1917",
            "lang": "nl",
            "year": 1917,
            "description": "Rooms-Katholieke bijbelvertaling uit 1917",
        },
    },
    "canisiusbijbel": {
        "key": "canisius",
        "meta": {
            "name": "Canisiusbijbel",
            "shortname": "canisius",
            "module": "canisius",
            "lang": "nl",
            "year": 1939,
            "description": "Rooms-Katholieke Canisiusbijbel vertaling",
        },
    },
}
FOLDER_COMMENTARIES = {
    "dachsel": {
        "key": "dachsel",
        "meta": {
            "id": "dachsel",
            "name": "Karl August Dachsel Bijbelcommentaar",
            "lang": "nl",
        },
    },
}

def _bible_data_candidate_dirs() -> list[str]:
    out: list[str] = []
    for d in [DATA_DIR, DEFAULT_DATA_DIR, PRIVATE_DATA_FALLBACK]:
        if d not in out:
            out.append(d)
    return out


def _any_bible_json_locally() -> bool:
    for d in _bible_data_candidate_dirs():
        try:
            if os.path.isdir(d) and any(name.endswith(".json") for name in os.listdir(d)):
                return True
        except OSError:
            continue
    return False


def _maybe_sync_private_repo_bible_data() -> None:
    """
    Als er nog geen .json bijbelbestanden lokaal staan maar GITHUB_DATA_REPO wel gezet is,
    voer sync_private_data.py uit (zelfde als op Render vóór uvicorn).
    Vereist voor private repos: GITHUB_TOKEN in .env.
    """
    if os.getenv("SKIP_PRIVATE_DATA_SYNC", "").strip().lower() in ("1", "true", "yes", "on"):
        return
    if _any_bible_json_locally():
        return
    repo = os.getenv("GITHUB_DATA_REPO", "").strip()
    if not repo:
        logging.info(
            "Geen lokale bijbel-JSON en GITHUB_DATA_REPO ontbreekt — sync overgeslagen. "
            "Zet GITHUB_DATA_REPO (+ GITHUB_TOKEN) of plaats .json in data/ of private-data/."
        )
        return
    script_path = os.path.join(BASE_DIR, "scripts", "sync_private_data.py")
    if not os.path.isfile(script_path):
        logging.warning("[data-sync] Script niet gevonden: %s", script_path)
        return
    logging.info("[data-sync] Geen lokale JSON; start sync uit GitHub-repo %s", repo)
    import importlib.util

    spec = importlib.util.spec_from_file_location("bijbelapi_sync_private_data", script_path)
    if spec is None or spec.loader is None:
        logging.error("[data-sync] Kan module niet laden")
        return
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        rc = int(mod.main())
        if rc != 0:
            logging.warning("[data-sync] Script exit code %s (controleer token, repo, branch, subdir)", rc)
        else:
            logging.info("[data-sync] Klaar.")
    except Exception as e:
        logging.exception("[data-sync] Sync mislukt: %s", e)


def _load_folder_based_translation(trans_dir: str) -> dict:
    """Load a translation stored as one folder per book, one JSON file per chapter."""
    structured = {}
    if not os.path.isdir(trans_dir):
        return structured
    for book_name in os.listdir(trans_dir):
        book_dir = os.path.join(trans_dir, book_name)
        if not os.path.isdir(book_dir):
            continue
        chapters: dict = {}
        for fname in os.listdir(book_dir):
            if fname == "chapters.json" or not fname.endswith(".json"):
                continue
            match = re.search(r'(\d+)$', fname[:-5])
            if not match:
                continue
            chapter_num = match.group(1)
            path = os.path.join(book_dir, fname)
            try:
                with open(path, encoding="utf-8") as f:
                    verses = json.load(f)
                chapters[chapter_num] = {str(k): str(v) for k, v in verses.items()}
            except Exception as e:
                logging.warning(f"Failed to load chapter file {path}: {e}")
        if chapters:
            structured[book_name] = chapters
    return structured


def load_folder_based_versions() -> dict:
    """Load all translations defined in FOLDER_TRANSLATIONS from candidate data dirs."""
    candidate_dirs = _bible_data_candidate_dirs()
    new_versions: dict = {}
    for folder_name, info in FOLDER_TRANSLATIONS.items():
        key = info["key"]
        meta = info["meta"]
        for base_dir in candidate_dirs:
            trans_dir = os.path.join(base_dir, folder_name)
            if os.path.isdir(trans_dir):
                data = _load_folder_based_translation(trans_dir)
                if data:
                    new_versions[key] = {"meta": meta, "data": data}
                    logging.info(f"Loaded folder-based translation '{key}' from {trans_dir} ({len(data)} books)")
                    break
        else:
            logging.warning(f"Folder-based translation '{folder_name}' not found in: {candidate_dirs}")
    return new_versions


def load_all_versions():
    candidate_dirs = _bible_data_candidate_dirs()

    versions_dir = None
    for d in candidate_dirs:
        if not os.path.isdir(d):
            continue
        has_json = any(name.endswith(".json") for name in os.listdir(d))
        if has_json:
            versions_dir = d
            break

    if versions_dir is None:
        # Keep previous behavior but with explicit warning: no usable data dir found.
        versions_dir = DATA_DIR if os.path.isdir(DATA_DIR) else DEFAULT_DATA_DIR
        logging.warning(
            f"Geen bruikbare data-map met JSON-bestanden gevonden. "
            f"Geprobeerd: {candidate_dirs}. Gebruik: '{versions_dir}'."
        )
    elif versions_dir != DATA_DIR:
        logging.warning(
            f"DATA_DIR '{DATA_DIR}' is leeg of onbruikbaar, fallback naar '{versions_dir}'."
        )

    versions = {}
    if not os.path.isdir(versions_dir):
        logging.warning(f"Versions dir '{versions_dir}' not found.")
        return versions
    for filename in os.listdir(versions_dir):
        if filename.endswith(".json"):
            version_name = filename.replace(".json", "")
            path = os.path.join(versions_dir, filename)
            try:
                with open(path, encoding="utf-8") as f:
                    raw_data = json.load(f)
            except Exception as e:
                logging.warning(f"Failed to load version file {path}: {e}")
                continue
            structured_data = {}
            books = raw_data.get("books")
            if isinstance(books, dict) and books:
                for book_name, book_obj in books.items():
                    structured_data[book_name] = {}
                    chapters = book_obj.get("chapters") or {}
                    if not isinstance(chapters, dict):
                        continue
                    for ch_key, ch_obj in chapters.items():
                        ch = str(ch_key)
                        structured_data[book_name][ch] = {}
                        verses_map = (ch_obj or {}).get("verses") or {}
                        if not isinstance(verses_map, dict):
                            continue
                        for v_key, text in verses_map.items():
                            structured_data[book_name][ch][str(v_key)] = text
            else:
                for verse in raw_data.get("verses", []):
                    book = verse.get("book_name")
                    chapter = str(verse.get("chapter"))
                    verse_number = str(verse.get("verse"))
                    text = verse.get("text")
                    if book not in structured_data:
                        structured_data[book] = {}
                    if chapter not in structured_data[book]:
                        structured_data[book][chapter] = {}
                    structured_data[book][chapter][verse_number] = text
            normalized_name = version_name.lower()
            canonical_name = TRANSLATION_ALIASES.get(normalized_name, normalized_name)
            if normalized_name in LEGACY_TRANSLATIONS:
                logging.info(f"Sla legacy vertaling over: {version_name}")
                continue
            if canonical_name not in SUPPORTED_TRANSLATIONS:
                logging.info(f"Sla niet-ondersteunde vertaling over: {version_name}")
                continue
            meta = raw_data.get("metadata")
            if meta:
                meta_out = meta
            else:
                meta_out = {}
                if raw_data.get("name"):
                    meta_out["name"] = raw_data["name"]
                if raw_data.get("id"):
                    meta_out["shortname"] = raw_data["id"]
                    meta_out["module"] = raw_data["id"]
                if raw_data.get("name") or raw_data.get("id"):
                    meta_out["lang"] = "nl"
            versions[canonical_name] = {
                "meta": meta_out,
                "data": structured_data
            }
    folder_based_keys = {info["key"] for info in FOLDER_TRANSLATIONS.values()}
    missing_translations = [
        code for code in sorted(SUPPORTED_TRANSLATIONS)
        if code not in folder_based_keys and not any(key.lower() == code for key in versions.keys())
    ]
    if missing_translations:
        logging.warning(f"Ontbrekende verplichte vertalingen: {', '.join(missing_translations)}")
    return versions

_maybe_sync_private_repo_bible_data()
all_versions = load_all_versions()
all_versions.update(load_folder_based_versions())

def get_version_key(version: str):
    version = version.lower()
    version = TRANSLATION_ALIASES.get(version, version)
    if version in LEGACY_TRANSLATIONS:
        return None
    for key, v in all_versions.items():
        if key.lower() not in SUPPORTED_TRANSLATIONS:
            continue
        meta = v.get("meta", {})
        if (
            key.lower() == version
            or meta.get("shortname", "").lower() == version
            or meta.get("module", "").lower() == version
            or meta.get("name", "").lower() == version
        ):
            return key
    return None

def resolve_version_key(version: str):
    """
    Resolve requested translation with safe fallbacks.
    Avoids unnecessary 404s when requested/default translation is missing.
    """
    version_key = get_version_key(version)
    if version_key:
        return version_key

    default_key = get_version_key(DEFAULT_TRANSLATION)
    if default_key:
        logging.info(f"Gevraagde vertaling '{version}' niet gevonden; fallback naar default '{default_key}'.")
        return default_key

    if all_versions:
        fallback_key = next(iter(all_versions.keys()))
        logging.warning(f"Gevraagde vertaling '{version}' niet gevonden; fallback naar beschikbare vertaling '{fallback_key}'.")
        return fallback_key

    raise HTTPException(status_code=503, detail="Geen vertalingen beschikbaar")

def normalize_book_name(version_key, book_name):
    data = all_versions.get(version_key, {}).get("data", {})
    return resolve_book_name_for_data(book_name, data)

# --- Commentary loading (Matthew Henry, etc.) ---
COMMENTARIES_DIR = os.path.join(BASE_DIR, "commentaries")


def _commentary_candidate_dirs() -> list[str]:
    out: list[str] = []
    for d in [
        os.path.join(DATA_DIR, "commentaries"),
        os.path.join(DEFAULT_DATA_DIR, "commentaries"),
        os.path.join(PRIVATE_DATA_FALLBACK, "commentaries"),
        COMMENTARIES_DIR,
    ]:
        if d not in out:
            out.append(d)
    return out

def load_commentaries():
    commentaries = {}
    candidate_dirs = _commentary_candidate_dirs()
    found_any_dir = False

    folder_commentary_names = set(FOLDER_COMMENTARIES.keys())

    # Walk through all candidate directories to find JSON files
    for base_dir in candidate_dirs:
        if not os.path.isdir(base_dir):
            continue
        found_any_dir = True
        for root, dirs, files in os.walk(base_dir):
            # Skip directories that are handled as folder-based commentaries
            dirs[:] = [d for d in dirs if d not in folder_commentary_names]
            for fname in files:
                path = os.path.join(root, fname)
                raw = None
                try:
                    if fname.endswith(".json"):
                        with open(path, encoding="utf-8") as f:
                            raw = json.load(f)
                    elif fname.endswith(".json.gz"):
                        with gzip.open(path, "rt", encoding="utf-8") as f:
                            raw = json.load(f)
                    else:
                        continue

                    if raw:
                        # identify key: meta.id or filename without ext
                        key = raw.get("meta", {}).get("id") or fname.replace(".json", "").replace(".gz", "")
                        if key in ENGLISH_COMMENTARY_KEYS or "\\en\\" in path.replace("/", "\\"):
                            logging.info(f"Sla Engelse commentary over: {key}")
                            continue
                        if key in commentaries:
                            logging.info(f"Sla duplicate commentary over: {key} ({path})")
                            continue
                        commentaries[key] = raw
                        logging.info(f"Loaded commentary '{key}' from {path}")
                except Exception as e:
                    logging.warning(f"Failed to load commentary file {path}: {e}")

    # Load folder-based commentaries (e.g. dachsel: one folder per book, one file per chapter)
    for base_dir in candidate_dirs:
        if not os.path.isdir(base_dir):
            continue
        for folder_name, info in FOLDER_COMMENTARIES.items():
            source_key = info["key"]
            if source_key in commentaries:
                continue
            folder_path = os.path.join(base_dir, folder_name)
            if not os.path.isdir(folder_path):
                continue
            books: dict = {}
            for book_name in os.listdir(folder_path):
                book_dir = os.path.join(folder_path, book_name)
                if not os.path.isdir(book_dir):
                    continue
                chapters: dict = {}
                for fname in os.listdir(book_dir):
                    if fname == "chapters.json" or not fname.endswith(".json"):
                        continue
                    match = re.search(r'(\d+)$', fname[:-5])
                    if not match:
                        continue
                    chapter_num = match.group(1)
                    path = os.path.join(book_dir, fname)
                    try:
                        with open(path, encoding="utf-8") as f:
                            verses = json.load(f)
                        chapters[chapter_num] = {str(k): str(v) for k, v in verses.items()}
                    except Exception as e:
                        logging.warning(f"Failed to load commentary file {path}: {e}")
                if chapters:
                    books[book_name] = {"chapters": chapters}
            if books:
                commentaries[source_key] = {"meta": info["meta"], "books": books}
                logging.info(f"Loaded folder-based commentary '{source_key}' from {folder_path} ({len(books)} books)")

    if not found_any_dir:
        logging.warning(
            f"Geen commentaries-map gevonden. Geprobeerd: {candidate_dirs}"
        )
    return commentaries

all_commentaries = load_commentaries()

def normalize_commentary_book(source_key: str, book_name: str):
    """
    Try to match book_name (e.g. Dutch 'Johannes' or English 'John') to an entry in the commentary file.
    Uses the same token/id resolution as bible text endpoints; commentary JSON keys are often English.
    Returns the stored book key (e.g. 'John') or None.
    """
    src = all_commentaries.get(source_key)
    if not src:
        return None
    books = src.get("books", {})
    resolved = resolve_book_name_for_data(book_name, books)
    if resolved:
        return resolved
    # Fallback: explicit id field in commentary entries
    for name, info in books.items():
        if info.get("id", "").lower() == book_name.lower():
            return name
    return None

# --- SEO: crawlers expect these at site root (not only under /site/)
@app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
@app.head("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
def robots_txt():
    base = public_base_url()
    body = "\n".join(
        [
            "User-agent: *",
            "Allow: /",
            "",
            f"Sitemap: {base}/sitemap.xml",
            "",
        ]
    )
    return PlainTextResponse(body, media_type="text/plain; charset=utf-8")


@app.get("/sitemap.xml", include_in_schema=False)
@app.head("/sitemap.xml", include_in_schema=False)
def sitemap_xml():
    base = public_base_url()
    lastmod = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for path, priority, changefreq in _SITEMAP_ENTRIES:
        loc = f"{base}{path}"
        lines.append("  <url>")
        lines.append(f"    <loc>{xml_escape(loc)}</loc>")
        lines.append(f"    <lastmod>{lastmod}</lastmod>")
        lines.append(f"    <changefreq>{changefreq}</changefreq>")
        lines.append(f"    <priority>{priority}</priority>")
        lines.append("  </url>")
    lines.append("</urlset>")
    xml_body = "\n".join(lines)
    return Response(content=xml_body, media_type="application/xml; charset=utf-8")


@app.get("/humans.txt", response_class=PlainTextResponse, include_in_schema=False)
@app.head("/humans.txt", response_class=PlainTextResponse, include_in_schema=False)
def humans_txt():
    path = os.path.join(BASE_DIR, "site", "humans.txt")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="humans.txt niet gevonden")
    with open(path, encoding="utf-8") as f:
        return PlainTextResponse(f.read(), media_type="text/plain; charset=utf-8")


@app.get("/favicon.ico", include_in_schema=False)
@app.head("/favicon.ico", include_in_schema=False)
def favicon():
    """
    Serve favicon from a stable root path expected by browsers.
    """
    candidate_paths = [
        os.path.join(BASE_DIR, "public", "favicon", "favicon.ico"),
        os.path.join(BASE_DIR, "favicon.ico"),
    ]
    for path in candidate_paths:
        if os.path.exists(path):
            return FileResponse(path, media_type="image/x-icon")
    raise HTTPException(status_code=404, detail="favicon.ico niet gevonden")


# --- Serve index.html on /
# HEAD is required for Cloudflare and many probes; GET-only returns 405.
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
@app.head("/", response_class=HTMLResponse, include_in_schema=False)
def serve_index():
    return _inject_canonical_html("index.html")


@app.get("/privacy.html", response_class=HTMLResponse, include_in_schema=False)
@app.head("/privacy.html", response_class=HTMLResponse, include_in_schema=False)
def privacy_page():
    return _inject_canonical_html("privacy.html")


@app.get("/bron.html", response_class=HTMLResponse, include_in_schema=False)
@app.head("/bron.html", response_class=HTMLResponse, include_in_schema=False)
def bron_page():
    return _inject_canonical_html("bron.html")


@app.get("/health")
@app.head("/health")
def health():
    return {"status": "ok"}


@app.get("/api/mongo-status", include_in_schema=False)
@app.head("/api/mongo-status", include_in_schema=False)
@limiter.limit("20/minute")
def mongo_status_public(request: Request):
    """MongoDB-connectiviteit; alleen actief bij EXPOSE_MONGO_STATUS=true (niet voor publiek scraping)."""
    if not EXPOSE_MONGO_STATUS:
        raise HTTPException(status_code=404, detail="Niet gevonden")
    return billing_db.ping_mongo()

# --- Existing Bible endpoints (unchanged) ---
@app.get("/api/random")
def get_random_verse(request: Request, version: str = DEFAULT_TRANSLATION, x_api_key: Optional[str] = Security(optional_api_key_header)):
    ensure_paid_access(request, x_api_key)
    version_key = resolve_version_key(version)
    data = all_versions[version_key]["data"]
    book = random.choice(list(data.keys()))
    chapter = random.choice(list(data[book].keys()))
    verse = random.choice(list(data[book][chapter].keys()))
    return {
        "version": version_key,
        "book": book,
        "chapter": chapter,
        "verse": verse,
        "text": data[book][chapter][verse],
    }

@app.get("/api/verse")
def get_verse(book: str, chapter: str, verse: str, request: Request, version: str = DEFAULT_TRANSLATION, x_api_key: Optional[str] = Security(optional_api_key_header)):
    ensure_paid_access(request, x_api_key)
    version_key = resolve_version_key(version)
    data = all_versions[version_key]["data"]
    book_key = normalize_book_name(version_key, book)
    if not book_key:
        raise HTTPException(status_code=404, detail="Boek niet gevonden")
    try:
        text = data[book_key][chapter][verse]
        return {
            "version": version_key,
            "book": book_key,
            "chapter": chapter,
            "verse": verse,
            "text": text,
        }
    except KeyError:
        raise HTTPException(status_code=404, detail="Vers niet gevonden")

@app.get("/api/passage")
def get_passage(book: str, chapter: str, start: int, end: int, request: Request, version: str = DEFAULT_TRANSLATION, x_api_key: Optional[str] = Security(optional_api_key_header)):
    ensure_paid_access(request, x_api_key)
    version_key = resolve_version_key(version)
    data = all_versions[version_key]["data"]
    book_key = normalize_book_name(version_key, book)
    if not book_key:
        raise HTTPException(status_code=404, detail="Boek niet gevonden")
    try:
        verses = []
        for i in range(start, end + 1):
            verses.append({"verse": str(i), "text": data[book_key][str(chapter)][str(i)]})
        return {
            "version": version_key,
            "book": book_key,
            "chapter": chapter,
            "verses": verses,
        }
    except KeyError:
        raise HTTPException(status_code=404, detail="Passage niet gevonden")

@app.get("/api/books")
def get_books(request: Request, version: str = DEFAULT_TRANSLATION, x_api_key: Optional[str] = Security(optional_api_key_header)):
    ensure_paid_access(request, x_api_key)
    version_key = resolve_version_key(version)
    return list(all_versions[version_key]["data"].keys())

@app.get("/api/chapters")
def get_chapters(book: str, request: Request, version: str = DEFAULT_TRANSLATION, x_api_key: Optional[str] = Security(optional_api_key_header)):
    ensure_paid_access(request, x_api_key)
    version_key = resolve_version_key(version)
    data = all_versions[version_key]["data"]
    book_key = normalize_book_name(version_key, book)
    if not book_key:
        raise HTTPException(status_code=404, detail="Boek niet gevonden")
    return list(data[book_key].keys())

@app.get("/api/verses")
def get_verses(book: str, chapter: str, request: Request, version: str = DEFAULT_TRANSLATION, x_api_key: Optional[str] = Security(optional_api_key_header)):
    ensure_paid_access(request, x_api_key)
    version_key = resolve_version_key(version)
    data = all_versions[version_key]["data"]
    book_key = normalize_book_name(version_key, book)
    if not book_key:
        raise HTTPException(status_code=404, detail="Boek niet gevonden")
    try:
        return list(data[book_key][chapter].keys())
    except KeyError:
        raise HTTPException(status_code=404, detail="Hoofdstuk niet gevonden")

@app.get("/api/search")
def search_verses(request: Request, query: str = Query(..., min_length=1), version: str = DEFAULT_TRANSLATION, x_api_key: Optional[str] = Security(optional_api_key_header)):
    ensure_paid_access(request, x_api_key)
    version_key = resolve_version_key(version)
    data = all_versions[version_key]["data"]
    results = []
    for book, chapters in data.items():
        for chapter, verses in chapters.items():
            for verse_number, text in verses.items():
                if query.lower() in text.lower():
                    results.append({
                        "book": book,
                        "chapter": chapter,
                        "verse": verse_number,
                        "text": text,
                    })
    return results

@app.get("/api/daytext")
def get_daytext(request: Request, seed: str = None, version: str = DEFAULT_TRANSLATION, x_api_key: Optional[str] = Security(optional_api_key_header)):
    ensure_paid_access(request, x_api_key)
    version_key = resolve_version_key(version)
    data = all_versions[version_key]["data"]
    books = list(data.keys())
    base = seed if seed else date.today().isoformat()
    hash_val = int(hashlib.sha256(base.encode()).hexdigest(), 16)
    random.seed(hash_val)
    book = random.choice(books)
    chapter = random.choice(list(data[book].keys()))
    verse = random.choice(list(data[book][chapter].keys()))
    return {
        "version": version_key,
        "book": book,
        "chapter": chapter,
        "verse": verse,
        "text": data[book][chapter][verse],
    }

@app.get("/api/versions")
@limiter.limit("10/minute")
def get_versions(request: Request):
    # Return metadata for all versions
    return [
        {
            "key": k,
            "name": v.get("meta", {}).get("name", k),
            "shortname": v.get("meta", {}).get("shortname"),
            "module": v.get("meta", {}).get("module"),
            "lang": v.get("meta", {}).get("lang"),
            "year": v.get("meta", {}).get("year"),
            "description": v.get("meta", {}).get("description"),
        }
        for k, v in all_versions.items()
    ]

@app.get("/api/chapter")
def get_chapter(book: str, chapter: str, request: Request, version: str = DEFAULT_TRANSLATION, x_api_key: Optional[str] = Security(optional_api_key_header)):
    ensure_paid_access(request, x_api_key)
    version_key = resolve_version_key(version)
    data = all_versions[version_key]["data"]
    book_key = normalize_book_name(version_key, book)
    if not book_key:
        raise HTTPException(status_code=404, detail="Boek niet gevonden")
    try:
        return {
            "version": version_key,
            "book": book_key,
            "chapter": chapter,
            "verses": data[book_key][chapter],
        }
    except KeyError:
        raise HTTPException(status_code=404, detail="Hoofdstuk niet gevonden")

# --- COMMENTARY ENDPOINT ---
@app.get("/api/commentary")
def get_commentary(request: Request, source: str, book: str, chapter: str, verse: str = None, x_api_key: Optional[str] = Security(optional_api_key_header)):
    ensure_paid_access(request, x_api_key)
    """
    Returns commentary for a chapter or specific verse.

    Example:
    /api/commentary?source=matthew-henry&book=Genesis&chapter=5
    -> { "1": "...", "2": "...", ... }

    /api/commentary?source=matthew-henry&book=Genesis&chapter=5&verse=3
    -> { "3": "..." }
    """
    source_key = COMMENTARY_SOURCE_ALIASES.get(source, source)
    src = all_commentaries.get(source_key)
    if not src:
        raise HTTPException(status_code=404, detail="Broncommentaar niet gevonden")
    book_key = normalize_commentary_book(source_key, book)
    if not book_key:
        raise HTTPException(status_code=404, detail="Boek niet gevonden in commentaar")
    chapters = src.get("books", {}).get(book_key, {}).get("chapters", {})
    if chapter not in chapters:
        raise HTTPException(status_code=404, detail="Hoofdstuk niet gevonden in commentaar")
    verses = chapters[chapter]  # dict of verse -> text
    if verse:
        if verse not in verses:
            raise HTTPException(status_code=404, detail="Vers niet gevonden in commentaar")
        return {verse: verses[verse]}
    return verses

@app.on_event("startup")
def _startup_billing_mongo():
    config_error = billing_db.mongo_config_error()
    if config_error:
        logging.warning("%s", config_error)
        return
    try:
        billing_db.ensure_billing_indexes()
        source = billing_db.get_mongo_uri_source() or "MONGODB_URI"
        logging.info("MongoDB billing indexes OK (%s, via %s)", billing_db.get_mongo_db_name(), source)
    except Exception as e:
        logging.exception("MongoDB index-init mislukt: %s", e)


def _require_mongo_configured():
    config_error = billing_db.mongo_config_error()
    if config_error:
        raise HTTPException(
            status_code=503,
            detail=config_error,
        )


def is_valid_key_in_db(key: str):
    if not billing_db.get_mongo_uri():
        return False
    try:
        billing_db.ensure_billing_indexes()
        db = billing_db.get_billing_db()
        return billing_db.mongo_find_active_api_key_by_secret(db, key) is not None
    except Exception as e:
        logging.warning("is_valid_key_in_db: %s", e)
        return False


def verify_api_key(key: str = Security(api_key_header)):
    _require_mongo_configured()
    if not is_valid_key_in_db(key):
        raise HTTPException(status_code=403, detail="Ongeldige of verlopen sleutel")


def _request_client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return "unknown"


def _enforce_free_tier_limit(request: Request) -> None:
    today = date.today().isoformat()
    ip = _request_client_ip(request)
    usage_key = (ip, today)
    count = FREE_TIER_USAGE.get(usage_key, 0) + 1
    FREE_TIER_USAGE[usage_key] = count
    if count > FREE_TIER_DAILY_LIMIT:
        billing_trace(
            "free_tier_geblokkeerd",
            ip=ip,
            count=count,
            limiet=FREE_TIER_DAILY_LIMIT,
            path=str(request.url.path),
        )
        raise HTTPException(
            status_code=429,
            detail=(
                f"Gratis limiet bereikt ({FREE_TIER_DAILY_LIMIT} req/dag per IP). "
                "Upgrade naar Pro voor 100.000 req/dag — vanaf \u20ac9,99/maand op bijbelapi.com."
            ),
        )


def _enforce_pro_tier_limit(request: Request, api_key: str) -> None:
    today = date.today().isoformat()
    token = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:32]
    usage_key = (token, today)
    count = PRO_TIER_USAGE.get(usage_key, 0) + 1
    PRO_TIER_USAGE[usage_key] = count
    if count > PRO_TIER_DAILY_LIMIT:
        billing_trace(
            "pro_tier_geblokkeerd",
            count=count,
            limiet=PRO_TIER_DAILY_LIMIT,
            path=str(request.url.path),
            api_key=_mask_api_key_for_log(api_key),
        )
        raise HTTPException(
            status_code=429,
            detail=(
                "Pro fair-use limiet voor vandaag bereikt. Neem contact op bij ongebruikelijk hoge load "
                "of probeer morgen opnieuw."
            ),
        )


def _route_minute_limit(path: str) -> int:
    if path in FREE_ROUTE_LIMITS_PER_MINUTE:
        return FREE_ROUTE_LIMITS_PER_MINUTE[path]
    if path.startswith("/api/parse/reference/"):
        return FREE_ROUTE_LIMITS_PER_MINUTE["/api/parse/reference"]
    return 30


def _enforce_tiered_minute_limit(request: Request, key: Optional[str]) -> None:
    path = str(request.url.path)
    free_limit = _route_minute_limit(path)
    bucket = int(math.floor(datetime.utcnow().timestamp() / 60))

    if key:
        token = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        usage_key = (token, bucket, path)
        count = PRO_TIER_MINUTE_USAGE.get(usage_key, 0) + 1
        PRO_TIER_MINUTE_USAGE[usage_key] = count
        pro_limit = free_limit * PRO_MINUTE_LIMIT_MULTIPLIER
        if count > pro_limit:
            raise HTTPException(
                status_code=429,
                detail=f"Pro limiet bereikt: {pro_limit} requests per minuut voor dit endpoint.",
            )
        return

    ip = _request_client_ip(request)
    usage_key = (ip, bucket, path)
    count = FREE_TIER_MINUTE_USAGE.get(usage_key, 0) + 1
    FREE_TIER_MINUTE_USAGE[usage_key] = count
    if count > free_limit:
        raise HTTPException(
            status_code=429,
            detail=f"Free limiet bereikt: {free_limit} requests per minuut voor dit endpoint.",
        )


def ensure_paid_access(request: Request, key: Optional[str]) -> None:
    """
    Enforce billing entitlement when BILLING_ENFORCED=true.
    """
    if not BILLING_ENFORCED:
        return
    if not key:
        _enforce_tiered_minute_limit(request, None)
        _enforce_free_tier_limit(request)
        return

    _require_mongo_configured()
    billing_db.ensure_billing_indexes()
    db = billing_db.get_billing_db()
    api_key_doc = billing_db.mongo_find_active_api_key_by_secret(db, key)
    if not api_key_doc:
        billing_trace(
            "ensure_paid_access: geweigerd ongeldige_sleutel",
            path=str(request.url.path),
            api_key=_mask_api_key_for_log(key),
        )
        raise HTTPException(status_code=403, detail="Ongeldige of verlopen sleutel")
    subscription = billing_db.mongo_find_latest_subscription_by_api_key_id(db, api_key_doc["_id"])
    if not subscription or subscription.get("status") not in {"active", "trialing"}:
        billing_trace(
            "ensure_paid_access: geweigerd geen_actief_abonnement",
            path=str(request.url.path),
            api_key=_mask_api_key_for_log(key),
            subscription_status=(subscription or {}).get("status"),
        )
        raise HTTPException(status_code=402, detail="Geen actief abonnement")
    _enforce_tiered_minute_limit(request, key)
    _enforce_pro_tier_limit(request, key)
    if BILLING_TRACE_REQUESTS:
        billing_trace(
            "ensure_paid_access: ok_pro_request",
            path=str(request.url.path),
            api_key=_mask_api_key_for_log(key),
            subscription_status=subscription.get("status"),
            plan=subscription.get("plan_name"),
        )

@app.get("/secure-data", include_in_schema=False)
@limiter.limit("10/minute")
def secure_data(request: Request, _: str = Depends(verify_api_key)):
    return {"message": "Je bent geauthenticeerd!"}
# --- einde authenticatie ---

# Import parsing modules
from parsing.reference_parser import ReferenceParser
from pydantic import BaseModel

# Pydantic models for parsing requests
class ParseRequest(BaseModel):
    reference: str
    version: str = DEFAULT_TRANSLATION

class ParseMultipleRequest(BaseModel):
    references: list[str]
    version: str = DEFAULT_TRANSLATION


class CheckoutSessionRequest(BaseModel):
    email: str
    plan: str = "pro_monthly"


class PortalSessionRequest(BaseModel):
    email: str

# Parsing endpoints
@app.post("/api/parse/reference")
def parse_reference(request: Request, parse_req: ParseRequest, x_api_key: Optional[str] = Security(optional_api_key_header)):
    """Parse a single Bible reference with complex parsing support."""
    try:
        ensure_paid_access(request, x_api_key)
        version_key = resolve_version_key(parse_req.version)
        parser = ReferenceParser(all_versions=all_versions, version=version_key)
        result = parser.parse(parse_req.reference, version_key)
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/parse/reference/{reference}")
def parse_single_reference(request: Request, reference: str, version: str = DEFAULT_TRANSLATION, x_api_key: Optional[str] = Security(optional_api_key_header)):
    """Parse a single Bible reference via GET request."""
    try:
        ensure_paid_access(request, x_api_key)
        version_key = resolve_version_key(version)
        parser = ReferenceParser(all_versions=all_versions, version=version_key)
        result = parser.parse(reference, version_key)
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/parse/references")
def parse_multiple_references(request: Request, parse_req: ParseMultipleRequest, x_api_key: Optional[str] = Security(optional_api_key_header)):
    """Parse multiple Bible references with complex parsing support."""
    try:
        ensure_paid_access(request, x_api_key)
        version_key = resolve_version_key(parse_req.version)
        parser = ReferenceParser(all_versions=all_versions, version=version_key)
        results = []
        for reference in parse_req.references:
            result = parser.parse(reference, version_key)
            results.append(result)
        return {"references": results}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


def get_price_id_for_plan(plan: str) -> str:
    plan_map = {
        "pro_monthly": STRIPE_PRICE_ID_PRO_MONTHLY,
        "pro_yearly": STRIPE_PRICE_ID_PRO_YEARLY,
    }
    price_id = plan_map.get(plan)
    if not price_id:
        raise HTTPException(status_code=400, detail="Onbekend of niet geconfigureerd prijsplan")
    return price_id


def _ensure_local_billing_debug_access(request: Request) -> None:
    if not DEBUG_BILLING:
        raise HTTPException(status_code=404, detail="Niet gevonden")
    host = _request_client_ip(request)
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(status_code=403, detail="Alleen lokaal beschikbaar")


def record_processed_event(event_id: str, event_type: str) -> bool:
    """True wanneer event al eerder verwerkt is."""
    _require_mongo_configured()
    billing_db.ensure_billing_indexes()
    db = billing_db.get_billing_db()
    return billing_db.mongo_record_webhook_event(db, event_id, event_type)


@app.post("/billing/checkout-session", include_in_schema=False)
@limiter.limit("10/minute")
def create_checkout_session(request: Request, payload: CheckoutSessionRequest):
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe is niet geconfigureerd")
    price_id = get_price_id_for_plan(payload.plan)
    billing_trace(
        "checkout_session:start",
        email=_mask_email_for_log(payload.email),
        plan=payload.plan,
        price_id=price_id,
    )
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=payload.email,
            metadata={"plan": payload.plan},
            success_url=f"{APP_BASE_URL.rstrip('/')}/?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{APP_BASE_URL.rstrip('/')}/?checkout=cancelled",
        )
        logging.info(f"Stripe checkout session created: id={session.id} email={payload.email} plan={payload.plan}")
        billing_trace(
            "checkout_session:stripe_ok",
            stripe_session_id=session.id,
            email=_mask_email_for_log(payload.email),
            plan=payload.plan,
            heeft_checkout_url=bool(session.url),
        )
        return {"checkout_url": session.url, "session_id": session.id}
    except Exception as exc:
        billing_trace(
            "checkout_session:stripe_fout",
            email=_mask_email_for_log(payload.email),
            plan=payload.plan,
            fout=str(exc)[:300],
        )
        logging.exception(f"Stripe checkout failed for email={payload.email} plan={payload.plan}")
        raise HTTPException(status_code=400, detail=f"Stripe checkout mislukt: {exc}")


@app.post("/billing/portal-session", include_in_schema=False)
@limiter.limit("10/minute")
def create_portal_session(request: Request, payload: PortalSessionRequest):
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe is niet geconfigureerd")
    billing_trace("portal_session:start", email=_mask_email_for_log(payload.email))
    try:
        customers = stripe.Customer.list(email=payload.email, limit=1)
        if not customers.data:
            billing_trace(
                "portal_session:geen_stripe_klant",
                email=_mask_email_for_log(payload.email),
            )
            raise HTTPException(status_code=404, detail="Geen Stripe-klant gevonden voor dit e-mailadres")
        portal = stripe.billing_portal.Session.create(
            customer=customers.data[0].id,
            return_url=APP_BASE_URL.rstrip("/"),
        )
        logging.info(f"Stripe portal session created for email={payload.email}")
        billing_trace(
            "portal_session:stripe_ok",
            email=_mask_email_for_log(payload.email),
            customer_id_tail=customers.data[0].id[-10:] if customers.data[0].id else "",
            heeft_portal_url=bool(portal.url),
        )
        return {"portal_url": portal.url}
    except HTTPException:
        raise
    except Exception as exc:
        billing_trace(
            "portal_session:fout",
            email=_mask_email_for_log(payload.email),
            fout=str(exc)[:300],
        )
        logging.exception(f"Stripe portal failed for email={payload.email}")
        raise HTTPException(status_code=400, detail=f"Stripe portal mislukt: {exc}")


@app.get("/billing/checkout-success", include_in_schema=False)
@limiter.limit("30/minute")
def billing_checkout_success(request: Request, session_id: str = Query(..., min_length=10, max_length=200)):
    """Poll na redirect van Stripe: levert API-sleutel zodra webhook + Mongo klaar zijn."""
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe is niet geconfigureerd")
    sid = session_id.strip()
    if not sid.startswith("cs_"):
        raise HTTPException(status_code=400, detail="Ongeldige checkout-sessie")
    billing_trace("checkout_success:poll_start", session_id_tail=sid[-14:])
    try:
        sess = stripe.checkout.Session.retrieve(sid)
    except Exception as exc:
        logging.warning("checkout-success retrieve failed: %s", exc)
        billing_trace("checkout_success:stripe_retrieve_fout", session_id_tail=sid[-14:], fout=str(exc)[:180])
        raise HTTPException(status_code=400, detail="Sessie niet gevonden of verlopen")
    plain = _stripe_to_plain(sess)
    if not isinstance(plain, dict):
        raise HTTPException(status_code=500, detail="Kon Stripe-sessie niet verwerken")
    if plain.get("mode") != "subscription":
        raise HTTPException(status_code=400, detail="Geen abonnements-checkout")
    if plain.get("payment_status") != "paid" or plain.get("status") != "complete":
        billing_trace(
            "checkout_success:nog_niet_betaald_of_complete",
            payment_status=str(plain.get("payment_status")),
            status=str(plain.get("status")),
            session_id_tail=sid[-14:],
        )
        return {
            "ready": False,
            "message": "Betaling nog niet afgerond. Vernieuw over een moment.",
        }
    email = _checkout_session_email(plain)
    if not email:
        billing_trace("checkout_success:geen_email_op_sessie", session_id_tail=sid[-14:])
        raise HTTPException(status_code=400, detail="Geen e-mailadres op de sessie")
    _require_mongo_configured()
    billing_db.ensure_billing_indexes()
    db = billing_db.get_billing_db()
    api_key_obj = billing_db.mongo_find_api_key_by_email(db, email)
    if not api_key_obj:
        billing_trace("checkout_success:api_key_nog_niet_gekoppeld", email=_mask_email_for_log(email))
        return {
            "ready": False,
            "message": "Je betaling wordt gekoppeld. Vernieuw over enkele seconden.",
        }
    sub = billing_db.mongo_find_latest_subscription_by_api_key_id(db, api_key_obj["_id"])
    active = bool(sub and sub.get("status") in {"active", "trialing"})
    if not active:
        billing_trace(
            "checkout_success:abonnement_nog_niet_actief",
            email=_mask_email_for_log(email),
            status=(sub or {}).get("status"),
        )
        return {
            "ready": False,
            "message": "Abonnement wordt nog geactiveerd. Vernieuw over enkele seconden.",
        }
    billing_trace(
        "checkout_success:ready",
        email=_mask_email_for_log(email),
        plan=(sub or {}).get("plan_name"),
        session_id_tail=sid[-14:],
    )
    return {
        "ready": True,
        "api_key": api_key_obj["api_key"],
        "email_masked": _mask_email_public(str(email)),
        "billing_email": str(email),
        "plan": sub.get("plan_name") if sub else None,
    }


@app.get("/billing/status", include_in_schema=False)
@limiter.limit("30/minute")
def billing_status(request: Request, key: str = Security(api_key_header)):
    billing_trace("billing_status:request", api_key=_mask_api_key_for_log(key))
    _require_mongo_configured()
    billing_db.ensure_billing_indexes()
    db = billing_db.get_billing_db()
    api_key = billing_db.mongo_find_api_key_by_secret(db, key)
    if not api_key:
        billing_trace("billing_status:onbekende_sleutel", api_key=_mask_api_key_for_log(key))
        raise HTTPException(status_code=404, detail="API-sleutel niet gevonden")
    subscription = billing_db.mongo_find_latest_subscription_by_api_key_id(db, api_key["_id"])
    if not subscription:
        billing_trace(
            "billing_status:geen_subscription_row",
            email=_mask_email_for_log(api_key.get("user_email")),
        )
        return {
            "active": False,
            "plan": "free",
            "status": "inactive",
            "email_masked": _mask_email_public(str(api_key.get("user_email") or "")),
        }
    cpe = subscription.get("current_period_end")
    raw_email = str(api_key.get("user_email") or "")
    body = {
        "active": subscription.get("status") in {"active", "trialing"},
        "plan": subscription.get("plan_name"),
        "status": subscription.get("status"),
        "current_period_end": cpe.isoformat() if hasattr(cpe, "isoformat") else (cpe if isinstance(cpe, str) else None),
        "email_masked": _mask_email_public(raw_email) if raw_email else "",
    }
    billing_trace(
        "billing_status:response",
        email=_mask_email_for_log(api_key.get("user_email")),
        active=body["active"],
        plan=body["plan"],
        status=body["status"],
    )
    return body


@app.get("/billing/plans", include_in_schema=False)
@limiter.limit("30/minute")
def billing_plans(request: Request):
    return {
        "currency": "eur",
        "free": {
            "price_monthly": 0,
            "daily_request_limit": FREE_TIER_DAILY_LIMIT,
            "requires_api_key": False,
            "description": "Generous free tier met daglimiet per IP.",
        },
        "pro_monthly": {
            "price_id": STRIPE_PRICE_ID_PRO_MONTHLY or "configure_in_env",
            "daily_request_limit": PRO_TIER_DAILY_LIMIT,
            "requires_api_key": True,
        },
        "pro_yearly": {
            "price_id": STRIPE_PRICE_ID_PRO_YEARLY or "configure_in_env",
            "daily_request_limit": PRO_TIER_DAILY_LIMIT,
            "requires_api_key": True,
        },
        "billing_enforced": BILLING_ENFORCED,
    }


def _stripe_to_plain(value: Any) -> Any:
    """Stripe Webhook.construct_event returns nested StripeObject; normalize for dict-style .get()."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_stripe_to_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _stripe_to_plain(v) for k, v in value.items()}
    raw = getattr(value, "_data", None)
    if isinstance(raw, dict):
        return {k: _stripe_to_plain(v) for k, v in raw.items()}
    return value


def _normalize_stripe_expandable_id(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, str):
        return val
    if isinstance(val, dict) and val.get("id"):
        return str(val["id"])
    return None


def _webhook_subscription_id_from_object(data: dict) -> Optional[str]:
    """Resolve Stripe subscription id from webhook object; never use invoice id as subscription id."""
    if not isinstance(data, dict):
        return None
    obj = data.get("object")
    sub = _normalize_stripe_expandable_id(data.get("subscription"))
    if sub:
        return sub
    if obj == "subscription":
        return _normalize_stripe_expandable_id(data.get("id"))
    if obj == "invoice" and data.get("id") and STRIPE_SECRET_KEY:
        try:
            inv = stripe.Invoice.retrieve(str(data["id"]))
            inv_plain = _stripe_to_plain(inv)
            if isinstance(inv_plain, dict):
                return _normalize_stripe_expandable_id(inv_plain.get("subscription"))
        except Exception as ex:
            billing_trace("stripe_webhook:invoice_subscription_lookup", err=str(ex)[:200])
        return None
    return None


def _mask_email_public(email: str) -> str:
    if not email or "@" not in email:
        return ""
    local, _, domain = email.partition("@")
    if not local:
        return f"***@{domain}"
    if len(local) <= 2:
        return f"{local[0]}***@{domain}"
    return f"{local[0]}***{local[-1]}@{domain}"


def _checkout_session_email(data: dict) -> Optional[str]:
    """Stripe zet niet altijd `customer_email`; val terug op customer_details.email."""
    if not isinstance(data, dict):
        return None
    em = data.get("customer_email")
    if em:
        return str(em)
    details = data.get("customer_details")
    if isinstance(details, dict) and details.get("email"):
        return str(details["email"])
    return None


@app.post("/stripe/webhook", include_in_schema=False)
@limiter.limit("500/minute")
async def stripe_webhook(request: Request):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="STRIPE_WEBHOOK_SECRET ontbreekt")
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        billing_trace("stripe_webhook:handtekening_mislukt", fout=str(e)[:200])
        logging.warning(f"Stripe webhook signature verify failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))

    event = _stripe_to_plain(event)
    if not isinstance(event, dict):
        raise HTTPException(status_code=500, detail="Ongeldig Stripe event-formaat")

    _require_mongo_configured()
    billing_db.ensure_billing_indexes()
    db = billing_db.get_billing_db()

    event_id = event.get("id")
    event_type = event.get("type")
    if not event_id or not event_type:
        raise HTTPException(status_code=400, detail="Ongeldig Stripe event")
    logging.info(f"Stripe webhook received: id={event_id} type={event_type}")
    billing_trace("stripe_webhook:parsed", event_id=event_id, event_type=event_type)
    if record_processed_event(event_id, event_type):
        billing_trace("stripe_webhook:duplicate_skip", event_id=event_id, event_type=event_type)
        return {"status": "al_verwerkt"}

    data = event.get("data", {}).get("object", {})
    if not isinstance(data, dict):
        data = {}

    stripe_customer_id = data.get("customer")
    if isinstance(stripe_customer_id, dict) and stripe_customer_id.get("id"):
        stripe_customer_id = stripe_customer_id["id"]
    stripe_subscription_id = _webhook_subscription_id_from_object(data)

    billing_trace(
        "stripe_webhook:object_samenvatting",
        event_type=event_type,
        object_type=data.get("object"),
        object_id=data.get("id"),
        mode=data.get("mode"),
        heeft_customer=bool(stripe_customer_id),
        heeft_subscription_id=bool(stripe_subscription_id),
    )

    email: Optional[str] = None
    if event_type == "checkout.session.completed":
        email = _checkout_session_email(data)
        if not email and stripe_customer_id and isinstance(stripe_customer_id, str) and STRIPE_SECRET_KEY:
            billing_trace(
                "stripe_webhook:checkout_email_uit_customer_halen",
                customer_tail=stripe_customer_id[-12:],
            )
            try:
                cust = stripe.Customer.retrieve(stripe_customer_id)
                email = getattr(cust, "email", None) or None
                if email:
                    billing_trace(
                        "stripe_webhook:email_via_customer_ok",
                        email=_mask_email_for_log(str(email)),
                    )
            except Exception as ex:
                billing_trace("stripe_webhook:customer_retrieve_fout", err=str(ex)[:200])

    plan_name = "pro"
    stripe_price_id = None
    items: list[Any] = []
    items_obj = data.get("items")
    if isinstance(items_obj, dict):
        items = list(items_obj.get("data") or [])
    elif isinstance(items_obj, list):
        items = list(items_obj)
    if not items:
        line_obj = data.get("line_items")
        if isinstance(line_obj, dict):
            items = list(line_obj.get("data") or [])
        elif isinstance(line_obj, list):
            items = list(line_obj)
    if items and isinstance(items[0], dict):
        price_obj = items[0].get("price")
        if isinstance(price_obj, dict):
            stripe_price_id = price_obj.get("id")
        elif isinstance(price_obj, str):
            stripe_price_id = price_obj
        if stripe_price_id == STRIPE_PRICE_ID_PRO_YEARLY:
            plan_name = "pro_yearly"
        else:
            plan_name = "pro_monthly"

    md = data.get("metadata")
    if isinstance(md, dict):
        mp = md.get("plan")
        if mp in ("pro_monthly", "pro_yearly"):
            plan_name = mp
            if not stripe_price_id:
                stripe_price_id = (
                    STRIPE_PRICE_ID_PRO_YEARLY if mp == "pro_yearly" else STRIPE_PRICE_ID_PRO_MONTHLY
                ) or None

    if event_type == "checkout.session.completed":
        if not email:
            billing_trace(
                "stripe_webhook:checkout_completed_GEEN_EMAIL",
                session_id=data.get("id"),
                customer_id=str(stripe_customer_id) if stripe_customer_id else "",
            )
        else:
            billing_trace(
                "stripe_webhook:checkout_verwerken",
                email=_mask_email_for_log(email),
                plan_name=plan_name,
                price_id=stripe_price_id or "(onbekend)",
                subscription_id_tail=str(stripe_subscription_id)[-14:] if stripe_subscription_id else "",
            )
            api_key_obj = billing_db.mongo_find_api_key_by_email(db, email)
            is_new_key = api_key_obj is None
            if not api_key_obj:
                api_key_obj = billing_db.mongo_insert_api_key(db, email, str(uuid.uuid4()), True)
            else:
                billing_db.mongo_set_api_key_active_by_email(db, email, True)
                api_key_obj = billing_db.mongo_find_api_key_by_email(db, email)

            billing_db.mongo_upsert_subscription_after_checkout(
                db,
                api_key_obj["_id"],
                stripe_customer_id,
                stripe_subscription_id,
                stripe_price_id,
                plan_name,
            )
            sub_after = billing_db.mongo_find_latest_subscription_by_api_key_id(db, api_key_obj["_id"])
            billing_trace(
                "stripe_webhook:checkout_db_ok",
                email=_mask_email_for_log(email),
                nieuwe_api_key=is_new_key,
                api_key_id=str(api_key_obj["_id"]),
                subscription_status=(sub_after or {}).get("status", ""),
            )

    elif event_type in {"customer.subscription.updated", "invoice.paid"}:
        billing_trace(
            "stripe_webhook:subscription_actief_houden",
            event_type=event_type,
            subscription_id_tail=str(stripe_subscription_id)[-14:] if stripe_subscription_id else "",
        )
        if stripe_subscription_id:
            subscription = billing_db.mongo_find_subscription_by_stripe_sub_id(db, stripe_subscription_id)
            if subscription:
                db.billing_subscriptions.update_one(
                    {"_id": subscription["_id"]},
                    {"$set": {"status": "active", "updated_at": datetime.utcnow()}},
                )
                billing_trace(
                    "stripe_webhook:subscription_geüpdatet",
                    db_subscription_id=str(subscription["_id"]),
                )
            else:
                billing_trace(
                    "stripe_webhook:subscription_niet_in_db",
                    subscription_id_tail=str(stripe_subscription_id)[-14:],
                )

    elif event_type in {"invoice.payment_failed", "customer.subscription.deleted"}:
        billing_trace(
            "stripe_webhook:negatieve_gebeurtenis",
            event_type=event_type,
            subscription_id_tail=str(stripe_subscription_id)[-14:] if stripe_subscription_id else "",
        )
        if stripe_subscription_id:
            subscription = billing_db.mongo_find_subscription_by_stripe_sub_id(db, stripe_subscription_id)
            if subscription:
                new_status = "past_due" if event_type == "invoice.payment_failed" else "canceled"
                billing_db.mongo_subscription_set_status(db, stripe_subscription_id, new_status)
                subscription = billing_db.mongo_find_subscription_by_stripe_sub_id(db, stripe_subscription_id)
                api_key_obj = None
                aid = subscription.get("api_key_id") if subscription else None
                if isinstance(aid, ObjectId):
                    api_key_obj = billing_db.mongo_find_api_key_by_id(db, aid)
                    billing_db.mongo_set_api_key_active_by_id(
                        db,
                        aid,
                        event_type != "customer.subscription.deleted",
                    )
                billing_trace(
                    "stripe_webhook:subscription_achterstallig_of_beeindigd",
                    nieuwe_status=new_status,
                    api_key_actief=bool(api_key_obj and event_type != "customer.subscription.deleted"),
                )
            else:
                billing_trace(
                    "stripe_webhook:negatief_event_geen_db_row",
                    subscription_id_tail=str(stripe_subscription_id)[-14:],
                )
    else:
        billing_trace("stripe_webhook:geen_handler_voor_event_type", event_type=event_type)

    billing_trace("stripe_webhook:klaar", event_id=event_id, event_type=event_type)
    return {"status": "succes"}


@app.get("/billing/debug/config", include_in_schema=False)
@limiter.limit("30/minute")
def billing_debug_config(request: Request):
    """
    Local debug endpoint for Stripe configuration checks.
    Enabled only when DEBUG_BILLING=true and localhost access.
    """
    _ensure_local_billing_debug_access(request)
    return {
        "billing_enforced": BILLING_ENFORCED,
        "debug_billing": DEBUG_BILLING,
        "app_base_url": APP_BASE_URL,
        "stripe_secret_key_set": bool(STRIPE_SECRET_KEY),
        "stripe_secret_key_masked": _mask_secret(STRIPE_SECRET_KEY),
        "stripe_webhook_secret_set": bool(STRIPE_WEBHOOK_SECRET),
        "stripe_webhook_secret_masked": _mask_secret(STRIPE_WEBHOOK_SECRET),
        "stripe_price_id_pro_monthly_set": bool(STRIPE_PRICE_ID_PRO_MONTHLY),
        "stripe_price_id_pro_monthly": STRIPE_PRICE_ID_PRO_MONTHLY or "",
        "stripe_price_id_pro_yearly_set": bool(STRIPE_PRICE_ID_PRO_YEARLY),
        "stripe_price_id_pro_yearly": STRIPE_PRICE_ID_PRO_YEARLY or "",
        "mongodb_uri_set": bool(billing_db.get_mongo_uri()),
        "mongodb_db_name": billing_db.get_mongo_db_name(),
    }


@app.get("/billing/debug/ping", include_in_schema=False)
@limiter.limit("60/minute")
def billing_debug_ping(request: Request):
    """
    Lightweight local debug ping for frontend diagnostics.
    Enabled only when DEBUG_BILLING=true and localhost access.
    """
    _ensure_local_billing_debug_access(request)
    return {
        "ok": True,
        "debug_billing": DEBUG_BILLING,
        "billing_enforced": BILLING_ENFORCED,
        "app_base_url": APP_BASE_URL,
    }


@app.get("/billing/debug/email-status", include_in_schema=False)
@limiter.limit("30/minute")
def billing_debug_email_status(request: Request, email: str):
    """
    Local debug endpoint: inspect API key/subscription state for email.
    Enabled only when DEBUG_BILLING=true and localhost access.
    """
    _ensure_local_billing_debug_access(request)
    _require_mongo_configured()
    billing_db.ensure_billing_indexes()
    db = billing_db.get_billing_db()
    api_key = billing_db.mongo_find_api_key_by_email(db, email)
    if not api_key:
        return {"email": email, "exists": False}
    subscription = billing_db.mongo_find_latest_subscription_by_api_key_id(db, api_key["_id"])
    cpe = (subscription or {}).get("current_period_end")
    return {
        "email": email,
        "exists": True,
        "api_key_active": bool(api_key.get("active")),
        "api_key_masked": _mask_secret(api_key.get("api_key", ""), visible=6),
        "subscription": None
        if not subscription
        else {
            "plan_name": subscription.get("plan_name"),
            "status": subscription.get("status"),
            "stripe_customer_id": subscription.get("stripe_customer_id"),
            "stripe_subscription_id": subscription.get("stripe_subscription_id"),
            "stripe_price_id": subscription.get("stripe_price_id"),
            "current_period_end": cpe.isoformat() if hasattr(cpe, "isoformat") else (cpe if isinstance(cpe, str) else None),
        },
    }
