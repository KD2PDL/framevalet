"""External art sources: browse and import public/free images into the library.

Keyless out of the box: Openverse, NASA APOD, Reddit. Key-gated (admin panel):
Unsplash, Pexels, Pixabay, Rijksmuseum. Every provider maps to the same shape:
{id, title, author, thumb, full, link, source}. Imports run through the normal
ingest pipeline, so they get sRGB/date/dedupe like any upload.
"""
import ipaddress
import json
import socket
import urllib.parse
import urllib.request
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from . import auth, config, db as dbm, worker

router = APIRouter()
UA = {"User-Agent": "framevalet/0.2 (self-hosted Frame TV photo manager)"}
MAX_IMPORT_BYTES = 60_000_000

# Provider host allowlist: an imported URL must resolve to one of these (or a
# subdomain). Stops the import fetch from being pointed at internal services.
PROVIDER_HOSTS = {
    "openverse": ("openverse.org", "githubusercontent.com", "flickr.com",
                  "staticflickr.com", "wikimedia.org", "rawpixel.com"),
    "nasa": ("nasa.gov", "apod.nasa.gov"),
    "reddit": ("redd.it", "redditmedia.com", "reddit.com"),
    "rijksmuseum": ("rijksmuseum.nl",),
    "unsplash": ("unsplash.com", "images.unsplash.com"),
    "pexels": ("pexels.com",),
    "pixabay": ("pixabay.com",),
}


