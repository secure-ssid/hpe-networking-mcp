"""Opt-in Chromium checks; the dedicated browser CI job always enables them."""

from __future__ import annotations

import copy
import os
import re
import xml.etree.ElementTree as ET

import pytest

from hpe_networking_mcp.mcp_servers.design_lib.model import parse_model
from hpe_networking_mcp.mcp_servers.design_lib.preview_renderer import render_topology_html

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_TOPOLOGY_BROWSER_TESTS") != "1",
    reason="Set RUN_TOPOLOGY_BROWSER_TESTS=1 after installing Playwright Chromium",
)

MODEL = {
    "title": "Branch network review",
    "nodes": [
        {"id": "internet", "label": "Internet", "role": "internet", "vendor": "generic"},
        {"id": "edge", "label": "Branch edge", "role": "firewall", "vendor": "hpe"},
        {"id": "core", "label": "Campus core", "role": "core_switch", "vendor": "aruba"},
        {"id": "identity", "label": "ClearPass policy", "role": "clearpass", "vendor": "clearpass"},
        {"id": "west", "label": "West access", "role": "access_switch", "vendor": "aruba"},
        {"id": "east", "label": "East access", "role": "access_switch", "vendor": "juniper"},
        {"id": "west-ap", "label": "West wireless", "role": "campus_ap", "vendor": "aruba"},
        {"id": "east-ap", "label": "East wireless", "role": "mist_ap", "vendor": "mist"},
    ],
    "links": [
        {"source": "internet", "target": "edge", "link_type": "wan", "bandwidth": "1 Gbps"},
        {"source": "edge", "target": "core", "link_type": "trunk", "bandwidth": "10 Gbps"},
        {"source": "core", "target": "identity", "link_type": "logical", "label": "RADIUS"},
        {"source": "core", "target": "west", "link_type": "trunk", "bandwidth": "10 Gbps"},
        {"source": "core", "target": "east", "link_type": "trunk", "bandwidth": "10 Gbps"},
        {"source": "west", "target": "west-ap", "link_type": "ethernet", "bandwidth": "2.5 Gbps"},
        {"source": "east", "target": "east-ap", "link_type": "ethernet", "bandwidth": "2.5 Gbps"},
    ],
    "groups": [
        {"id": "west-site", "label": "West Site", "members": ["west", "west-ap"]},
        {"id": "east-site", "label": "East Site", "members": ["east", "east-ap"]},
    ],
}

GEOMETRY_CHECK = """model => {
  const boxes = Object.fromEntries(model.nodes.map(node => [
    node.id, document.querySelector('#node-' + node.id + ' rect').getBoundingClientRect()
  ]));
  const crossings = [];
  [...document.querySelectorAll('.topology-link-group')].forEach((group, index) => {
    const link = model.links[index];
    for (const geometry of group.querySelectorAll('line,path,polyline')) {
      const matrix = geometry.getScreenCTM();
      const length = geometry.getTotalLength();
      for (const [id, box] of Object.entries(boxes)) {
        if (id === link.source || id === link.target) continue;
        for (let step = 1; step < 200; step++) {
          const point = geometry.getPointAtLength(length * step / 200).matrixTransform(matrix);
          if (point.x > box.left + 2 && point.x < box.right - 2 &&
              point.y > box.top + 2 && point.y < box.bottom - 2) {
            crossings.push({source: link.source, target: link.target, crosses: id});
            break;
          }
        }
      }
    }
  });
  const overlaps = [];
  for (const group of model.groups) {
    const label = document.querySelector('#group-' + group.id + ' text').getBoundingClientRect();
    for (const id of group.members) {
      const box = boxes[id];
      if (label.left < box.right && label.right > box.left &&
          label.top < box.bottom && label.bottom > box.top) overlaps.push([group.id, id]);
    }
  }
  const intersects = (a, b) => a.left < b.right && a.right > b.left &&
    a.top < b.bottom && a.bottom > b.top;
  const labels = [...document.querySelectorAll('.link-label')].map(element => ({
    id: element.parentElement.id, box: element.getBoundingClientRect()
  }));
  const labelOverlaps = [];
  labels.forEach((label, index) => {
    for (const [id, box] of Object.entries(boxes))
      if (intersects(label.box, box)) labelOverlaps.push([label.id, id]);
    for (const other of labels.slice(index + 1))
      if (intersects(label.box, other.box)) labelOverlaps.push([label.id, other.id]);
  });
  return {crossings, overlaps, labelOverlaps};
}"""

