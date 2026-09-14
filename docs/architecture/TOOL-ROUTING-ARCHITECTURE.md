---
title: "Tool Routing Architecture"
parent: "Architecture"
nav_order: 5
---

# Tool Routing Architecture

The `hpe-networking-mcp` package provides unified, low-token tool routing across multi-platform HPE Networking toolsets (Aruba Central, GreenLake Platform, Mist, ClearPass, Apstra, EdgeConnect, UXI, Axis Atmos, and AOS8 migration tooling).

## High-Level Topology

```
                  ┌───────────────────────────────┐
                  │    Client (Casper / CLI)       │
                  └───────────────┬───────────────┘
                                  │ tool request
                                  ▼
                  ┌───────────────────────────────┐
                  │          MCP Router           │
                  └───────┬───────────────┬───────┘
                          │               │
        semantic search   │               │ fast path (#82/#83)
                          ▼               ▼
     ┌────────────────────────┐  ┌────────────────────────┐
     │       find_tool        │  │   Fast-Path Wrappers   │
     └────────────┬───────────┘  └────────────┬───────────┘
                  │                           │
                  └─────────────┬─────────────┘
                                │ dispatch
                                ▼
                  ┌───────────────────────────────┐
                  │      9 Platform Backends      │
                  └───────────────┬───────────────┘
                                  │ REST / WSS
                                  ▼
                  ┌───────────────────────────────┐
                  │          Vendor APIs          │
                  └───────────────────────────────┘
```

## Core Components

1. **MCP Router (`tool_router.py`)**
   - Single low-token entry point for discovery and execution.
   - Proxies tool execution requests to enabled platform backends.

2. **Semantic Discovery (`find_tool`)**
   - Combines semantic vector search (via LanceDB / SQLite tool index) with keyword matches.
   - Serves as the primary discovery mechanism for model intent routing.

3. **Fast-Path Wrappers**
   - High-frequency read-only helper wrappers (shipped in #82/#83).
   - Short-circuit complex tool searches for routine operations.

4. **Platform Backends & Data Stores**
   - **Backends:** Central Monitoring, Central Config, Central Ops, NAC, Central Streaming, GLP, RAG.
   - **RAG Corpus & Specs:** Hybrid document vector search (LanceDB) and exact OpenAPI spec search (SQLite).
   - **Hardware Catalog:** SQLite database for HPE hardware specifications and migration taxonomy.

## Key Architectural Principles

- **Low-Token Default:** Router mode (`HPE_MCP_ROUTER_MODE=minimal`) keeps default tool schemas concise.
- **Read-Only Safety:** Direct read dispatch uses `invoke_read_tool`; non-read or destructive operations require `invoke_tool` with explicit user intent.
- **RAG-First & API-First:** Model queries query exact OpenAPI definitions (`lookup_api`) or documented guidance (`ask_docs`/`search_docs`) before attempting write operations.
