#!/usr/bin/env python3
"""
Scrape New Central docs and convert to markdown for RAG.
Uses Python's standard urllib client to fetch rendered-enough HTML (ReadMe docs are mostly SSR),
then pandoc to convert to clean markdown.
"""
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent / "sources" / "developer_docs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
}

DOC_PAGES = [
    "https://developer.arubanetworks.com/new-central/docs/about",
    "https://developer.arubanetworks.com/new-central/docs/alert-with-webhooks",
    "https://developer.arubanetworks.com/new-central/docs/building-a-local-mcp-server",
    "https://developer.arubanetworks.com/new-central/docs/central-github-copilot-setup",
    "https://developer.arubanetworks.com/new-central/docs/central-large-public-venue-dashboard",
    "https://developer.arubanetworks.com/new-central/docs/central-mcp-claude-code-setup",
    "https://developer.arubanetworks.com/new-central/docs/central-mcp-claude-desktop-setup",
    "https://developer.arubanetworks.com/new-central/docs/central-mcp-server",
    "https://developer.arubanetworks.com/new-central/docs/central-mcp-server-in-action",
    "https://developer.arubanetworks.com/new-central/docs/central-mcp-setup",
    "https://developer.arubanetworks.com/new-central/docs/central-webhooks-with-servicenow",
    "https://developer.arubanetworks.com/new-central/docs/client-disconnection",
    "https://developer.arubanetworks.com/new-central/docs/cluster-alerts",
    "https://developer.arubanetworks.com/new-central/docs/configuration-apis-collection",
    "https://developer.arubanetworks.com/new-central/docs/configure-tunneled-ssid-workflow",
    "https://developer.arubanetworks.com/new-central/docs/cutover-validation",
    "https://developer.arubanetworks.com/new-central/docs/device-function-and-persona",
    "https://developer.arubanetworks.com/new-central/docs/getting-started-with-mcp-servers",
    "https://developer.arubanetworks.com/new-central/docs/getting-started-with-rest-apis",
    "https://developer.arubanetworks.com/new-central/docs/getting-started-with-webhooks",
    "https://developer.arubanetworks.com/new-central/docs/hierarchy-visualizer",
    "https://developer.arubanetworks.com/new-central/docs/how-to-get-scope-ids",
    "https://developer.arubanetworks.com/new-central/docs/introduction-to-configuration-apis",
    "https://developer.arubanetworks.com/new-central/docs/lan-alerts",
    "https://developer.arubanetworks.com/new-central/docs/making-api-calls",
    "https://developer.arubanetworks.com/new-central/docs/mrt-apis-collection",
    "https://developer.arubanetworks.com/new-central/docs/new-central-hierarchy",
    "https://developer.arubanetworks.com/new-central/docs/onboarding",
    "https://developer.arubanetworks.com/new-central/docs/ping-iperf-troubleshooting-test",
    "https://developer.arubanetworks.com/new-central/docs/postman-collection",
    "https://developer.arubanetworks.com/new-central/docs/profile-operations",
    "https://developer.arubanetworks.com/new-central/docs/rename-hostnames",
    "https://developer.arubanetworks.com/new-central/docs/routing-alerts",
    "https://developer.arubanetworks.com/new-central/docs/security-alerts",
    "https://developer.arubanetworks.com/new-central/docs/streaming-api-cloudevents",
    "https://developer.arubanetworks.com/new-central/docs/streaming-api-connection-management",
    "https://developer.arubanetworks.com/new-central/docs/streaming-api-event-ap-monitoring",
    "https://developer.arubanetworks.com/new-central/docs/streaming-api-event-audit-trail",
    "https://developer.arubanetworks.com/new-central/docs/streaming-api-event-geofence",
    "https://developer.arubanetworks.com/new-central/docs/streaming-api-event-location",
    "https://developer.arubanetworks.com/new-central/docs/streaming-api-event-location-analytics",
    "https://developer.arubanetworks.com/new-central/docs/streaming-api-getting-started",
    "https://developer.arubanetworks.com/new-central/docs/streaming-api-postman",
    "https://developer.arubanetworks.com/new-central/docs/streaming-modes",
    "https://developer.arubanetworks.com/new-central/docs/system-alerts-2",
    "https://developer.arubanetworks.com/new-central/docs/wan-alerts",
    "https://developer.arubanetworks.com/new-central/docs/webhook-authentication",
    "https://developer.arubanetworks.com/new-central/docs/what-are-library-profiles",
    "https://developer.arubanetworks.com/new-central/docs/what-are-local-profiles",
    "https://developer.arubanetworks.com/new-central/docs/wlan-alerts",
    "https://developer.arubanetworks.com/new-central/docs/wlan-config-open-ssid",
    "https://developer.arubanetworks.com/new-central/docs/wlan-wpa3-psk",
    "https://developer.arubanetworks.com/new-central/docs/working-with-library-profiles",
    "https://developer.arubanetworks.com/new-central/docs/working-with-local-profiles",
]

def slug_from_url(url):
    return re.sub(r"[^a-z0-9_-]", "_", url.split("//")[1].replace("/", "_").lower())

def fetch_html(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")

def extract_content(html):
    """Extract the dehydrated article content from ReadMe.io pages."""
    import html as htmllib
    # Primary: grab the dehydrated content div (encoded HTML in attribute)
    m = re.search(r'dehydrated="(.*?)"(?=\s*>)', html, re.DOTALL)
    if m:
        inner = htmllib.unescape(m.group(1))
        # Also grab the H1 title from the article header
        title_m = re.search(r'<article[^>]*>.*?<h1[^>]*>(.*?)</h1>', html, re.DOTALL)
        title_html = f"<h1>{title_m.group(1)}</h1>\n" if title_m else ""
        return title_html + inner
    # Fallback: extract the full <article> tag
    m2 = re.search(r'<article[^>]*>(.*?)</article>', html, re.DOTALL)
    if m2:
        return m2.group(1)
    return html

def html_to_markdown(html, url):
    content = extract_content(html)
    result = subprocess.run(
        ["pandoc", "-f", "html", "-t", "markdown_strict", "--wrap=none"],
        input=content.encode(),
        capture_output=True,
        timeout=30,
    )
    md = result.stdout.decode("utf-8", errors="replace")
    md = f"<!-- source: {url} -->\n\n" + md
    return md

def scrape_page(url):
    slug = slug_from_url(url)
    out_path = OUTPUT_DIR / f"{slug}.md"
    try:
        html = fetch_html(url)
        md = html_to_markdown(html, url)
        out_path.write_text(md, encoding="utf-8")
        print(f"  OK: {slug} ({len(md)} chars)")
    except Exception as e:
        print(f"  ERROR {url}: {e}")
    time.sleep(0.5)

def main():
    print(f"Scraping {len(DOC_PAGES)} pages -> {OUTPUT_DIR}")
    for i, url in enumerate(DOC_PAGES, 1):
        print(f"[{i}/{len(DOC_PAGES)}] {url}")
        scrape_page(url)
    print("Done.")

if __name__ == "__main__":
    main()
