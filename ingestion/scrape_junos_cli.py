#!/usr/bin/env python3
"""Download Juniper's complete Junos CLI reference into RAG-ready markdown.

Reads the URL inventory written by ``discover_junos_cli_urls.py`` and fetches
the server-rendered DITA pages with a small paced worker pool. The generated
files mirror the source URL path under ``ingestion/sources/junos_cli`` and
remain git-ignored with the rest of the scraped corpus.

Usage:
    python ingestion/discover_junos_cli_urls.py
    python ingestion/scrape_junos_cli.py --skip-existing
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingestion.scrape_report import write_scrape_report  # noqa: E402

OUTPUT_DIR = Path(__file__).parent / "sources" / "junos_cli"
URLS_PATH = Path(__file__).parent / "junos_cli_urls.json"
PATH_PREFIX = "/documentation/us/en/software/junos/cli-reference/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
}


def load_urls() -> list[str]:
    if not URLS_PATH.exists():
        raise FileNotFoundError(
            f"{URLS_PATH} is missing; run ingestion/discover_junos_cli_urls.py first"
        )
    urls = json.loads(URLS_PATH.read_text(encoding="utf-8"))
    if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
        raise ValueError(f"{URLS_PATH} must contain a JSON list of absolute URLs")
    return urls


def output_path(url: str) -> Path:
    path = urlparse(url).path
    if not path.startswith(PATH_PREFIX):
        raise ValueError(f"URL is outside the Junos CLI reference: {url}")
    relative = Path(path[len(PATH_PREFIX) :])
    return OUTPUT_DIR / relative.with_suffix(".md")


def fetch_html(url: str) -> str:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def extract_html(html: str) -> str:
    """Keep only the DITA topic body and discard the global Vue shell."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.select(
        "script, style, noscript, sw-header, sw-footer, sw-leftnav, sw-rightnav, "
        "sw-topic-details, sw-breadcrumb, .page-options, .minitoc, "
        ".related-documentation"
    ):
        tag.decompose()

    content = soup.select_one("#topic-content") or soup.select_one(".topicBody")
    if content is None:
        raise ValueError("Junos page has no topicBody/topic-content region")
    return str(content)


def html_to_markdown(html: str) -> str:
    try:
        result = subprocess.run(
            ["pandoc", "-f", "html", "-t", "markdown_strict", "--wrap=none"],
            input=html.encode("utf-8"),
            capture_output=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "pandoc is required to scrape Junos CLI pages; install pandoc and retry"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"pandoc failed ({result.returncode}): {detail[:500]}")
    markdown = result.stdout.decode("utf-8", errors="replace")
    markdown = re.sub(r"<[^>]+>", "", markdown)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    if not markdown:
        raise ValueError("pandoc produced empty Junos CLI content")
    return markdown


def scrape_page(url: str, delay: float, skip_existing: bool) -> str:
    target = output_path(url)
    if skip_existing and target.exists():
        return f"SKIP {target.relative_to(OUTPUT_DIR)}"

    try:
        markdown = html_to_markdown(extract_html(fetch_html(url)))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"<!-- source: {url} -->\n\n{markdown}\n", encoding="utf-8")
        result = f"OK {target.relative_to(OUTPUT_DIR)} ({len(markdown)} chars)"
    except ValueError as exc:
        result = f"PARSER_ERROR {url}: {exc}"
    except (OSError, RuntimeError, urllib.error.URLError) as exc:
        result = f"ERROR {url}: {exc}"
    time.sleep(delay)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="fetch only the first N pages")
    parser.add_argument("--workers", type=int, default=4, help="parallel fetch workers")
    parser.add_argument("--delay", type=float, default=0.4, help="delay after each request")
    parser.add_argument("--skip-existing", action="store_true")
    args = parser.parse_args()
    if args.limit < 0 or args.workers < 1 or args.delay < 0:
        parser.error("--limit, --workers, and --delay must be non-negative (workers > 0)")

    urls = load_urls()
    if args.limit:
        urls = urls[: args.limit]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Scraping {len(urls)} Junos CLI pages -> {OUTPUT_DIR}")

    errors: list[str] = []
    parser_errors: list[str] = []
    skipped = 0
    ok = 0
    completed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(scrape_page, url, args.delay, args.skip_existing): url for url in urls
        }
        for future in as_completed(futures):
            result = future.result()
            completed += 1
            if result.startswith("PARSER_ERROR"):
                parser_errors.append(result)
            elif result.startswith("ERROR"):
                errors.append(result)
            elif result.startswith("SKIP"):
                skipped += 1
            else:
                ok += 1
            if completed % 100 == 0 or completed == len(urls):
                print(f"  [{completed}/{len(urls)}] {result}", flush=True)

    report = write_scrape_report(
        "junos_cli",
        inventory_path=URLS_PATH,
        document_count=len(urls),
        ok=ok,
        skipped=skipped,
        errors=errors,
        parser_errors=parser_errors,
        extra={"output_dir": str(OUTPUT_DIR)},
    )
    failed = len(errors) + len(parser_errors)
    print(
        f"Done. {ok} ok, {skipped} skipped, {failed} errors. "
        f"Report: {report}"
    )
    for error in (parser_errors + errors)[:20]:
        print(f"  {error}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
