#!/usr/bin/env python3
"""Interactive wizard to scaffold a new RAG doc source for hpe-networking-mcp.

Prompts for a source key, purpose, seed URL(s), and a scraper strategy, then:

1. Generates `ingestion/scrape_<key>.py` from a strategy template.
2. Appends a well-formed entry to `ingestion/source_manifest.json`.
3. Best-effort auto-patches `ingest_docs.py`'s `SOURCE_META` and `rag.py`'s
   `_DOC_TYPE_TO_SOURCE` dicts (falls back to printed manual instructions if
   the file doesn't match the expected simple dict-literal shape).

Supported scraper strategies:
    simple_http_pandoc  - urllib + pandoc, like ingestion/scrape.py.
      Good default for sites that serve plain server-rendered HTML.
    playwright_headless - real headless Chrome, like
      ingestion/scrape_techdocs_pw.py / scrape_vsg.py. Use when a site blocks
      plain HTTP clients (403/406) or needs JS rendering.
    openapi_json        - download OpenAPI JSON specs directly, like
      ingestion/scrape_openapi.py. No chunking needed — ingest_docs.py parses
      schemas/endpoints itself.
    sitemap_crawl        - discover URLs from a sitemap/search-index first
      (like ingestion/discover_aos_urls.py), write them to a seed file, then
      scrape each with Playwright (like ingestion/scrape_aos_pw.py).

Usage:
    python scripts/add_rag_source.py
    python scripts/add_rag_source.py --yes --source my_docs \\
        --doc-type my-docs --purpose "..." --seed-url https://example.com \\
        --strategy simple_http_pandoc
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "ingestion" / "source_manifest.json"
INGEST_DOCS_PATH = ROOT / "ingestion" / "ingest_docs.py"
RAG_PY_PATH = ROOT / "src" / "hpe_networking_mcp" / "mcp_servers" / "rag.py"

STRATEGIES = ("simple_http_pandoc", "playwright_headless", "openapi_json", "sitemap_crawl")

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _ask_text(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def _ask_choice(prompt: str, choices: tuple[str, ...], default: str) -> str:
    listed = ", ".join(choices)
    while True:
        answer = _ask_text(f"{prompt} ({listed})", default)
        if answer in choices:
            return answer
        print(f"  Please choose one of: {listed}")


# ── Scraper templates ────────────────────────────────────────────────────────


def _template_simple_http_pandoc(key: str, seed_urls: list[str]) -> str:
    urls_literal = ",\n".join(f'    "{u}"' for u in seed_urls)
    return f'''#!/usr/bin/env python3
"""
Scrape {key} pages and convert to markdown for RAG.

Scaffolded by scripts/add_rag_source.py (strategy: simple_http_pandoc).
Uses urllib for plain server-rendered HTML + pandoc for markdown conversion.
Add real page URLs to PAGES below, or replace load_urls() with a crawler.
"""
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent / "sources" / "{key}"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

HEADERS = {{
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
}}

PAGES = [
{urls_literal}
]


def slug_from_url(url):
    return re.sub(r"[^a-z0-9_-]", "_", url.split("//")[1].replace("/", "_").lower())


def fetch_html(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def html_to_markdown(html, url):
    result = subprocess.run(
        ["pandoc", "-f", "html", "-t", "markdown_strict", "--wrap=none"],
        input=html.encode(),
        capture_output=True,
        timeout=30,
    )
    md = result.stdout.decode("utf-8", errors="replace")
    return f"<!-- source: {{url}} -->\\n\\n" + md


def scrape_page(url):
    slug = slug_from_url(url)
    out_path = OUTPUT_DIR / f"{{slug}}.md"
    try:
        html = fetch_html(url)
        md = html_to_markdown(html, url)
        out_path.write_text(md, encoding="utf-8")
        print(f"  OK: {{slug}} ({{len(md)}} chars)")
    except Exception as e:
        print(f"  ERROR {{url}}: {{e}}")
    time.sleep(0.5)


def main():
    print(f"Scraping {{len(PAGES)}} pages -> {{OUTPUT_DIR}}")
    for i, url in enumerate(PAGES, 1):
        print(f"[{{i}}/{{len(PAGES)}}] {{url}}")
        scrape_page(url)
    print("Done.")


if __name__ == "__main__":
    main()
'''


def _template_playwright_headless(key: str, seed_urls: list[str]) -> str:
    urls_literal = ",\n".join(f'    "{u}"' for u in seed_urls)
    return f'''#!/usr/bin/env python3
"""
Scrape {key} pages using Playwright (headless Chrome) to bypass plain-client
blocking / render JS content.

