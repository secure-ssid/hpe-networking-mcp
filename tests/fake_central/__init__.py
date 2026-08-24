"""Hermetic fake Central API for the MCP benchmarking harness.

Serves Aruba-Central-shaped fixture data over real HTTP on localhost with
token auth, pagination, rate limiting, and Central-style error envelopes, and
records a faithful request journal the runner consumes.
"""
