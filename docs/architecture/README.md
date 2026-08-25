---
title: "Architecture"
nav_order: 9
has_children: true
permalink: /architecture/
---

# Architecture

Design notes for core system behavior, data stores, and retrieval strategy.

| Doc | Purpose |
|---|---|
| [how-it-works.md](how-it-works.md) | Canonical MCP + RAG mental model: three router tools, index vs live APIs, dry-run/gates, and planners |
| [system-overview.md](system-overview.md) | End-to-end MCP architecture, runtime flow, router dispatch, and file map |
| [RAG-ARCHITECTURE.md](RAG-ARCHITECTURE.md) | Embedded LanceDB + SQLite RAG/API lookup design, migration rationale, and eval results |
| [0.10.0 release notes](../release-notes-0.10.0.md) | Offline-first API lookup, deny-by-default writes, MCP host instead of standalone client, and gates that check what they claim |
| [0.8.0 release notes](../release-notes-0.8.0.md) | Clean repository/package rename, MCP 2 transport repair, PII and interop additions, strict catalog/RAG facts, and classified drift gates |
| [0.7.0 release notes](../release-notes-0.7.0.md) | Artifact/live-test gates, source lifecycle provenance, RAG structured intelligence, Central/GLP/AOS8/optional-product depth, observability, and router automation |
| [0.6.0 release notes](../release-notes-0.6.0.md) | Security/lifecycle RAG, expanded platform workflows, provenance, auditing, and reporting |
| [0.5.0 release notes](../release-notes-0.5.0.md) | Verified AOS8 migration expansion, verification taxonomy, and read-only live/dry-run evaluation |
| [0.4.0 release notes (historical)](../release-notes-0.4.0.md) | Migration execution, Mist diagnostics, EdgeConnect compatibility, GLP, and Axis changes |
| [0.3.0 release notes (historical)](../release-notes-0.3.0.md) | Platform coverage, migration, safety, tool-catalog, and API-source changes |
| [Capability gap matrix](../capability-gap-matrix.md) | Reproducible executable-tool, generated-operation, and pinned-benchmark comparison |
