"""US Courts bankruptcy forms sync service.

The goal is to mirror official bankruptcy form PDFs from uscourts.gov without
triggering anti-bot controls:
- low request rate (configurable delay)
- explicit User-Agent
- conditional GET support (ETag / Last-Modified)
- lightweight HTML parsing (no JS rendering)

The service also computes a manifest and a diff against the prior snapshot so
callers can detect newly added, removed, or revised forms.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

BASE_URL = "https://www.uscourts.gov"
BANKRUPTCY_INDEX_URL = f"{BASE_URL}/forms-rules/forms/bankruptcy-forms"
SITEMAP_INDEX_URL = f"{BASE_URL}/sitemap.xml"
FORM_PATH_PREFIX = "/forms-rules/forms/"
PDF_HOST = "www.uscourts.gov"
DEFAULT_USER_AGENT = "fillform-bankruptcy-sync/1.0 (+https://example.invalid/contact)"
ROBOTS_URL = f"{BASE_URL}/robots.txt"
BOT_CHALLENGE_MARKERS = (
    "captcha",
    "cloudflare",
    "cf-chl",
    "attention required",
    "verify you are human",
)


class _LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.links.append(href)


@dataclass(slots=True)
class FormDocument:
    slug: str
    page_url: str
    pdf_url: str
    file_name: str
    sha256: str
    size_bytes: int
    pdf_etag: str
    pdf_last_modified: str


@dataclass(slots=True)
class SyncResult:
    fetched_at_unix: int
    total_index_forms: int
    total_pdf_forms: int
    downloaded_files: int
    unchanged_files: int
    reused_without_fetch: int
    manifest_path: str
    added: list[str]
    removed: list[str]
    changed: list[str]


class USCourtsBankruptcyFormsSync:
    """Crawler/downloader with diffing and conservative network behavior."""

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        min_request_interval_seconds: float = 1.2,
        max_retries: int = 3,
        retry_base_delay_seconds: float = 1.5,
        respect_robots_txt: bool = True,
    ):
        self.user_agent = user_agent
        self.min_request_interval_seconds = min_request_interval_seconds
        self.max_retries = max_retries
        self.retry_base_delay_seconds = retry_base_delay_seconds
        self.respect_robots_txt = respect_robots_txt
        self._last_request_ts = 0.0

    def sync(
        self,
        output_dir: str | Path,
        state_path: str | Path,
        download_pdfs: bool = True,
        max_form_pages: int | None = None,
    ) -> SyncResult:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        state_file = Path(state_path)
        state_file.parent.mkdir(parents=True, exist_ok=True)

        state = self._load_json(state_file)
        response_cache = state.get("responses", {}) if isinstance(state, dict) else {}
        prior_page_lastmod = state.get("form_page_lastmod", {}) if isinstance(state, dict) else {}

        if self.respect_robots_txt:
            robots_delay, response_cache = self._discover_robots_crawl_delay(response_cache)
            if robots_delay is not None:
                self.min_request_interval_seconds = max(self.min_request_interval_seconds, robots_delay)

        index_html, response_cache = self._get_text(BANKRUPTCY_INDEX_URL, response_cache)
        form_pages = self._extract_form_pages(index_html)
        if max_form_pages is not None:
            form_pages = form_pages[: max(0, max_form_pages)]
        sitemap_lastmods, response_cache = self._discover_form_page_lastmods(response_cache)

        previous_manifest = self._load_previous_manifest(state_file)
        current_manifest: dict[str, dict[str, Any]] = {}
        downloaded = 0
        unchanged = 0
        reused_without_fetch = 0

        for page_url in form_pages:
            slug = self._slug_from_page(page_url)
            prior_entries_for_page = self._prior_entries_for_page(previous_manifest, page_url)
            prior_entry = prior_entries_for_page[0] if prior_entries_for_page else previous_manifest.get(slug, {})

            new_lastmod = sitemap_lastmods.get(page_url)
            old_lastmod = prior_page_lastmod.get(page_url)
            can_reuse_entry = (
                bool(prior_entry)
                and bool(new_lastmod)
                and new_lastmod == old_lastmod
                and prior_entry.get("pdf_url")
            )

            if can_reuse_entry:
                for reused in prior_entries_for_page:
                    key = str(reused.get("slug", slug))
                    current_manifest[key] = reused
                reused_without_fetch += len(prior_entries_for_page) if prior_entries_for_page else 1
                continue

            page_html, response_cache = self._get_text(page_url, response_cache)
            pdf_urls = self._extract_pdf_links(page_url, page_html)
            if not pdf_urls:
                continue

            for idx, pdf_url in enumerate(pdf_urls, start=1):
                doc_key = self._document_key(slug, pdf_url, idx)
                file_name = f"{doc_key}.pdf"
                target = out / file_name

                file_sha = ""
                file_size = 0
                pdf_etag = ""
                pdf_last_modified = ""
                prior_pdf_entry = self._prior_entry_for_pdf(previous_manifest, page_url, pdf_url)

                if download_pdfs:
                    changed = self._download_if_needed(pdf_url, target)
                    if changed:
                        downloaded += 1
                    else:
                        unchanged += 1
                    file_sha = self._sha256_file(target)
                    file_size = target.stat().st_size
                    pdf_etag, pdf_last_modified = self._probe_pdf_headers(pdf_url)
                else:
                    # Preserve previous known file fingerprint when skipping body downloads.
                    if isinstance(prior_pdf_entry, dict) and prior_pdf_entry.get("pdf_url") == pdf_url:
                        file_sha = str(prior_pdf_entry.get("sha256", ""))
                        file_size = int(prior_pdf_entry.get("size_bytes", 0) or 0)
                    pdf_etag, pdf_last_modified = self._probe_pdf_headers(pdf_url)

                current_manifest[doc_key] = asdict(
                    FormDocument(
                        slug=doc_key,
                        page_url=page_url,
                        pdf_url=pdf_url,
                        file_name=file_name,
                        sha256=file_sha,
                        size_bytes=file_size,
                        pdf_etag=pdf_etag,
                        pdf_last_modified=pdf_last_modified,
                    )
                )

        fetched_at = int(time.time())
        manifest_path = out / f"bankruptcy_forms_manifest_{fetched_at}.json"
        manifest_path.write_text(json.dumps(current_manifest, indent=2, sort_keys=True), encoding="utf-8")

        added, removed, changed = self._manifest_diff(previous_manifest, current_manifest)
        state_file.write_text(
            json.dumps(
                {
                    "last_sync_at_unix": fetched_at,
                    "latest_manifest_path": str(manifest_path),
                    "form_page_lastmod": sitemap_lastmods,
                    "reused_without_fetch": reused_without_fetch,
                    "responses": response_cache,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        return SyncResult(
            fetched_at_unix=fetched_at,
            total_index_forms=len(form_pages),
            total_pdf_forms=len(current_manifest),
            downloaded_files=downloaded,
            unchanged_files=unchanged,
            reused_without_fetch=reused_without_fetch,
            manifest_path=str(manifest_path),
            added=added,
            removed=removed,
            changed=changed,
        )

    def _load_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _load_previous_manifest(self, state_file: Path) -> dict[str, Any]:
        state = self._load_json(state_file)
        manifest_path = state.get("latest_manifest_path") if isinstance(state, dict) else None
        if not manifest_path:
            return {}
        manifest_file = Path(manifest_path)
        if not manifest_file.exists():
            return {}
        return self._load_json(manifest_file)

    def _manifest_diff(
        self,
        old: dict[str, dict[str, Any]],
        new: dict[str, dict[str, Any]],
    ) -> tuple[list[str], list[str], list[str]]:
        old_keys = set(old)
        new_keys = set(new)
        added = sorted(new_keys - old_keys)
        removed = sorted(old_keys - new_keys)

        changed: list[str] = []
        for key in sorted(old_keys & new_keys):
            prior = old[key]
            current = new[key]
            if prior.get("pdf_url") != current.get("pdf_url"):
                changed.append(key)
                continue
            if prior.get("sha256") and current.get("sha256") and prior.get("sha256") != current.get("sha256"):
                changed.append(key)
                continue
            if prior.get("pdf_etag") and current.get("pdf_etag") and prior.get("pdf_etag") != current.get("pdf_etag"):
                changed.append(key)
                continue
            if (
                prior.get("pdf_last_modified")
                and current.get("pdf_last_modified")
                and prior.get("pdf_last_modified") != current.get("pdf_last_modified")
            ):
                changed.append(key)
        return added, removed, changed

    def _extract_form_pages(self, html: str) -> list[str]:
        parser = _LinkExtractor()
        parser.feed(html)
        out: set[str] = set()
        for href in parser.links:
            absolute = urljoin(BANKRUPTCY_INDEX_URL, href)
            parsed = urlparse(absolute)
            if parsed.netloc != PDF_HOST:
                continue
            if not parsed.path.startswith(FORM_PATH_PREFIX):
                continue
            if parsed.path.endswith("/bankruptcy-forms"):
                continue
            out.add(f"{parsed.scheme}://{parsed.netloc}{parsed.path}")
        return sorted(out)

    def _extract_pdf_links(self, page_url: str, html: str) -> list[str]:
        parser = _LinkExtractor()
        parser.feed(html)
        out: set[str] = set()
        for href in parser.links:
            if ".pdf" not in href.lower():
                continue
            absolute = urljoin(page_url, href)
            parsed = urlparse(absolute)
            if parsed.netloc != PDF_HOST:
                continue
            out.add(absolute)
        return sorted(out)

    def _discover_form_page_lastmods(
        self,
        response_cache: dict[str, Any],
    ) -> tuple[dict[str, str], dict[str, Any]]:
        index_xml, response_cache = self._get_text(SITEMAP_INDEX_URL, response_cache)
        sitemap_pages = self._extract_sitemap_pages(index_xml)
        out: dict[str, str] = {}
        for sitemap_url in sitemap_pages:
            xml, response_cache = self._get_text(sitemap_url, response_cache)
            for loc, lastmod in self._extract_sitemap_entries(xml):
                parsed = urlparse(loc)
                if parsed.netloc != PDF_HOST:
                    continue
                if not parsed.path.startswith(FORM_PATH_PREFIX):
                    continue
                if parsed.path.endswith("/bankruptcy-forms"):
                    continue
                if not lastmod:
                    continue
                out[f"{parsed.scheme}://{parsed.netloc}{parsed.path}"] = lastmod
        return out, response_cache

    def _extract_sitemap_pages(self, sitemap_index_xml: str) -> list[str]:
        out: list[str] = []
        for loc, _lastmod in self._extract_sitemap_entries(sitemap_index_xml):
            if loc.startswith(f"{BASE_URL}/sitemap.xml"):
                out.append(loc)
        return out

    def _extract_sitemap_entries(self, xml: str) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError:
            return entries

        for item_node in root.findall(".//{*}url") + root.findall(".//{*}sitemap"):
            loc_node = item_node.find("{*}loc")
            if loc_node is None or loc_node.text is None:
                continue
            lastmod_node = item_node.find("{*}lastmod")
            lastmod_text = lastmod_node.text.strip() if (lastmod_node is not None and lastmod_node.text) else ""
            entries.append((loc_node.text.strip(), self._normalize_lastmod(lastmod_text)))

        if entries:
            return entries

        # Fallback parser keeps backward compatibility if XML namespaces/shape differ.
        for block in re.findall(r"<(?:url|sitemap)>(.*?)</(?:url|sitemap)>", xml, flags=re.S):
            loc_match = re.search(r"<loc>(.*?)</loc>", block, flags=re.S)
            lastmod_match = re.search(r"<lastmod>(.*?)</lastmod>", block, flags=re.S)
            if not loc_match:
                continue
            loc = loc_match.group(1).strip()
            lastmod_raw = (lastmod_match.group(1).strip() if lastmod_match else "")
            entries.append((loc, self._normalize_lastmod(lastmod_raw)))
        return entries

    def _normalize_lastmod(self, value: str) -> str:
        if not value:
            return ""
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt).isoformat()
            except ValueError:
                continue
        return value

    def _slug_from_page(self, page_url: str) -> str:
        raw = page_url.rstrip("/").rsplit("/", 1)[-1].lower()
        return re.sub(r"[^a-z0-9-]+", "-", raw).strip("-") or "form"

    def _document_key(self, page_slug: str, pdf_url: str, index: int) -> str:
        tail = pdf_url.split("/")[-1].split("?")[0].lower()
        base = re.sub(r"\.pdf$", "", tail)
        base = re.sub(r"[^a-z0-9-]+", "-", base).strip("-")
        if not base:
            base = f"doc-{index:02d}"
        if base.startswith(page_slug):
            return base
        return f"{page_slug}--{base}"

    def _prior_entries_for_page(
        self,
        manifest: dict[str, dict[str, Any]],
        page_url: str,
    ) -> list[dict[str, Any]]:
        return [entry for entry in manifest.values() if isinstance(entry, dict) and entry.get("page_url") == page_url]

    def _prior_entry_for_pdf(
        self,
        manifest: dict[str, dict[str, Any]],
        page_url: str,
        pdf_url: str,
    ) -> dict[str, Any] | None:
        for entry in manifest.values():
            if not isinstance(entry, dict):
                continue
            if entry.get("page_url") == page_url and entry.get("pdf_url") == pdf_url:
                return entry
        return None

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_ts
        if elapsed < self.min_request_interval_seconds:
            time.sleep(self.min_request_interval_seconds - elapsed)

    def _get_text(self, url: str, response_cache: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        self._throttle()
        headers = {"User-Agent": self.user_agent, "Accept": "text/html,application/xhtml+xml"}
        cache_entry = response_cache.get(url, {})
        if isinstance(cache_entry, dict):
            if cache_entry.get("etag"):
                headers["If-None-Match"] = str(cache_entry["etag"])
            if cache_entry.get("last_modified"):
                headers["If-Modified-Since"] = str(cache_entry["last_modified"])

        req = Request(url, headers=headers)
        for attempt in range(self.max_retries + 1):
            try:
                with urlopen(req, timeout=30) as resp:
                    self._last_request_ts = time.time()
                    text = resp.read().decode("utf-8", "ignore")
                    if self._looks_like_bot_challenge(text):
                        raise RuntimeError(
                            f"Potential anti-bot challenge detected at {url}. "
                            "Increase min_request_interval_seconds and retry later."
                        )
                    response_cache[url] = {
                        "etag": resp.headers.get("ETag"),
                        "last_modified": resp.headers.get("Last-Modified"),
                        "cached_body": text,
                    }
                    return text, response_cache
            except HTTPError as exc:
                self._last_request_ts = time.time()
                if exc.code == 304 and isinstance(cache_entry, dict) and cache_entry.get("cached_body"):
                    return str(cache_entry["cached_body"]), response_cache
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    time.sleep(self.retry_base_delay_seconds * (2**attempt))
                    continue
                raise RuntimeError(f"HTTP error fetching {url}: {exc.code}") from exc
            except URLError as exc:
                if attempt < self.max_retries:
                    time.sleep(self.retry_base_delay_seconds * (2**attempt))
                    continue
                raise RuntimeError(f"Network error fetching {url}: {exc}") from exc

    def _download_if_needed(self, pdf_url: str, target: Path) -> bool:
        self._throttle()
        req = Request(pdf_url, headers={"User-Agent": self.user_agent, "Accept": "application/pdf"})
        payload = b""
        for attempt in range(self.max_retries + 1):
            try:
                with urlopen(req, timeout=60) as resp:
                    self._last_request_ts = time.time()
                    payload = resp.read()
                    break
            except HTTPError as exc:
                self._last_request_ts = time.time()
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    time.sleep(self.retry_base_delay_seconds * (2**attempt))
                    continue
                raise RuntimeError(f"Failed to download PDF {pdf_url}: HTTP {exc.code}") from exc
            except URLError as exc:
                if attempt < self.max_retries:
                    time.sleep(self.retry_base_delay_seconds * (2**attempt))
                    continue
                raise RuntimeError(f"Failed to download PDF {pdf_url}: {exc}") from exc

        new_hash = hashlib.sha256(payload).hexdigest()
        if target.exists() and self._sha256_file(target) == new_hash:
            return False

        target.write_bytes(payload)
        return True

    def _probe_pdf_headers(self, pdf_url: str) -> tuple[str, str]:
        self._throttle()
        req = Request(pdf_url, headers={"User-Agent": self.user_agent}, method="HEAD")
        for attempt in range(self.max_retries + 1):
            try:
                with urlopen(req, timeout=30) as resp:
                    self._last_request_ts = time.time()
                    return resp.headers.get("ETag", ""), resp.headers.get("Last-Modified", "")
            except HTTPError as exc:
                self._last_request_ts = time.time()
                # If HEAD isn't allowed, do not fail the sync.
                if exc.code in (403, 405):
                    return "", ""
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    time.sleep(self.retry_base_delay_seconds * (2**attempt))
                    continue
                return "", ""
            except URLError:
                if attempt < self.max_retries:
                    time.sleep(self.retry_base_delay_seconds * (2**attempt))
                    continue
                return "", ""
        return "", ""

    def _discover_robots_crawl_delay(
        self,
        response_cache: dict[str, Any],
    ) -> tuple[float | None, dict[str, Any]]:
        try:
            robots_text, response_cache = self._get_text(ROBOTS_URL, response_cache)
        except Exception:
            return None, response_cache
        return self._parse_robots_crawl_delay(robots_text), response_cache

    def _parse_robots_crawl_delay(self, robots_txt: str) -> float | None:
        current_agents: list[str] = []
        matched_delay: float | None = None
        for line in robots_txt.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or ":" not in stripped:
                if not stripped:
                    current_agents = []
                continue
            key, value = [part.strip() for part in stripped.split(":", 1)]
            key_lower = key.lower()
            if key_lower == "user-agent":
                current_agents.append(value.lower())
            elif key_lower == "crawl-delay":
                if "*" in current_agents or self.user_agent.lower() in current_agents:
                    try:
                        matched_delay = float(value)
                    except ValueError:
                        continue
        return matched_delay

    def _looks_like_bot_challenge(self, text: str) -> bool:
        body = text.lower()
        return any(marker in body for marker in BOT_CHALLENGE_MARKERS)

    def _sha256_file(self, path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