def _is_public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def _host_allowed(host: str, source: str) -> bool:
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in PROVIDER_HOSTS.get(source, ()))


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """Re-validate scheme + host public-ness on every redirect hop."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urlsplit(newurl)
        if parts.scheme != "https" or not _is_public_host(parts.hostname or ""):
            raise urllib.error.HTTPError(newurl, code, "blocked redirect target", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_GuardedRedirect)


def _get_json(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _q(s):
    return urllib.parse.quote(s or "")


# --- providers -----------------------------------------------------------
def openverse(db, query, page):
    d = _get_json("https://api.openverse.org/v1/images/"
                  f"?q={_q(query or 'landscape')}&page_size=20&page={page}"
                  "&license_type=commercial,modification")
    return [{"id": x["id"], "title": x.get("title") or "", "author": x.get("creator") or "",
             "thumb": x.get("thumbnail") or x.get("url"), "full": x.get("url"),
             "link": x.get("foreign_landing_url") or "", "source": "openverse"}
            for x in d.get("results", []) if x.get("url")]


def nasa(db, query, page):
    key = config.get(db, "nasa_key") or "DEMO_KEY"
    d = _get_json(f"https://api.nasa.gov/planetary/apod?api_key={_q(key)}&count=24")
    return [{"id": x.get("date", ""), "title": x.get("title") or "",
             "author": x.get("copyright") or "NASA APOD",
             "thumb": x.get("url"), "full": x.get("hdurl") or x.get("url"),
             "link": "https://apod.nasa.gov/apod/", "source": "nasa"}
            for x in d if x.get("media_type") == "image" and x.get("url")]


def reddit(db, query, page):
    sub = (query or "EarthPorn").strip().lstrip("r/")
    try:
        d = _get_json(f"https://www.reddit.com/r/{_q(sub)}/top.json?limit=25&t=month")
    except Exception as e:
        if "403" in str(e):
            raise HTTPException(502, "Reddit is blocking anonymous access from this "
                                     "network; try again later or use another source")
        raise
    out = []
    for c in d.get("data", {}).get("children", []):
        p = c.get("data", {})
        url = p.get("url_overridden_by_dest") or p.get("url") or ""
        if not url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png")):
            continue
        out.append({"id": p.get("id"), "title": p.get("title") or "",
                    "author": f"u/{p.get('author')}",
                    "thumb": (p.get("thumbnail") if str(p.get("thumbnail", "")).startswith("http")
                              else url),
                    "full": url,
                    "link": f"https://reddit.com{p.get('permalink', '')}",
                    "source": "reddit"})
    return out


def rijksmuseum(db, query, page):
    key = config.get(db, "rijksmuseum_key")
    if not key:
        raise HTTPException(400, "Rijksmuseum API key not set (Admin > Settings)")
    d = _get_json(f"https://www.rijksmuseum.nl/api/en/collection?key={_q(key)}"
                  f"&q={_q(query or 'landscape')}&imgonly=True&ps=24&p={page}")
    return [{"id": x["objectNumber"], "title": x.get("title") or "",
             "author": x.get("principalOrFirstMaker") or "",
             "thumb": (x.get("webImage") or {}).get("url"),
             "full": (x.get("webImage") or {}).get("url"),
             "link": x.get("links", {}).get("web") or "", "source": "rijksmuseum"}
            for x in d.get("artObjects", []) if x.get("webImage")]


def unsplash(db, query, page):
    key = config.get(db, "unsplash_key")
    if not key:
        raise HTTPException(400, "Unsplash API key not set (Admin > Settings)")
    d = _get_json("https://api.unsplash.com/search/photos"
                  f"?query={_q(query or 'landscape')}&per_page=24&page={page}",
                  headers={"Authorization": f"Client-ID {key}"})
    return [{"id": x["id"], "title": x.get("alt_description") or "",
             "author": x.get("user", {}).get("name") or "",
             "thumb": x["urls"]["small"], "full": x["urls"]["full"],
             "link": x.get("links", {}).get("html") or "", "source": "unsplash"}
            for x in d.get("results", [])]


def pexels(db, query, page):
    key = config.get(db, "pexels_key")
    if not key:
        raise HTTPException(400, "Pexels API key not set (Admin > Settings)")
    d = _get_json("https://api.pexels.com/v1/search"
                  f"?query={_q(query or 'landscape')}&per_page=24&page={page}",
                  headers={"Authorization": key})
    return [{"id": str(x["id"]), "title": x.get("alt") or "",
             "author": x.get("photographer") or "",
             "thumb": x["src"]["medium"], "full": x["src"]["original"],
             "link": x.get("url") or "", "source": "pexels"}
            for x in d.get("photos", [])]


def pixabay(db, query, page):
    key = config.get(db, "pixabay_key")
    if not key:
        raise HTTPException(400, "Pixabay API key not set (Admin > Settings)")
    d = _get_json(f"https://pixabay.com/api/?key={_q(key)}"
                  f"&q={_q(query or 'landscape')}&per_page=24&page={page}"
                  "&image_type=photo")
    return [{"id": str(x["id"]), "title": ", ".join((x.get("tags") or "").split(",")[:3]),
             "author": x.get("user") or "",
             "thumb": x.get("webformatURL"), "full": x.get("largeImageURL"),
             "link": x.get("pageURL") or "", "source": "pixabay"}
            for x in d.get("hits", [])]


PROVIDERS = {"openverse": openverse, "nasa": nasa, "reddit": reddit,
             "rijksmuseum": rijksmuseum, "unsplash": unsplash,
             "pexels": pexels, "pixabay": pixabay}
KEYLESS = ("openverse", "nasa", "reddit")


def provider_status(db) -> dict:
    return {name: (name in KEYLESS or bool(config.get(db, f"{name}_key")))
            for name in PROVIDERS}


# --- routes --------------------------------------------------------------
@router.get("/sources/search")
def search(source: str, q: str = "", page: int = 1,
           db=Depends(dbm.get_db), user=Depends(auth.current_user)):
    fn = PROVIDERS.get(source)
    if not fn:
        raise HTTPException(404, "unknown source")
    try:
        return JSONResponse({"results": fn(db, q, max(1, page))})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, "the source could not be reached") from e


@router.post("/sources/import")
def import_image(body: dict = Body(...), db=Depends(dbm.get_db),
                 user=Depends(auth.current_user)):
    if not auth.can(user, "upload"):
        raise HTTPException(403, "uploads not allowed for this account")
    url, title, source = body.get("url", ""), body.get("title", ""), body.get("source", "")
    parts = urlsplit(url)
    if (source not in PROVIDERS or parts.scheme != "https"
            or not parts.hostname or not _host_allowed(parts.hostname, source)
            or not _is_public_host(parts.hostname)):
        raise HTTPException(400, "import URL is not an allowed source host")
    req = urllib.request.Request(url, headers=UA)
    try:
        with _opener.open(req, timeout=60) as r:
            data = r.read(MAX_IMPORT_BYTES + 1)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "could not download the image")
    if len(data) > MAX_IMPORT_BYTES:
        raise HTTPException(413, "image too large")
    name = (title or source)[:80] + ".jpg"
    from . import pipeline
    try:
        pid = worker.ingest_bytes(db, data, filename=name, user_id=user["id"])
    except pipeline.PipelineError as e:
        raise HTTPException(415, str(e)) from e
    if pid is None:
        raise HTTPException(409, "already in the library")
    worker.kick()
    return JSONResponse({"ok": True, "id": pid})
