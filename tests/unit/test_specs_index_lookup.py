"""Unit tests for specs_index.lookup() — the engine behind the lookup_api tool.

Builds a tiny OpenAPI spec fixture into a temp SQLite DB, so tests run without
the real data/specs.sqlite (gitignored) or any network.

Test bar:
- _query_stems drops question scaffolding, keeps domain terms, folds plurals,
  and expands hyphenated tokens into token + components
- exact enum hit: field-like term -> authoritative enum list, ranked first
- endpoint trim: device-firmware-upgrade resolves to the /device-firmware path
- relevance threshold: an off-corpus query returns [] (caller falls back to
  search_docs) instead of plausible-but-wrong rows
- exact-hit corroboration: a lone field-name collision on a multi-term query
  is dropped (the MVRP "registration" regression)
- missing DB raises FileNotFoundError with build instructions
"""

from __future__ import annotations

import json

import pytest

from hpe_networking_mcp.pipeline.clients import specs_index

FIXTURE_SPECS = {
    "cda-auth-profile.json": {
        "info": {"title": "CDA Auth Profile"},
        "servers": [{"url": "https://example.test/cda"}],
        "paths": {
            "/auth-profiles/{name}": {
                "patch": {
                    "operationId": "updateAuthProfile",
                    "summary": "Update auth profile",
                    "description": "Update an existing CDA auth profile.",
                },
            },
        },
        "components": {"schemas": {
            "CdaAuthProfile": {
                "description": "CDA authentication profile.",
                "properties": {
                    "auth-type": {
                        "type": "string",
                        "description": "Authentication type for the profile.",
                        "enum": ["MPSK", "EAP", "CAPTIVE_PORTAL", "MAB"],
                    },
                },
            },
        }},
    },
    "firmware-management.json": {
        "info": {"title": "Firmware Management"},
        "servers": [{"url": "https://example.test/config"}],
        "paths": {
            "/device-firmware": {
                "post": {
                    "operationId": "createDeviceFirmware",
                    "summary": "Create device firmware settings",
                    "description": "Configure device firmware for a scope.",
                },
                "patch": {
                    "operationId": "updateDeviceFirmware",
                    "summary": "Update device firmware settings",
                    "description": "Update device firmware for a scope.",
                },
            },
        },
        "components": {"schemas": {}},
    },
    "interface-ethernet.json": {
        "info": {"title": "Interface Ethernet"},
        "servers": [{"url": "https://example.test/config"}],
        "paths": {},
        "components": {"schemas": {
            "MvrpInterfaceConfig": {
                "description": "MVRP interface settings.",
                "properties": {
                    "registration": {
                        "type": "string",
                        "description": "MVRP registrar state machine control.",
                        "enum": ["NORMAL", "FIXED", "FORBIDDEN"],
                    },
                },
            },
        }},
    },
}

VERSIONED_SOURCE_SPECS = {
    "openapi_specs": {
        "central-firmware-v26-04.json": {
            "info": {"title": "Central Firmware", "version": "v1alpha1"},
            "servers": [{"url": "https://apigw-prod2.central.arubanetworks.com"}],
            "paths": {
                "/device-firmware": {
                    "patch": {
                        "operationId": "updateDeviceFirmware",
                        "summary": "Update device firmware settings",
                        "description": "Update device firmware for Central 26.04.",
                    },
                },
            },
            "components": {
                "schemas": {
                    "FirmwareProfile": {
                        "description": "Firmware settings profile.",
                        "properties": {
                            "mode": {
                                "type": "string",
                                "description": "Upgrade mode.",
                                "enum": ["AUTO", "MANUAL"],
                            },
                        },
                    },
                },
            },
        },
        "central-firmware-v26-04-mirror.json": {
            "info": {"title": "Central Firmware Mirror", "version": "v1alpha1"},
            "servers": [{"url": "https://apigw-prod2.central.arubanetworks.com"}],
            "paths": {
                "/device-firmware": {
                    "patch": {
                        "operationId": "updateDeviceFirmware",
                        "summary": "Update device firmware settings",
                        "description": "Mirror bundle for the same Central 26.04 endpoint.",
                    },
                },
            },
            "components": {
                "schemas": {
                    "FirmwareProfile": {
                        "description": "Firmware settings profile (mirror).",
                        "properties": {
                            "mode": {
                                "type": "string",
                                "description": "Upgrade mode (mirror).",
                                "enum": ["AUTO", "MANUAL"],
                            },
                        },
                    },
                },
            },
        },
    },
    "product_specs": {
        "cppm-enforcement.json": {
            "info": {"title": "ClearPass Enforcement", "version": "v1"},
            "servers": [{"url": "https://clearpass.example.test/api"}],
            "paths": {
                "/tips/role-mappings": {
                    "get": {
                        "operationId": "getRoleMappings",
                        "summary": "List role mappings",
                        "description": "Retrieve exact ClearPass role mappings.",
                    },
                },
            },
            "components": {
                "schemas": {
                    "RoleMapping": {
                        "description": "Role mapping settings.",
                        "properties": {
                            "role_type": {
                                "type": "string",
                                "description": "Role action type.",
                                "enum": ["ALLOW", "DENY"],
                            },
                        },
                    },
                },
            },
        },
        "cppm-prose-only.json": {
            "info": {"title": "Narrative only"},
            "description": "This file has no exact OpenAPI paths or schemas.",
        },
    },
}