PRESENTATION = """() => [...document.querySelectorAll('.topology-node')].map(node => {
  const box = getComputedStyle(node.querySelector('rect'));
  const text = getComputedStyle(node.querySelector('text'));
  return {id: node.id, fill: box.fill, stroke: box.stroke, textFill: text.fill,
          fontSize: text.fontSize, fontFamily: text.fontFamily};
})"""


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def page(browser):
    context = browser.new_context(
        viewport={"width": 1440, "height": 900}, color_scheme="light", accept_downloads=True
    )
    errors = []
    requests = []
    context.route(re.compile(r"^https?://"), lambda route: route.abort())
    context.on(
        "request",
        lambda request: (
            requests.append(request.url) if request.url.startswith(("http:", "https:")) else None
        ),
    )
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        yield page
        assert errors == []
        assert requests == []
    finally:
        context.close()


@pytest.fixture
def preview(tmp_path):
    path = tmp_path / "branch.html"
    path.write_text(render_topology_html(parse_model(MODEL)), encoding="utf-8")
    return path.as_uri()


@pytest.mark.parametrize("width,height", [(390, 844), (1440, 900), (1920, 1080), (2048, 1320)])
def test_preview_fits_viewport(page, preview, width, height):
    page.set_viewport_size({"width": width, "height": height})
    page.goto(preview)
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    box = page.locator(".svg-container").bounding_box()
    assert box is not None
    assert box["height"] <= min(722, height * 0.8)
    table = page.get_by_role("region", name="Nodes table")
    table.focus()
    assert table.evaluate("element => element.scrollWidth") >= table.evaluate(
        "element => element.clientWidth"
    )


def test_preview_geometry_and_keyboard_actions(page, preview):
    page.goto(preview)
    assert page.evaluate(GEOMETRY_CHECK, MODEL) == {
        "crossings": [],
        "overlaps": [],
        "labelOverlaps": [],
    }
    for index, node in enumerate(MODEL["nodes"]):
        element = page.locator("#node-" + node["id"])
        assert element.get_attribute("tabindex") == "0"
        element.focus()
        element.press("Enter" if index % 2 == 0 else "Space")
        assert node["label"] in page.locator("#details-content").inner_text()

    viewport = page.locator("#svg-viewport")
    page.get_by_role("button", name="Reset view").click()
    original = viewport.get_attribute("transform")
    page.get_by_role("button", name="Zoom in", exact=True).click()
    assert viewport.get_attribute("transform") != original
    page.get_by_role("button", name="Reset view").click()
    assert viewport.get_attribute("transform") == original

    page.get_by_role("textbox", name="Search topology nodes").fill("west-ap")
    assert page.locator(".topology-node.dimmed").count() == len(MODEL["nodes"]) - 1
    incoming = page.locator("#link-5-west-west-ap")
    assert "dimmed" not in (incoming.get_attribute("class") or "").split()
    assert incoming.locator(".topology-link").get_attribute("data-target") == "west-ap"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_downloaded_svg_preserves_presentation(page, preview, tmp_path, theme):
    page.emulate_media(color_scheme=theme)
    page.goto(preview)
    expected = page.evaluate(PRESENTATION)
    assert all(node["fill"] not in {"none", "rgb(0, 0, 0)"} for node in expected)
    with page.expect_download() as download:
        page.get_by_role("button", name="Download SVG").click()
    path = tmp_path / "download.svg"
    download.value.save_as(path)
    assert ET.parse(path).getroot().tag == "{http://www.w3.org/2000/svg}svg"
    page.goto(path.as_uri())
    assert page.evaluate(PRESENTATION) == expected


def test_no_javascript_fallback(browser, preview):
    context = browser.new_context(java_script_enabled=False)
    try:
        page = context.new_page()
        page.goto(preview)
        assert page.locator(".topology-node").count() == len(MODEL["nodes"])
        assert page.locator("#nodes-table tbody tr").count() == len(MODEL["nodes"])
        assert page.locator(".table-wrapper").count() == 3
    finally:
        context.close()


def test_hostile_text_cannot_execute(page):
    model = copy.deepcopy(MODEL)
    payload = '</script><img src="https://example.invalid/x" onerror="window.injected=1">'
    model["title"] = payload
    model["nodes"][0]["label"] = payload
    model["notes"] = [payload]
    page.set_content(render_topology_html(parse_model(model)))
    assert page.evaluate("window.injected") is None
    assert page.locator("img").count() == 0
