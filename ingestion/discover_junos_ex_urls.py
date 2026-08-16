#!/usr/bin/env python3
"""
Discover URLs for Juniper EX-series hardware guides and EX-specific Junos
release notes.

Same juniper.net documentation sitemap crawl as discover_mist_docs_urls.py
(https://www.juniper.net/documentation/sitemap/sitemap.xml -> 19 per-locale
sub-sitemaps), confirmed via direct urllib probe to need no Playwright (no
403/bot-gate on this host, unlike the Aruba techdocs/support.hpe.com hosts).
Two independent path/name filters are applied to the same crawl:

1. EX HARDWARE GUIDE (`/documentation/us/en/hardware/ex*/`): the physical
   install/maintenance book for every EX chassis family (site guidelines,
   cabling, power, safety, chassis component replacement), plus a handful of
   "configure Junos OS" first-boot pages per model. Confirmed 723 pages
   across all EX hardware families (ex2300, ex3400, ex4000, ex4100(-f/-h),
   ex4300, ex4400, ex4600, ex4650, ex9204/9208/9214/9251/9253).

2. EX JUNOS RELEASE NOTES (`/documentation/us/en/software/junos/
   release-notes/`, filename containing an "ex-" platform tag): per-version
   new-features/resolved-issues/open-issues/what-changed pages specific to
   the EX platform. The full release-notes tree is much larger (6,627 pages
   across ALL Junos platforms: ACX/MX/NFX/PTX/QFX/SRX/SSR/JRR plus ~3,200
   platform-generic feature pages) -- by the same "bound to this project's
   switch/AP focus" reasoning already applied to aoscx_release_notes and
   mist_product_updates, only the 193 pages whose filename carries an
   explicit "ex-" platform tag are kept. The other ~6,400 pages (other
   platforms' specific content, plus platform-generic feature pages that may
   or may not apply to EX) are a deliberate exclusion, not an oversight --
   see source_manifest.json for the full reasoning if that scope needs
   revisiting later.

Both page families use the same DITA template as mist_docs (topicBody div),
confirmed via direct sampling.

Outputs: ingestion/junos_ex_hardware_urls.json and
ingestion/junos_ex_release_notes_urls.json.
"""
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}

SITEMAP_INDEX = "https://www.juniper.net/documentation/sitemap/sitemap.xml"
_LOC_RE = re.compile(r"<loc>\s*(https?://[^\s<]+)\s*</loc>")

HARDWARE_PREFIX = "/documentation/us/en/hardware/ex"
RELEASE_NOTES_PREFIX = "/documentation/us/en/software/junos/release-notes/"
_EX_FILENAME_RE = re.compile(
    r"[/-]ex-|ex-series|-ex\.html|ex-what|ex-new|ex-open|ex-resolv|ex-upgrade"
)

HW_OUT = Path(__file__).parent / "junos_ex_hardware_urls.json"
RN_OUT = Path(__file__).parent / "junos_ex_release_notes_urls.json"


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def main():
    print(f"Fetching sitemap index: {SITEMAP_INDEX}")
    index_xml = fetch(SITEMAP_INDEX)
    sub_sitemaps = _LOC_RE.findall(index_xml)
    print(f"  {len(sub_sitemaps)} sub-sitemaps")

    hardware: set[str] = set()
    release_notes: set[str] = set()
    for i, sm_url in enumerate(sub_sitemaps, 1):
        try:
            xml = fetch(sm_url)
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"  [{i}/{len(sub_sitemaps)}] ERROR {sm_url}: {e}")
            continue
        locs = _LOC_RE.findall(xml)
        hw_found = 0
        rn_found = 0
        for loc in locs:
            if HARDWARE_PREFIX in loc and loc.endswith(".html"):
                hardware.add(loc)
                hw_found += 1
            elif RELEASE_NOTES_PREFIX in loc and loc.endswith(".html"):
                filename = loc.rsplit("/", 1)[-1]
                if _EX_FILENAME_RE.search(filename):
                    release_notes.add(loc)
                    rn_found += 1
        print(
            f"  [{i}/{len(sub_sitemaps)}] {sm_url.rsplit('/', 1)[-1]}: "
            f"+{hw_found} hw, +{rn_found} ex-rn"
        )
        time.sleep(0.15)

    hw_urls = sorted(hardware)
    rn_urls = sorted(release_notes)
    HW_OUT.write_text(json.dumps(hw_urls, indent=2) + "\n", encoding="utf-8")
    RN_OUT.write_text(json.dumps(rn_urls, indent=2) + "\n", encoding="utf-8")
    print(f"\nEX hardware guide: {len(hw_urls)} pages -> {HW_OUT}")
    print(f"EX release notes:  {len(rn_urls)} pages -> {RN_OUT}")


if __name__ == "__main__":
    main()