OPENAPI_MANIFEST_FIXTURE = {
    "generated_at": "2026-08-15T00:00:00+00:00",
    "registries": {
        "central-main": {
            "output_path": "ingestion/sources/openapi_specs/central-firmware-v26-04.json",
            "path_count": 1,
            "portal_version": "v26.04",
            "project": "aruba-new-central-config",
            "registry_id": "central-main",
            "source_url": "https://developer.arubanetworks.com/new-central-config/reference/device-firmware",
            "spec_version": "v1alpha1",
            "title": "Central Firmware",
        },
        "central-mirror": {
            "output_path": "ingestion/sources/openapi_specs/central-firmware-v26-04-mirror.json",
            "path_count": 1,
            "portal_version": "v26.04",
            "project": "aruba-new-central-config",
            "registry_id": "central-mirror",
            "source_url": "https://developer.arubanetworks.com/new-central-config/reference/device-firmware-mirror",
            "spec_version": "v1alpha1",
            "title": "Central Firmware Mirror",
        },
    },
}

PRODUCT_MANIFEST_FIXTURE = {
    "specs": [
        {
            "branch": "6.0",
            "output_path": "ingestion/sources/product_specs/cppm-enforcement.json",
            "path_count": 1,
            "project": "aruba-cppm",
            "section": "cppm",
            "source_url": "https://developer.arubanetworks.com/cppm/reference/get-role-mappings",
            "spec_uri": "/branches/6.0/apis/cppm-enforcement.json",
            "title": "ClearPass Enforcement",
        },
        {
            "branch": "6.0",
            "output_path": "ingestion/sources/product_specs/cppm-prose-only.json",
            "path_count": 0,
            "project": "aruba-cppm",
            "section": "cppm",
            "source_url": "https://developer.arubanetworks.com/cppm/reference/prose-only",
            "spec_uri": "/branches/6.0/apis/cppm-prose-only.json",
            "title": "Narrative only",
        },
    ],
}


@pytest.fixture
def db(tmp_path):
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    for fname, spec in FIXTURE_SPECS.items():
        (specs_dir / fname).write_text(json.dumps(spec))
    db_path = tmp_path / "specs.sqlite"
    counts = specs_index.build(specs_dir=specs_dir, db_path=db_path)
    assert counts["specs"] == 3 and counts["endpoints"] == 3
    return db_path


@pytest.fixture
def versioned_db(tmp_path):
    source_dirs = {}
    for source_family, specs in VERSIONED_SOURCE_SPECS.items():
        specs_dir = tmp_path / source_family
        specs_dir.mkdir()
        for fname, spec in specs.items():
            (specs_dir / fname).write_text(json.dumps(spec))
        source_dirs[source_family] = specs_dir

    openapi_manifest = tmp_path / "openapi_registry_manifest.json"
    openapi_manifest.write_text(json.dumps(OPENAPI_MANIFEST_FIXTURE))
    product_manifest = tmp_path / "product_specs_manifest.json"
    product_manifest.write_text(json.dumps(PRODUCT_MANIFEST_FIXTURE))

    db_path = tmp_path / "versioned.sqlite"
    counts = specs_index.build(
        db_path=db_path,
        source_dirs=source_dirs,
        manifest_paths={
            "openapi_specs": openapi_manifest,
            "product_specs": product_manifest,
        },
    )
    assert counts["specs"] == 3
    assert counts["skipped"] == 1
    return db_path


