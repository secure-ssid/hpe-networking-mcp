from __future__ import annotations

from ingestion.discover_junos_cli_urls import discover_urls
from ingestion.scrape_mist_api_docs import iter_pages, render_page


def test_junos_cli_toc_discovery_filters_and_normalizes_urls():
    script = """
    var __data = {
      "toc": {
        "items": [
          {"url": "/documentation/us/en/software/junos/cli-reference/a.html#syntax"},
          {"url": "b.html"},
          {"url": "https://www.juniper.net/documentation/us/en/software/mist/api/no.html"},
          {"url": "https://example.com/outside.html"},
          {"url": "notes.txt"}
        ]
      }
    };
    """

    assert discover_urls(script) == [
        "https://www.juniper.net/documentation/us/en/software/junos/cli-reference/a.html",
        "https://www.juniper.net/documentation/us/en/software/junos/cli-reference/b.html",
    ]


def test_mist_virtual_pages_and_endpoint_rendering_are_stable():
    sections = [
        {
            "Title": "API",
            "SuggestedLink": "$h/api",
            "Nodes": [
                {
                    "Title": "Get Widget",
                    "Type": "endpointreference",
                    "Description": "Returns a widget.",
                    "MethodSignature": {"Text": "GET /api/v1/widgets/{id}", "Language": "http"},
                    "Parameters": [
                        {
                            "Name": "id",
                            "DataType": "String",
                            "ParamType": "Path",
                            "Required": True,
                            "Description": "Widget identifier.",
                        }
                    ],
                    "Response": [
                        {
                            "StatusCode": "200",
                            "Description": "OK",
                            "Headers": [],
                            "Content": [
                                {
                                    "ContentType": "application/json",
                                    "DataType": "Widget",
                                    "Example": {
                                        "Text": '{"id": "widget-1"}',
                                        "Language": "json",
                                    },
                                    "Examples": [],
                                }
                            ],
                        }
                    ],
                    "Errors": [{"StatusCode": "404", "Description": "Not found"}],
                    "UsageExample": {
                        "HttpCallTemplate": '{"method":"GET"}',
                        "Templates": {"HTTP_CURL_V1": "curl https://api.mist.com/api/v1/widgets/1"},
                    },
                }
            ],
        }
    ]

    pages = iter_pages(sections)
    assert [link for link, _ in pages] == ["$h/api"]

    path_one, text_one = render_page(pages[0][0], pages[0][1], "https://example.test/docs.json")
    path_two, text_two = render_page(pages[0][0], pages[0][1], "https://example.test/docs.json")

    assert path_one == path_two
    assert text_one == text_two
    assert "GET /api/v1/widgets/{id}" in text_one
    assert "| id | String | Path | yes | Widget identifier. |" in text_one
    assert '{"id": "widget-1"}' in text_one
    assert "curl https://api.mist.com/api/v1/widgets/1" in text_one
    assert "404" in text_one


def test_mist_websocket_event_rendering_is_stable():
    from ingestion.scrape_mist_api_docs import render_page

    node = {
        "Title": "Streaming events",
        "SuggestedLink": "$h/websocket/events",
        "Nodes": [
            {
                "Title": "Client Join",
                "Type": "eventreference",
                "Description": "Fired when a client joins.",
                "Headers": [
                    {
                        "Name": "topic",
                        "DataType": "String",
                        "ParamType": "Header",
                        "Required": True,
                        "Description": "Event topic.",
                    }
                ],
                "Payload": {
                    "Type": "eventpayloadreference",
                    "Title": "Join payload",
                    "Fields": [
                        {
                            "Name": "mac",
                            "DataType": "String",
                            "ParamType": "Body",
                            "Required": True,
                            "Description": "Client MAC.",
                        }
                    ],
                },
                "PayloadExamples": [
                    {"Text": '{"mac": "aa:bb:cc:dd:ee:ff"}', "Language": "json"}
                ],
            }
        ],
    }

    path, text = render_page(
        "$h/websocket/events", node, "https://example.test/mist-api.json"
    )
    assert path.name.endswith(".md")
    assert "Fired when a client joins." in text
    assert "| topic | String | Header | yes | Event topic. |" in text
    assert "### Payload fields" in text
    assert "| mac | String | Body | yes | Client MAC. |" in text
    assert '{"mac": "aa:bb:cc:dd:ee:ff"}' in text


def test_junos_missing_topic_body_is_a_parser_error():
    from ingestion.scrape_junos_cli import extract_html

    try:
        extract_html("<html><body><p>no topic region</p></body></html>")
    except ValueError as exc:
        assert "topicBody" in str(exc) or "topic-content" in str(exc)
    else:
        raise AssertionError("missing topic body must raise ValueError")


def test_junos_scrape_page_classifies_parser_errors(monkeypatch, tmp_path):
    from ingestion import scrape_junos_cli as scraper

    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(
        scraper,
        "fetch_html",
        lambda url: "<html><body><p>no topic region</p></body></html>",
    )
    url = (
        "https://www.juniper.net/documentation/us/en/software/"
        "junos/cli-reference/a.html"
    )
    result = scraper.scrape_page(url, delay=0, skip_existing=False)
    assert result.startswith("PARSER_ERROR")


def test_committed_refresh_plan_runs_junos_and_mist_discovery_before_scrape():
    from scripts import refresh_rag_sources as refresh

    manifest = refresh.load_manifest()
    pairs = (
        (
            "junos_cli",
            "ingestion/discover_junos_cli_urls.py",
            "ingestion/scrape_junos_cli.py",
        ),
        (
            "mist_api_docs",
            "ingestion/discover_mist_api_docs.py",
            "ingestion/scrape_mist_api_docs.py",
        ),
    )
    for source, discover, scrape in pairs:
        plan = refresh.build_plan([source], manifest=manifest, refresh_sources=True)
        commands = [
            step["command"]
            for step in plan["steps"]
            if step["kind"] in {refresh.STEP_EXTRA_SCRIPT, refresh.STEP_SCRAPER}
        ]
        assert commands[:2] == [[discover], [scrape]], source


def test_rag_vendor_maps_include_junos_and_mist_api_docs():
    from hpe_networking_mcp.mcp_servers.rag import _SOURCE_BOOST, _SOURCE_VENDOR
    from ingestion.ingest_docs import SOURCE_META

    assert SOURCE_META["junos_cli"] == "junos-cli"
    assert SOURCE_META["mist_api_docs"] == "mist-api-docs"
    assert _SOURCE_VENDOR["junos_cli"] == "juniper"
    assert _SOURCE_VENDOR["mist_api_docs"] == "juniper"
    assert _SOURCE_BOOST["mist_api_docs"] == 0.10
    assert _SOURCE_BOOST["junos_cli"] == 0.0
