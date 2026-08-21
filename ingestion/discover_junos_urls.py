#!/usr/bin/env python3
"""
Discover URLs for Juniper EX/MX/QFX/SRX hardware guides and their
platform-tagged Junos release notes.

Same juniper.net documentation sitemap crawl as discover_mist_docs_urls.py
(https://www.juniper.net/documentation/sitemap/sitemap.xml -> 19 per-locale
sub-sitemaps), confirmed via direct urllib probe to need no Playwright (no
403/bot-gate on this host, unlike the Aruba techdocs/support.hpe.com hosts).

Formerly discover_junos_ex_urls.py (EX-only). Generalized in place once EX
proved the pattern, to cover the switch (EX/QFX), router (MX), and firewall
(SRX, plus its vSRX/cSRX virtual-form release notes) platforms the original
EX-only docstring already flagged as "a candidate for later reconsideration"
(see ingestion/source_manifest.json's junos_ex_release_notes notes). Four
independent path/name filter pairs are applied to the same single crawl:

1. HARDWARE GUIDE (`/documentation/us/en/hardware/<tag>*/`): the physical
   install/maintenance book for every chassis family of that platform (site
   guidelines, cabling, power, safety, chassis component replacement), plus a
   handful of "configure Junos OS" first-boot pages per model. SRX has no
   separate vsrx/csrx hardware prefix -- those are virtual/software-only
   firewall forms with no physical install guide.

2. JUNOS RELEASE NOTES (`/documentation/us/en/software/junos/
   release-notes/`, filename containing an explicit platform tag): per-version
   new-features/resolved-issues/open-issues/what-changed pages specific to
   that platform. The full release-notes tree is much larger (6,627 pages
   across ALL Junos platforms: ACX/MX/NFX/PTX/QFX/SRX/SSR/JRR plus ~3,200
   platform-generic feature pages) -- by the same "bound to this project's
   switch/AP focus" reasoning already applied to aoscx_release_notes and
   mist_product_updates, only pages whose filename carries one of this
   platform's explicit tags are kept. SRX also pulls in vsrx-/csrx--tagged
   pages (same firewall product line, virtual deployment forms). The other
   platforms (ACX/NFX/PTX/SSR/JRR) plus the ~3,200 platform-generic feature
   pages remain a deliberate exclusion -- see source_manifest.json for the
   full reasoning if that scope needs revisiting later.

All page families use the same DITA template as mist_docs (topicBody div),
confirmed via direct sampling on the original EX pages; re-confirmed for a
sample of MX/QFX/SRX pages before this script was relied on for those
platforms.

Outputs one `junos_<platform>_hardware_urls.json` and one
`junos_<platform>_release_notes_urls.json` per platform in PLATFORMS below.
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

RELEASE_NOTES_PREFIX = "/documentation/us/en/software/junos/release-notes/"
BASE = Path(__file__).parent


def _release_notes_pattern(tags: list[str]) -> re.Pattern:
    """Build the same "does this filename carry one of this platform's
    explicit tags" pattern the original EX-only script hand-wrote, generalized
    over one or more filename tags (SRX also matches its vsrx/csrx virtual
    forms)."""
    alternatives = []
    for tag in tags:
        alternatives.append(
            rf"[/-]{tag}-|{tag}-series|-{tag}\.html|{tag}-what|{tag}-new|{tag}-open|"
            rf"{tag}-resolv|{tag}-upgrade"
        )
    return re.compile("|".join(alternatives))


# platform key -> (hardware URL path prefix, release-notes filename tags)
PLATFORMS: dict[str, dict[str, object]] = {
    "ex": {
        "hardware_prefix": "/documentation/us/en/hardware/ex",
        "release_notes_re": _release_notes_pattern(["ex"]),
    },
    "mx": {
        "hardware_prefix": "/documentation/us/en/hardware/mx",
        "release_notes_re": _release_notes_pattern(["mx"]),
    },
    "qfx": {
        "hardware_prefix": "/documentation/us/en/hardware/qfx",
        "release_notes_re": _release_notes_pattern(["qfx"]),
    },
    "srx": {
        "hardware_prefix": "/documentation/us/en/hardware/srx",
        "release_notes_re": _release_notes_pattern(["srx", "vsrx", "csrx"]),
    },
}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def main():
    print(f"Fetching sitemap index: {SITEMAP_INDEX}")
    index_xml = fetch(SITEMAP_INDEX)
    sub_sitemaps = _LOC_RE.findall(index_xml)
    print(f"  {len(sub_sitemaps)} sub-sitemaps")

    hardware: dict[str, set[str]] = {key: set() for key in PLATFORMS}
    release_notes: dict[str, set[str]] = {key: set() for key in PLATFORMS}
    for i, sm_url in enumerate(sub_sitemaps, 1):
        try:
            xml = fetch(sm_url)
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"  [{i}/{len(sub_sitemaps)}] ERROR {sm_url}: {e}")
            continue
        locs = _LOC_RE.findall(xml)
        counts = {key: [0, 0] for key in PLATFORMS}
        for loc in locs:
            if not loc.endswith(".html"):
                continue
            for key, cfg in PLATFORMS.items():
                if cfg["hardware_prefix"] in loc:
                    hardware[key].add(loc)
                    counts[key][0] += 1
                elif RELEASE_NOTES_PREFIX in loc:
                    filename = loc.rsplit("/", 1)[-1]
                    if cfg["release_notes_re"].search(filename):
                        release_notes[key].add(loc)
                        counts[key][1] += 1
        summary = ", ".join(
            f"+{counts[key][0]} {key}-hw, +{counts[key][1]} {key}-rn" for key in PLATFORMS
        )
        print(f"  [{i}/{len(sub_sitemaps)}] {sm_url.rsplit('/', 1)[-1]}: {summary}")
        time.sleep(0.15)

    for key in PLATFORMS:
        hw_urls = sorted(hardware[key])
        rn_urls = sorted(release_notes[key])
        hw_out = BASE / f"junos_{key}_hardware_urls.json"
        rn_out = BASE / f"junos_{key}_release_notes_urls.json"
        hw_out.write_text(json.dumps(hw_urls, indent=2) + "\n", encoding="utf-8")
        rn_out.write_text(json.dumps(rn_urls, indent=2) + "\n", encoding="utf-8")
        print(f"\n{key.upper()} hardware guide: {len(hw_urls)} pages -> {hw_out}")
        print(f"{key.upper()} release notes:  {len(rn_urls)} pages -> {rn_out}")


if __name__ == "__main__":
    main()