# ---------------------------------------------------------------------------
# _query_groups
# ---------------------------------------------------------------------------


class TestQueryGroups:
    def test_drops_scaffolding_keeps_domain_terms(self):
        groups = specs_index._query_groups(
            "What are the valid auth-type enum values for an auth profile?"
        )
        flat = [s for g in groups for s in g]
        assert "auth-type" in flat
        assert "profile" in flat
        # scaffolding and generic API words are gone
        for noise in ("what", "the", "valid", "enum", "values", "for"):
            assert noise not in flat

    def test_hyphen_token_is_one_group_with_components(self):
        groups = specs_index._query_groups("device-firmware-upgrade endpoint")
        # one concept -> ONE group (components must not corroborate each other)
        assert len(groups) == 1
        assert groups[0][0] == "device-firmware-upgrade"
        assert {"device", "firmware", "upgrade"} <= set(groups[0])

    def test_plural_fold_is_prefix_safe(self):
        flat = [s for g in specs_index._query_groups("passpoint profiles") for s in g]
        assert "profile" in flat  # "profiles" folded, still a prefix of original

    def test_irregular_plural_keeps_both_spellings(self):
        # neither "policy" nor "policie" alone covers the other as a prefix
        groups = specs_index._query_groups("authorization policies")
        policy_group = next(g for g in groups if any(s.startswith("polic") for s in g))
        assert "policy" in policy_group and "policie" in policy_group

    def test_no_duplicate_groups_and_no_short_or_numeric(self):
        groups = specs_index._query_groups("802.1X dot1x dot1x profile profile")
        flat = [s for g in groups for s in g]
        assert flat.count("dot1x") == 1
        assert all(len(s) >= 3 and not s.isdigit() for s in flat)

    def test_stopwords_checked_before_stemming(self):
        # Regression: "does" stemmed to "doe" BEFORE the stopword check and
        # survived as a junk concept group polluting FTS and the threshold.
        groups = specs_index._query_groups("What does the opmode field accept?")
        flat = [s for g in groups for s in g]
        assert "doe" not in flat and "does" not in flat
        assert flat == ["opmode"]  # only the real concept survives

    def test_domain_synonyms_join_the_same_group(self):
        # Regression: users say "SSID", the specs say "wlan"/"essid" — the
        # synonym must corroborate within ONE group, not add a new concept.
        groups = specs_index._query_groups("ssid opmode")
        assert len(groups) == 2
        ssid_group = next(g for g in groups if "ssid" in g)
        assert "wlan" in ssid_group and "essid" in ssid_group


# ---------------------------------------------------------------------------
# lookup
# ---------------------------------------------------------------------------