Scaffolded by scripts/add_rag_source.py (strategy: playwright_headless).
Add real page URLs to PAGES below, or replace load_urls() with a discovery
step (see ingestion/discover_aos_urls.py for a sitemap-based example).
"""
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

OUTPUT_DIR = Path(__file__).parent / "sources" / "{key}"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

PAGES = [
{urls_literal}
]


def slug_from_url(url: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "_", url.split("//")[1].replace("/", "_").lower())


def extract_content(html: str) -> str:
    for pattern in [r"<main[^>]*>(.*?)</main>", r"<article[^>]*>(.*?)</article>"]:
        m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1)
    return html


def html_to_markdown(html: str) -> str:
    import subprocess
    result = subprocess.run(
        ["pandoc", "-f", "html", "-t", "markdown_strict", "--wrap=none"],
        input=html.encode(), capture_output=True, timeout=30,
    )
    return result.stdout.decode("utf-8", errors="replace")


def main():
    print(f"Scraping {{len(PAGES)}} pages -> {{OUTPUT_DIR}}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        for i, url in enumerate(PAGES, 1):
            slug = slug_from_url(url)
            out_path = OUTPUT_DIR / f"{{slug}}.md"
            try:
                page.goto(url, wait_until="networkidle", timeout=45000)
                html = extract_content(page.content())
                md = f"<!-- source: {{url}} -->\\n\\n" + html_to_markdown(html)
                out_path.write_text(md, encoding="utf-8")
                print(f"[{{i}}/{{len(PAGES)}}] OK: {{slug}} ({{len(md)}} chars)")
            except Exception as e:
                print(f"[{{i}}/{{len(PAGES)}}] ERROR {{url}}: {{e}}")
            time.sleep(2)  # pace requests — avoid tripping bot detection
        browser.close()
    print("Done.")


if __name__ == "__main__":
    main()
'''


def _template_openapi_json(key: str, seed_urls: list[str]) -> str:
    base_url = seed_urls[0] if seed_urls else "https://example.com/openapi"
    return f'''#!/usr/bin/env python3
"""
Download OpenAPI spec JSON files for {key}.

Scaffolded by scripts/add_rag_source.py (strategy: openapi_json).
Fill in load_spec_names() with the real spec name list for this API, or a
directory-listing/discovery call if the source exposes one.
"""
import json
import urllib.request
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent / "sources" / "{key}"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

BASE_URL = "{base_url}"

HEADERS = {{
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}}


def load_spec_names() -> list[str]:
    # TODO: replace with the real list of spec slugs/names for this API.
    return []


def fetch_spec(name: str) -> str:
    out_path = OUTPUT_DIR / f"{{name}}.json"
    if out_path.exists():
        return f"SKIP {{name}}"
    url = f"{{BASE_URL}}/{{name}}.json"
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        json.loads(data)  # validate
        out_path.write_bytes(data)
        return f"OK {{name}} ({{len(data)}} bytes)"
    except Exception as e:
        return f"ERROR {{name}}: {{e}}"


def main():
    names = load_spec_names()
    print(f"Downloading {{len(names)}} OpenAPI specs -> {{OUTPUT_DIR}}")
    for name in names:
        print(" ", fetch_spec(name))
    print("Done.")


if __name__ == "__main__":
    main()
'''


def _template_sitemap_crawl(key: str, seed_urls: list[str]) -> tuple[str, str]:
    base = seed_urls[0] if seed_urls else "https://example.com/"
    discover = f'''#!/usr/bin/env python3
"""
Discover URLs for {key} from a sitemap/search-index before scraping.

Scaffolded by scripts/add_rag_source.py (strategy: sitemap_crawl).
Adapt to the real sitemap format (XML sitemap.xml, Lunr/Doks search index
JSON, etc.) — see ingestion/discover_aos_urls.py for a worked example of
pulling a JS-rendered search index via Playwright.

