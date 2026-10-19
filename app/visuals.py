"""
VideoFactory - visual providers (legally usable images).

ImageProvider is the general shape every image source must follow, so more
sources (including free AI image generators) can be added later.

1. WikimediaImages: Wikimedia Commons (free, no key). Best for REAL historical
   places, artifacts, artworks and maps. Only freely licensed files are accepted
   (public domain, CC0, CC BY, CC BY-SA), and the author + license are recorded.
2. PixabayImages: Pixabay (free key). Best for general atmosphere (sea, sky, fire,
   ruins). Pixabay Content License: free to use; authors are credited anyway.
   As Pixabay's API rules require, searches are remembered for 24 hours and
   pictures are downloaded (never hot-linked).
3. PexelsImages: Pexels (free key; new keys are paused at the moment). Used
   only if a Pexels key is ever added.

Every picture is converted by FFmpeg into a 1080x1920 vertical frame. If FFmpeg
cannot read a file, the file is treated as broken and another one is tried.
"""
import os
import hashlib
import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import PEXELS_API_KEY, PIXABAY_API_KEY

BASE_DIR = Path(__file__).resolve().parent.parent
FFMPEG = BASE_DIR / "bin" / ("ffmpeg" + (".exe" if os.name == "nt" else ""))
USER_AGENT = "VideoFactory/0.1 (personal educational short-video tool; Python urllib)"
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
MIN_WIDTH = 800
CACHE_DIR = BASE_DIR / "data" / "cache"
FREE_LICENSE = re.compile(r"(public domain|^pd|cc0|cc[- ]by)", re.IGNORECASE)


class VisualError(Exception):
    """A temporary problem reaching an image service (worth retrying later)."""


class ImageProvider:
    name = "base"

    def available(self):
        return True

    def search(self, query, limit=8):
        """Return candidates: {'url', 'page_url', 'width', 'height', 'license', 'credit', 'provider'}."""
        raise NotImplementedError


def _http_json(url, headers=None, timeout=30):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise VisualError(f"The image service refused the request ({exc.code}). Check the key.")
        raise VisualError(f"The image service answered with error {exc.code}. It may be busy.")
    except (urllib.error.URLError, TimeoutError):
        raise VisualError("Could not reach the image service. Check the Mac's internet connection.")


def _strip_html(text):
    return " ".join(re.sub(r"<[^>]+>", " ", text or "").split())


class WikimediaImages(ImageProvider):
    name = "wikimedia_commons"
    API = "https://commons.wikimedia.org/w/api.php"

    def search(self, query, limit=8):
        params = {
            "action": "query", "format": "json", "formatversion": "2",
            "generator": "search", "gsrsearch": f"{query} filetype:bitmap",
            "gsrnamespace": "6", "gsrlimit": str(limit),
            "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": "1280",
        }
        data = _http_json(f"{self.API}?{urllib.parse.urlencode(params)}")
        pages = sorted(data.get("query", {}).get("pages", []), key=lambda p: p.get("index", 999))
        results = []
        for page in pages:
            info = (page.get("imageinfo") or [{}])[0]
            if info.get("mime") not in ("image/jpeg", "image/png"):
                continue
            if (info.get("width") or 0) < MIN_WIDTH:
                continue
            meta = info.get("extmetadata") or {}
            license_name = _strip_html((meta.get("LicenseShortName") or {}).get("value", ""))
            if not FREE_LICENSE.search(license_name):
                continue
            artist = _strip_html((meta.get("Artist") or {}).get("value", "")) or "Unknown author"
            results.append({
                "url": info.get("thumburl") or info.get("url"),
                "fallback_url": info.get("url") if (info.get("size") or 0) < MAX_DOWNLOAD_BYTES else None,
                "page_url": info.get("descriptionurl", ""),
                "width": info.get("width"), "height": info.get("height"),
                "license": license_name,
                "credit": f"{artist[:120]} / Wikimedia Commons ({license_name})",
                "provider": self.name,
            })
        return results


class PexelsImages(ImageProvider):
    name = "pexels"
    API = "https://api.pexels.com/v1/search"

    def __init__(self, api_key=PEXELS_API_KEY):
        self.api_key = api_key

    def available(self):
        return bool(self.api_key)

    def search(self, query, limit=8):
        params = {"query": query, "orientation": "portrait", "size": "large", "per_page": str(limit)}
        data = _http_json(f"{self.API}?{urllib.parse.urlencode(params)}",
                          headers={"Authorization": self.api_key})
        results = []
        for photo in data.get("photos", []):
            src = photo.get("src") or {}
            if (photo.get("width") or 0) < MIN_WIDTH:
                continue
            results.append({
                "url": src.get("large2x") or src.get("original"),
                "fallback_url": src.get("large"),
                "page_url": photo.get("url", ""),
                "width": photo.get("width"), "height": photo.get("height"),
                "license": "Pexels License",
                "credit": f"Photo by {photo.get('photographer', 'unknown')} on Pexels",
                "provider": self.name,
            })
        return results


class PixabayImages(ImageProvider):
    name = "pixabay"
    API = "https://pixabay.com/api/"
    CACHE_SECONDS = 24 * 3600

    def __init__(self, api_key=PIXABAY_API_KEY):
        self.api_key = api_key

    def available(self):
        return bool(self.api_key)

    def _cached_search(self, params):
        cache_name = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:24]
        cache_file = CACHE_DIR / "pixabay" / f"{cache_name}.json"
        if cache_file.exists() and time.time() - cache_file.stat().st_mtime < self.CACHE_SECONDS:
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        data = _http_json(f"{self.API}?{urllib.parse.urlencode({**params, 'key': self.api_key})}")
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(data), encoding="utf-8")
        return data

    def search(self, query, limit=8):
        base = {"q": query[:100], "image_type": "photo", "safesearch": "true", "per_page": str(max(3, limit))}
        hits = self._cached_search({**base, "orientation": "vertical"}).get("hits", [])
        if len(hits) < 2:
            hits += self._cached_search(base).get("hits", [])
        results = []
        for hit in hits:
            results.append({
                "url": hit.get("largeImageURL"),
                "fallback_url": hit.get("webformatURL"),
                "page_url": hit.get("pageURL", ""),
                "width": hit.get("imageWidth"), "height": hit.get("imageHeight"),
                "license": "Pixabay Content License",
                "credit": f"Image by {hit.get('user', 'unknown')} from Pixabay",
                "provider": self.name,
            })
        return results


def download(url, destination):
    """Download one image file. Returns True on success (never raises for a single bad file)."""
    if not url:
        return False
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(MAX_DOWNLOAD_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False
    if len(data) > MAX_DOWNLOAD_BYTES or len(data) < 10_000:
        return False
    Path(destination).write_bytes(data)
    return True


def make_vertical_frame(source, destination):
    """Turn any picture into a 1080x1920 (9:16) frame. Returns True if FFmpeg could read it."""
    try:
        result = subprocess.run(
            [str(FFMPEG), "-y", "-v", "error", "-i", str(source),
             "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
             "-frames:v", "1", "-q:v", "3", str(destination)],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and Path(destination).exists() and Path(destination).stat().st_size > 5_000