class TestLookup:
    def test_exact_method_path_returns_only_literal_operation(self, db):
        hits = specs_index.lookup("patch /device-firmware", db_path=db)

        assert len(hits) == 1
        assert hits[0]["kind"] == "endpoint"
        assert hits[0]["file_path"].endswith(
            "firmware-management.json#PATCH /device-firmware"
        )
        assert "Update device firmware settings" in hits[0]["text"]

    def test_lookup_cache_returns_independent_copies(self, db):
        specs_index.clear_lookup_cache()
        first = specs_index.lookup("patch /device-firmware", db_path=db)
        first[0]["text"] = "mutated"
        second = specs_index.lookup("patch /device-firmware", db_path=db)
        assert "mutated" not in second[0]["text"]
        assert second[0]["kind"] == "endpoint"

    def test_exact_operation_id_is_case_insensitive(self, db):
        hits = specs_index.lookup("UPDATEDEVICEFIRMWARE", db_path=db)

        assert len(hits) == 1
        assert hits[0]["file_path"].endswith(
            "firmware-management.json#PATCH /device-firmware"
        )

    def test_exact_enum_hit_ranked_first_with_full_enum_list(self, db):
        hits = specs_index.lookup(
            "What are the valid auth-type values for a CDA auth profile?", db_path=db
        )
        assert hits, "expected an exact enum hit"
        top = hits[0]
        assert top["kind"] == "enum"
        assert "cda-auth-profile.json" in top["file_path"]
        for value in ("MPSK", "EAP", "CAPTIVE_PORTAL", "MAB"):
            assert value in top["text"]

    def test_endpoint_trim_resolves_hyphenated_token(self, db):
        # No /device-firmware-upgrade path exists; one right-trim finds /device-firmware
        hits = specs_index.lookup(
            "Is there a device-firmware-upgrade endpoint?", db_path=db
        )
        assert hits
        assert any(h["kind"] == "endpoint" and "/device-firmware" in h["text"] for h in hits)
        assert any("firmware-management.json" in h["file_path"] for h in hits)

    def test_off_corpus_query_returns_empty_not_noise(self, db):
        # Nothing about BGP route reflectors in the fixture -> honest empty
        hits = specs_index.lookup(
            "How do I configure a BGP route reflector cluster identifier?", db_path=db
        )
        assert hits == []

    def test_field_name_collision_needs_corroboration(self, db):
        # "registration" matches the MVRP field name, but nothing else in the
        # query corroborates it -> must NOT surface the MVRP enums
        hits = specs_index.lookup(
            "What URL and method updates a CNAC MAC registration?", db_path=db
        )
        assert all("MvrpInterfaceConfig" not in h["file_path"] for h in hits)

    def test_results_shape_matches_search_docs_contract(self, db):
        hits = specs_index.lookup("auth-type for the CDA auth profile", db_path=db)
        for h in hits:
            assert set(h) == {"text", "source", "file_path", "kind", "score"}
            assert h["source"] == "openapi_specs"
            assert h["file_path"].startswith("openapi_specs/")
            assert "#" in h["file_path"]

    def test_include_metadata_is_opt_in_for_old_callers(self, versioned_db):
        plain = specs_index.lookup("PATCH /device-firmware", db_path=versioned_db)
        rich = specs_index.lookup(
            "PATCH /device-firmware",
            db_path=versioned_db,
            include_metadata=True,
        )

        assert set(plain[0]) == {"text", "source", "file_path", "kind", "score"}
        assert rich[0]["platform"] == "central"
        assert rich[0]["version"] == "v26.04"
        assert rich[0]["api_version"] == "v1alpha1"
        assert rich[0]["source_url"].startswith(
            "https://developer.arubanetworks.com/new-central-config/reference/"
        )

    def test_top_k_caps_results(self, db):
        hits = specs_index.lookup("cda auth profile firmware device", top_k=2, db_path=db)
        assert len(hits) <= 2

    def test_top_k_is_defensively_clamped(self, db):
        assert len(specs_index.lookup("PATCH /device-firmware", top_k=0, db_path=db)) == 1

    def test_hyphen_components_do_not_self_corroborate(self, db):
        # Regression: "auth-type" used to expand to [auth-type, auth] and count
        # twice, letting an off-corpus query return confident enum hits instead
        # of [] (which would suppress the search_docs fallback).
        hits = specs_index.lookup(
            "auth-type quantum teleportation flux capacitor", db_path=db
        )
        assert hits == []

    def test_platform_and_version_filters_select_exact_spec_family(self, versioned_db):
        hits = specs_index.lookup(
            "PATCH /device-firmware",
            db_path=versioned_db,
            platform="central",
            version="v26.04",
            include_metadata=True,
        )

        assert len(hits) == 1
        assert hits[0]["source"] == "openapi_specs"
        assert hits[0]["platform"] == "central"
        assert hits[0]["version"] == "v26.04"

        assert specs_index.lookup(
            "PATCH /device-firmware",
            db_path=versioned_db,
            platform="central",
            version="v26.05",
        ) == []

    def test_product_specs_are_exact_eligible_and_source_filterable(self, versioned_db):
        hits = specs_index.lookup(
            "GET /tips/role-mappings",
            db_path=versioned_db,
            source="product_specs",
            platform="clearpass",
            version="6.0",
            include_metadata=True,
        )

        assert len(hits) == 1
        assert hits[0]["source"] == "product_specs"
        assert hits[0]["file_path"].startswith("product_specs/")
        assert hits[0]["platform"] == "clearpass"
        assert hits[0]["version"] == "6.0"
        assert "role mappings" in hits[0]["text"].lower()
        assert (
            specs_index.lookup(
                "Narrative only",
                db_path=versioned_db,
                source="product_specs",
                platform="clearpass",
                version="6.0",
            )
            == []
        )

    def test_duplicate_endpoint_and_schema_identities_are_collapsed(self, versioned_db):
        endpoint_hits = specs_index.lookup(
            "PATCH /device-firmware",
            db_path=versioned_db,
            platform="central",
            version="v26.04",
        )
        enum_hits = specs_index.get_enum(
            "mode",
            db_path=versioned_db,
            platform="central",
            version="v26.04",
            include_metadata=True,
        )

        assert len(endpoint_hits) == 1
        assert len(enum_hits) == 1
        assert enum_hits[0]["platform"] == "central"
        assert enum_hits[0]["version"] == "v26.04"

    def test_stale_index_requires_rebuild_for_operation_id(self, tmp_path):
        import sqlite3

        stale = tmp_path / "specs.sqlite"
        conn = sqlite3.connect(stale)
        conn.execute(
            "CREATE TABLE endpoints ("
            "id INTEGER PRIMARY KEY, spec_name TEXT, spec_file TEXT, server TEXT, "
            "method TEXT, path TEXT, summary TEXT, description TEXT)"
        )
        conn.commit()
        conn.close()

        with pytest.raises(FileNotFoundError, match="rebuild-shared"):
            specs_index.lookup("updateDeviceFirmware", db_path=stale)

    def test_missing_db_raises_with_build_instructions(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="--build"):
            specs_index.lookup("anything", db_path=tmp_path / "nope.sqlite")

    def test_corrupt_db_raises_filenotfound_not_sqlite_error(self, tmp_path):
        # The MCP tool only catches FileNotFoundError — sqlite errors from a
        # present-but-corrupt file must be converted, not leak to the transport.
        bad = tmp_path / "specs.sqlite"
        bad.write_bytes(b"this is not a sqlite database, not even close!!")
        with pytest.raises(FileNotFoundError, match="rebuild-shared"):
            specs_index.lookup("auth-type enum", db_path=bad)

    def test_schemaless_db_raises_filenotfound(self, tmp_path):
        # A present-but-empty/partial DB (e.g. a stray file) must still make
        # lookup fail gracefully rather than raising a raw sqlite error.
        import sqlite3
        empty = tmp_path / "specs.sqlite"
        sqlite3.connect(empty).close()  # creates a 0-byte file
        with pytest.raises(FileNotFoundError, match="rebuild-shared"):
            specs_index.lookup("firmware compliance", db_path=empty)