Writes: ingestion/{key}_urls.json (a flat JSON list of absolute URLs) —
referenced from source_manifest.json's `url_seed_file` for this source.
"""
import json
from pathlib import Path

BASE = "{base}"
OUT_PATH = Path(__file__).parent / "{key}_urls.json"


def main():
    # TODO: replace with a real sitemap/search-index fetch + parse.
    urls: list[str] = []
    if not urls:
        print(
            "No URLs discovered - this discover scaffold has not been adapted"
            " yet; edit it to fetch/parse the real sitemap or search index"
            " (see this file's docstring), then re-run."
        )
        raise SystemExit(1)
    OUT_PATH.write_text(json.dumps(sorted(set(urls)), indent=2))
    print(f"Discovered {{len(urls)}} URLs -> {{OUT_PATH}}")


if __name__ == "__main__":
    main()
'''
    scrape = _template_playwright_headless(key, seed_urls).replace(
        'PAGES = [\n' + ",\n".join(f'    "{u}"' for u in seed_urls) + "\n]",
        f'''def load_urls() -> list[str]:
    seed_file = Path(__file__).parent / "{key}_urls.json"
    if seed_file.exists():
        return json.loads(seed_file.read_text())
    return []


PAGES = load_urls()''',
    )
    scrape = scrape.replace("import re\nimport time\n", "import json\nimport re\nimport time\n", 1)
    return discover, scrape


_TEMPLATES = {
    "simple_http_pandoc": _template_simple_http_pandoc,
    "playwright_headless": _template_playwright_headless,
    "openapi_json": _template_openapi_json,
}


def write_scraper(key: str, strategy: str, seed_urls: list[str]) -> list[Path]:
    written: list[Path] = []
    if strategy == "sitemap_crawl":
        discover_text, scrape_text = _template_sitemap_crawl(key, seed_urls)
        discover_path = ROOT / "ingestion" / f"discover_{key}_urls.py"
        scrape_path = ROOT / "ingestion" / f"scrape_{key}.py"
        discover_path.write_text(discover_text, encoding="utf-8")
        scrape_path.write_text(scrape_text, encoding="utf-8")
        written = [discover_path, scrape_path]
    else:
        text = _TEMPLATES[strategy](key, seed_urls)
        scrape_path = ROOT / "ingestion" / f"scrape_{key}.py"
        scrape_path.write_text(text, encoding="utf-8")
        written = [scrape_path]
    for p in written:
        p.chmod(0o755)
    return written


# ── Manifest + registration patching ────────────────────────────────────────


def append_manifest_entry(key: str, doc_type: str, purpose: str,
                           seed_urls: list[str], strategy: str) -> dict:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if any(e["source"] == key for e in manifest):
        raise SystemExit(f"source_manifest.json already has an entry for {key!r}")

    entry: dict = {
        "source": key,
        "doc_type": doc_type,
        "purpose": purpose,
        "seed_urls": seed_urls,
        "output_dir": f"ingestion/sources/{key}",
        "scraper": f"ingestion/scrape_{key}.py",
        "notes": f"Scaffolded via scripts/add_rag_source.py (strategy: {strategy}).",
    }
    if strategy == "sitemap_crawl":
        entry["url_seed_file"] = f"ingestion/{key}_urls.json"
        discover = f"ingestion/discover_{key}_urls.py"
        entry["extra_scripts"] = [discover]
        # Discovery writes the URL list the scraper reads, so it must be
        # planned BEFORE the scrape (scripts/refresh_rag_sources.py orders
        # steps by this phase; scripts/validate_source_manifest.py enforces
        # that every discover_* script declares it).
        entry["extra_script_phases"] = {discover: "pre"}

    manifest.append(entry)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return entry


def _try_patch_dict_literal(path: Path, dict_name: str, new_key: str, new_value: str) -> bool:
    """Best-effort: insert `new_key: new_value` into a top-level `dict_name = {...}`
    dict literal, preserving formatting, only when the assignment parses cleanly
    as a plain dict of string keys/values. Returns False (no changes) otherwise.
    """
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False

    target = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Name) and t.id == dict_name and isinstance(node.value, ast.Dict):
                target = node
                break
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            t = node.target
            if isinstance(t, ast.Name) and t.id == dict_name and isinstance(node.value, ast.Dict):
                target = node
                break
    if target is None:
        return False

    # Already present?
    for k in target.value.keys:
        if isinstance(k, ast.Constant) and k.value == new_key:
            return True  # nothing to do

    lines = text.splitlines(keepends=True)
    closing_lineno = target.value.end_lineno  # 1-indexed line with the closing brace
    insert_at = closing_lineno - 1  # 0-indexed line *before* the closing brace line
    indent = "    "
    new_line = f'{indent}"{new_key}": {new_value},\n'
    lines.insert(insert_at, new_line)
    path.write_text("".join(lines), encoding="utf-8")
    return True


def try_auto_register(key: str, doc_type: str) -> dict[str, bool]:
    results = {}
    results["SOURCE_META"] = _try_patch_dict_literal(
        INGEST_DOCS_PATH, "SOURCE_META", key, f'"{doc_type}"'
    )
    results["_DOC_TYPE_TO_SOURCE"] = _try_patch_dict_literal(
        RAG_PY_PATH, "_DOC_TYPE_TO_SOURCE", doc_type, f'"{key}"'
    )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    parser.add_argument("--source", help="source key (snake_case)")
    parser.add_argument("--doc-type", help="doc_type tag")
    parser.add_argument("--purpose", help="one-line purpose description")
    parser.add_argument(
        "--seed-url",
        action="append",
        dest="seed_urls",
        help="seed URL (repeatable)",
    )
    parser.add_argument("--strategy", choices=STRATEGIES)
    args = parser.parse_args()

    key = args.source or _ask_text("Source key (snake_case, e.g. my_new_docs)")
    while not _KEY_RE.match(key):
        print(
            "  Must be snake_case: lowercase letters, digits, underscores, "
            "starting with a letter."
        )
        key = _ask_text("Source key (snake_case)")

    doc_type = args.doc_type or _ask_text(
        "doc_type tag (kebab-case, e.g. my-new-docs)",
        key.replace("_", "-"),
    )
    purpose = args.purpose or _ask_text("Purpose (one line)")
    seed_urls = args.seed_urls or []
    while not seed_urls:
        raw = _ask_text("Seed URL(s), comma-separated")
        seed_urls = [u.strip() for u in raw.split(",") if u.strip()]
    strategy = args.strategy or _ask_choice("Scraper strategy", STRATEGIES, "simple_http_pandoc")

    print(f"\nAbout to scaffold source {key!r}:")
    print(f"  doc_type:  {doc_type}")
    print(f"  purpose:   {purpose}")
    print(f"  seed_urls: {seed_urls}")
    print(f"  strategy:  {strategy}")
    print(f"  output_dir: ingestion/sources/{key}")

    if not args.yes:
        confirm = input("\nProceed? [Y/n] ").strip().lower()
        if confirm not in ("", "y", "yes"):
            print("Aborted.")
            return 1

    written = write_scraper(key, strategy, seed_urls)
    append_manifest_entry(key, doc_type, purpose, seed_urls, strategy)
    patched = try_auto_register(key, doc_type)

    print("\nDone. Created:")
    for p in written:
        print(f"  {p.relative_to(ROOT)}")
    print(f"  ingestion/source_manifest.json (+{key} entry)")

    print("\nRegistration status:")
    source_meta_status = "patched" if patched["SOURCE_META"] else "NEEDS MANUAL EDIT"
    doc_type_status = (
        "patched" if patched["_DOC_TYPE_TO_SOURCE"] else "NEEDS MANUAL EDIT"
    )
    print(f"  ingest_docs.py SOURCE_META: {source_meta_status}")
    print(f"  rag.py _DOC_TYPE_TO_SOURCE: {doc_type_status}")
    if not all(patched.values()):
        print(
            "\nOne or more files didn't match the expected simple dict-literal "
            "shape for auto-patching — add the entries by hand:\n"
            f'  ingest_docs.py SOURCE_META: "{key}": "{doc_type}",\n'
            f'  rag.py _DOC_TYPE_TO_SOURCE: "{doc_type}": "{key}",'
        )

    print(
        "\nNext steps:\n"
        f"  1. Fill in real page/spec URLs in ingestion/scrape_{key}.py\n"
        f"     (strategy {strategy!r}"
        f"{' + discovery script' if strategy == 'sitemap_crawl' else ''}).\n"
        f"  2. Run: python ingestion/scrape_{key}.py\n"
        "  3. Rebuild: uv run python ingestion/ingest_docs.py\n"
        "  4. Validate: uv run python scripts/validate_source_manifest.py"
        " && uv run python scripts/validate_release.py"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