# ---------------------------------------------------------------------------
# Atomic build (crash-safe rebuild)
# ---------------------------------------------------------------------------


def _write_specs(specs_dir):
    specs_dir.mkdir(parents=True, exist_ok=True)
    for fname, spec in FIXTURE_SPECS.items():
        (specs_dir / fname).write_text(json.dumps(spec))


def _write_knowledge_sources(sources_dir):
    for source_family in (
        "security_advisories",
        "juniper_security_advisories",
        "lifecycle_notices",
        "juniper_lifecycle",
    ):
        source_dir = sources_dir / source_family
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "record.md").write_text(f"# {source_family}\n")


class TestAtomicBuild:
    def test_build_leaves_no_tmp_and_produces_usable_db(self, tmp_path):
        specs_dir = tmp_path / "specs"
        _write_specs(specs_dir)
        db_path = tmp_path / "specs.sqlite"

        specs_index.build(specs_dir=specs_dir, db_path=db_path)

        assert db_path.exists()
        assert not db_path.with_name(db_path.name + ".tmp").exists()
        # Sanity: the freshly-built index answers a lookup.
        assert specs_index.lookup("auth-type enum", db_path=db_path)

    def test_build_preserves_shared_non_openapi_tables(self, tmp_path):
        import sqlite3

        specs_dir = tmp_path / "specs"
        _write_specs(specs_dir)
        db_path = tmp_path / "specs.sqlite"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE advisories (advisory_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO advisories VALUES ('HPESBNW00001')")
        conn.commit()
        conn.close()

        specs_index.build(specs_dir=specs_dir, db_path=db_path)

        conn = sqlite3.connect(db_path)
        try:
            assert conn.execute("SELECT advisory_id FROM advisories").fetchone() == (
                "HPESBNW00001",
            )
        finally:
            conn.close()
        assert specs_index.lookup("PATCH /device-firmware", db_path=db_path)

    def test_empty_source_build_preserves_live_index(self, tmp_path):
        import sqlite3

        db_path = tmp_path / "specs.sqlite"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE advisories (advisory_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO advisories VALUES ('HPESBNW00001')")
        conn.commit()
        conn.close()
        good_bytes = db_path.read_bytes()

        with pytest.raises(RuntimeError, match="no OpenAPI records"):
            specs_index.build(
                specs_dir=tmp_path / "missing-openapi-sources",
                db_path=db_path,
            )

        assert db_path.read_bytes() == good_bytes
        assert not db_path.with_name(db_path.name + ".tmp").exists()

    def test_preserving_build_rejects_corrupt_shared_index(self, tmp_path):
        specs_dir = tmp_path / "specs"
        _write_specs(specs_dir)
        db_path = tmp_path / "specs.sqlite"
        corrupt = b"not a sqlite database"
        db_path.write_bytes(corrupt)

        with pytest.raises(RuntimeError, match="rebuild-shared"):
            specs_index.build(specs_dir=specs_dir, db_path=db_path)

        assert db_path.read_bytes() == corrupt
        assert not db_path.with_name(db_path.name + ".tmp").exists()

    def test_preserving_build_rejects_malformed_sqlite_schema(self, tmp_path):
        import sqlite3

        specs_dir = tmp_path / "specs"
        _write_specs(specs_dir)
        db_path = tmp_path / "specs.sqlite"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE advisories (id INTEGER)")
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute(
            "UPDATE sqlite_master SET sql='CREATE TABLE advisories(' "
            "WHERE name='advisories'"
        )
        conn.commit()
        conn.close()
        corrupt = db_path.read_bytes()

        with pytest.raises(RuntimeError, match="rebuild-shared"):
            specs_index.build(specs_dir=specs_dir, db_path=db_path)

        assert db_path.read_bytes() == corrupt
        assert not db_path.with_name(db_path.name + ".tmp").exists()

    def test_fresh_build_recovers_from_corrupt_shared_index(self, tmp_path):
        specs_dir = tmp_path / "specs"
        _write_specs(specs_dir)
        db_path = tmp_path / "specs.sqlite"
        db_path.write_bytes(b"not a sqlite database")

        specs_index.build(
            specs_dir=specs_dir,
            db_path=db_path,
            preserve_shared=False,
        )

        assert specs_index.lookup("PATCH /device-firmware", db_path=db_path)

    def test_rebuild_shared_recreates_both_index_families(
        self, tmp_path, monkeypatch
    ):
        from hpe_networking_mcp.pipeline.clients import advisory_index

        db_path = tmp_path / "specs.sqlite"
        sources_dir = tmp_path / "sources"
        _write_knowledge_sources(sources_dir)
        calls = []

        def fake_specs_build(*, db_path, preserve_shared):
            calls.append(("openapi", db_path, preserve_shared))
            db_path.write_bytes(b"staged")
            return {"endpoints": 3}

        def fake_knowledge_build(*, sources_dir, db_path):
            calls.append(("knowledge", sources_dir, db_path))
            return {"advisories": 2, "lifecycle_events": 4}

        monkeypatch.setattr(specs_index, "build", fake_specs_build)
        monkeypatch.setattr(advisory_index, "build", fake_knowledge_build)

        result = specs_index.rebuild_shared(
            db_path=db_path,
            sources_dir=sources_dir,
        )

        assert result == {
            "openapi": {"endpoints": 3},
            "knowledge": {"advisories": 2, "lifecycle_events": 4},
        }
        staging_path = db_path.with_name(db_path.name + ".shared.tmp")
        assert calls == [
            ("openapi", staging_path, False),
            ("knowledge", sources_dir, staging_path),
        ]
        assert db_path.read_bytes() == b"staged"
        assert not staging_path.exists()

    def test_rebuild_shared_failure_preserves_live_index(self, tmp_path, monkeypatch):
        from hpe_networking_mcp.pipeline.clients import advisory_index

        db_path = tmp_path / "specs.sqlite"
        db_path.write_bytes(b"previous-good-index")
        sources_dir = tmp_path / "sources"
        _write_knowledge_sources(sources_dir)

        def fake_specs_build(*, db_path, preserve_shared):
            assert preserve_shared is False
            db_path.write_bytes(b"partial-rebuild")
            return {"endpoints": 3}

        def fail_knowledge_build(*, sources_dir, db_path):
            raise RuntimeError(f"failed knowledge rebuild for {db_path}")

        monkeypatch.setattr(specs_index, "build", fake_specs_build)
        monkeypatch.setattr(advisory_index, "build", fail_knowledge_build)

        with pytest.raises(RuntimeError, match="failed knowledge rebuild"):
            specs_index.rebuild_shared(
                db_path=db_path,
                sources_dir=sources_dir,
            )

        assert db_path.read_bytes() == b"previous-good-index"
        assert not db_path.with_name(db_path.name + ".shared.tmp").exists()

    def test_rebuild_shared_rejects_empty_knowledge_index(
        self, tmp_path, monkeypatch
    ):
        from hpe_networking_mcp.pipeline.clients import advisory_index

        db_path = tmp_path / "specs.sqlite"
        db_path.write_bytes(b"previous-good-index")
        sources_dir = tmp_path / "sources"
        _write_knowledge_sources(sources_dir)

        def fake_specs_build(*, db_path, preserve_shared):
            assert preserve_shared is False
            db_path.write_bytes(b"partial-rebuild")
            return {"specs": 3, "endpoints": 3}

        def empty_knowledge_build(*, sources_dir, db_path):
            return {"advisories": 0, "lifecycle_events": 0}

        monkeypatch.setattr(specs_index, "build", fake_specs_build)
        monkeypatch.setattr(advisory_index, "build", empty_knowledge_build)

        with pytest.raises(RuntimeError, match="no advisory or lifecycle records"):
            specs_index.rebuild_shared(
                db_path=db_path,
                sources_dir=sources_dir,
            )

        assert db_path.read_bytes() == b"previous-good-index"
        assert not db_path.with_name(db_path.name + ".shared.tmp").exists()

    def test_rebuild_shared_requires_every_source_family(self, tmp_path):
        db_path = tmp_path / "specs.sqlite"
        db_path.write_bytes(b"previous-good-index")
        sources_dir = tmp_path / "sources"
        _write_knowledge_sources(sources_dir)
        missing = sources_dir / "juniper_lifecycle" / "record.md"
        missing.unlink()

        with pytest.raises(RuntimeError, match="juniper_lifecycle"):
            specs_index.rebuild_shared(
                db_path=db_path,
                sources_dir=sources_dir,
            )

        assert db_path.read_bytes() == b"previous-good-index"
        assert not db_path.with_name(db_path.name + ".shared.tmp").exists()

    def test_interrupted_build_preserves_prior_good_index(self, tmp_path, monkeypatch):
        specs_dir = tmp_path / "specs"
        _write_specs(specs_dir)
        db_path = tmp_path / "specs.sqlite"

        # First build establishes a good index.
        specs_index.build(specs_dir=specs_dir, db_path=db_path)
        good_bytes = db_path.read_bytes()
        assert specs_index.lookup("auth-type enum", db_path=db_path)

        # Second build crashes exactly at the atomic swap.
        def _boom(src, dst):
            raise RuntimeError("simulated crash during swap")

        monkeypatch.setattr(specs_index.os, "replace", _boom)
        with pytest.raises(RuntimeError, match="simulated crash"):
            specs_index.build(specs_dir=specs_dir, db_path=db_path)

        # The live index is byte-for-byte the previous good one, still usable.
        assert db_path.read_bytes() == good_bytes
        assert specs_index.lookup("auth-type enum", db_path=db_path)
