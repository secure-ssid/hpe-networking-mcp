"""Pure, network-free declarative compliance-policy evaluation.

This module backs the router-native ``evaluate_compliance_policy`` tool in
``hpe_networking_mcp.mcp_servers.tool_router``, but never imports the MCP server
SDK, ``hpe_networking_mcp.mcp_servers.*``, or
any backend server module itself -- it only ever evaluates already-retrieved,
caller-supplied ``observations`` (plain dicts, e.g. one ``invoke_read_tool``
result per device/entity) against a declarative ``policy`` (a bounded list of
field/operator/expected rules) and returns a bounded, aggregate report.

Architecture is inspired by NAPALM's ``napalm.base.validate.compliance_report``
(a fixed dispatch table of comparison operators evaluated over structured
device state) and by Nornir-style aggregate run counts, but is implemented
independently here with this repository's own conventions:

- **No ``eval``/``exec``, no arbitrary expressions, no dynamic imports.**
  Field paths are a restricted dotted/indexed grammar (``interfaces[0].name``
  or ``interfaces.0.name``); operators are a fixed, closed dispatch table
  (see :data:`OPERATORS`) -- never a caller-supplied callable or code string.
- **Fail closed on an invalid policy.** :func:`validate_policy` (and
  :func:`evaluate_policy`, which calls it first) raises
  :class:`ComplianceError` for a structurally invalid policy/operator/
  expected-value shape *before* evaluating a single observation -- an
  invalid policy never silently produces a partial/misleading report.
- **Bounded by construction.** Policy rule count, observation count,
  per-rule result detail, string/regex/version lengths, and "in"/"contains"
  collection sizes are all hard-capped (see the ``MAX_*`` constants below);
  aggregate counts always reflect the *true* total even when the per-rule
  result detail list itself is capped (mirroring the
  ``excluded``/``excluded_count`` pattern in
  ``src/hpe_networking_mcp/pipeline/router_automation.py``).
- **Never success-shaped.** Every rule result is exactly one of "pass",
  "fail", "error", or "skipped" (see :data:`RULE_STATUSES`) -- a missing
  field, a type mismatch, or a malformed regex/version value for one rule
  on one observation becomes an explicit "error" result (or "skipped" when
  the rule is marked ``optional`` and the field is absent), never a silent
  pass.
- **Read-only and side-effect-free.** This module never fetches data itself
  (no network/file I/O) and never calls another MCP tool -- ``observations``
  must already be in hand before calling :func:`evaluate_policy`.
- **Fail-closed safe regex subset, never a thread/timeout-based mitigation.**
  ``regex_fullmatch`` patterns are statically parsed (via the stdlib's own
  regex parse tree, never a new dependency) and rejected outright if they
  contain more than one quantifier opcode (``*``, ``+``, ``?``, ``{m,n}``,
  or a lazy/possessive form of one) *anywhere* in the whole pattern --
  including a nested quantified group (e.g. ``(a+)+``, ``(a*)+``,
  ``([ab]+)*`` -- the catastrophic-backtracking shape) but also plain
  sibling/sequential quantifiers that never nest at all (e.g.
  ``a*a*a*a*a*a*a*a*a*b``, ``.*.*=.*``, ``[a-z]*[a-z]*!``,
  ``^\\d+\\.\\d+\\.\\d+$``) -- a deliberately conservative, auditable "at most
  one quantifier in the entire pattern" ceiling rather than an attempt to
  distinguish safe from unsafe multi-quantifier combinations. This module
  also rejects outright *any* alternation reachable under a quantifier at
  all (e.g. ``(a|a)*b``, ``(aa|a)*b``, ``(?:abc|def)+`` -- a simple,
  conservative rule rather than an attempt to distinguish ambiguous from
  unambiguous alternation), a backreference, a lookaround assertion, or any
  other construct this module does not explicitly recognize as safe -- see
  :func:`_validate_safe_regex_subset`. Finite repeat counts may not exceed
  the 500-character subject bound, and a quantified body must consume at
  least one character, preventing huge repeats of empty/zero-width groups
  from exhausting regex-engine memory. This is intentionally strict enough
  to reject some otherwise-safe complex regexes (e.g.
  ``^[\\w.-]+@[\\w.-]+$``, which has two independent quantifiers) in
  exchange for a small, fully auditable rule with no combinatorial edge
  cases.
- **"actual" is always bounded *and* redacted before it leaves this
  module.** :func:`_bound_actual` is the single choke point every extracted
  field value passes through before becoming a rule result's "actual": a
  field path containing any credential/secret-shaped segment (e.g.
  ``credentials.password``, ``device.api_key``) or tenant/workspace/
  account-shaped (e.g. ``account.tenant_id``) is redacted outright, any
  dict/list "actual" value is walked recursively so a *nested* secret/
  tenant key is redacted too (never only the top-level field), and the
  result is depth/collection/string/byte-bounded to always fit
  ``hpe_networking_mcp.pipeline.artifact_contracts``'s own ``compliance_report`` "actual"
  serialized-size ceiling -- falling back to a deterministic
  ``"**TRUNCATED-ACTUAL**"`` marker rather than ever exceeding it. This
  mirrors (without importing, to keep this module free of any
  ``hpe_networking_mcp.mcp_servers.*``/other-``pipeline``-module dependency)
  ``hpe_networking_mcp.mcp_servers.shared``'s sensitive-key and
  ``hpe_networking_mcp.pipeline.artifact_contracts``'s tenant-key semantics.

Typical usage::

    from hpe_networking_mcp.pipeline import compliance

    report = compliance.evaluate_policy(
        observations=[{"hostname": "sw1", "firmware": {"version": "8.10.0"}}],
        policy=[
            {
                "field": "firmware.version",
                "operator": "version_gte",
                "expected": "8.9.0",
            }
        ],
    )
    report["compliant"]  # True
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Safe-regex AST analysis -- stdlib-only, no new dependency. Python's public
# `re` module has no way to introspect a pattern's parse tree, so this
# reuses the private `re._parser` module (named `sre_parse` before Python
# 3.11; both expose the same opcode-tagged parse tree). If neither is
# importable in some future Python runtime, `_regex_ast_parse` is left
# `None` and `_validate_regex_pattern` fails closed -- rejecting every
# `regex_fullmatch` pattern -- rather than silently skipping the nested-
# quantifier/backreference/lookaround safe-subset check below.
# ---------------------------------------------------------------------------
try:
    from re import _parser as _regex_ast_module  # Python 3.11+
except ImportError:  # pragma: no cover - Python 3.10 fallback
    try:
        import sre_parse as _regex_ast_module  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - no safe-regex AST support available
        _regex_ast_module = None

_regex_ast_parse = getattr(_regex_ast_module, "parse", None)
_regex_ast_maxrepeat = getattr(_regex_ast_module, "MAXREPEAT", None)

# ---------------------------------------------------------------------------
# Bounds -- every growable/parseable input has a hard ceiling. Invalid policy
# shape fails closed (raises ComplianceError); a per-rule evaluation problem
# on one observation becomes an "error" result instead of raising, so one bad
# observation never aborts the whole report.
# ---------------------------------------------------------------------------
MAX_POLICY_RULES = 50
MAX_OBSERVATIONS = 100
MAX_FIELD_PATH_CHARS = 200
MAX_REGEX_PATTERN_CHARS = 200
# Conservative, auditable ReDoS policy: at most this many quantifier opcodes
# (MAX_REPEAT/MIN_REPEAT -- covering greedy, lazy, and bounded/unbounded
# quantifier syntax alike, e.g. "*", "+", "?", "{m,n}", and their lazy
# "*?"/"+?"/"{m,n}?" forms) anywhere in the entire parsed pattern, at any
# nesting/branch depth -- never per-group, never per-branch. This
# deliberately rejects some otherwise-safe complex regexes (e.g.
# "^[\w.-]+@[\w.-]+$", which has two independent, non-nested quantifiers)
# in exchange for a single, fully auditable rule with no combinatorial edge
# cases: two or more quantifiers in one pattern -- nested, sibling, or
# sequential -- is exactly the shape that produces catastrophic/
# combinatorial backtracking (e.g. "a*a*a*a*a*a*a*a*a*b", ".*.*=.*",
# "[a-z]*[a-z]*!", "^\d+\.\d+\.\d+$"), so this module never tries to
# distinguish a "safe" multi-quantifier pattern from an "unsafe" one.
MAX_REGEX_TOTAL_QUANTIFIERS = 1
MAX_STRING_COMPARE_CHARS = 500
# A finite repeat larger than the maximum permitted subject can never match
# and may consume substantial regex-engine memory, especially for grouped
# or zero-width bodies.
MAX_REGEX_FINITE_REPEAT = MAX_STRING_COMPARE_CHARS
MAX_VERSION_CHARS = 64
MAX_VERSION_SEGMENTS = 10
MAX_EXPECTED_LIST_ITEMS = 50
MAX_RESULT_ENTRIES = 500
MAX_OBSERVATION_ID_CHARS = 120
MAX_MESSAGE_CHARS = 500
MAX_RULE_ID_CHARS = 100
MAX_POLICY_ID_CHARS = 200

SEVERITIES: tuple[str, ...] = ("critical", "error", "warning", "info")
RULE_STATUSES: tuple[str, ...] = ("pass", "fail", "error", "skipped")

# Fixed, closed operator dispatch table -- never a caller-supplied callable.
OPERATORS: tuple[str, ...] = (
    "eq",
    "ne",
    "lt",
    "le",
    "gt",
    "ge",
    "contains",
    "in",
    "regex_fullmatch",
    "version_gte",
    "version_range",
    "exists",
    "not_exists",
)

_OPERATORS_REQUIRING_EXPECTED = tuple(op for op in OPERATORS if op not in ("exists", "not_exists"))


class ComplianceError(ValueError):
    """Raised for a structurally invalid policy/rule/observation input.

    Always raised *before* any rule is evaluated against any observation --
    this module never produces a partial report from a malformed policy.
    """


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_MISSING = object()


# ---------------------------------------------------------------------------
# Safe dotted/indexed field extraction -- no eval, no attribute access, only
# Mapping key lookup and Sequence integer indexing over a restricted
# character-class path grammar.
# ---------------------------------------------------------------------------

_FIELD_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_BRACKET_INDEX_RE = re.compile(r"\[(\d+)\]")


def _split_field_path(field_path: Any) -> list[str]:
    if not isinstance(field_path, str) or not field_path.strip():
        raise ComplianceError("field path must be a non-empty string")
    if len(field_path) > MAX_FIELD_PATH_CHARS:
        raise ComplianceError(
            f"field path exceeds the {MAX_FIELD_PATH_CHARS}-character bound"
        )
    # Normalize bracket-index syntax ("interfaces[0].name") to dotted
    # ("interfaces.0.name") so both notations resolve identically.
    normalized = _BRACKET_INDEX_RE.sub(r".\1", field_path)
    parts = [part for part in normalized.split(".") if part != ""]
    if not parts:
        raise ComplianceError(f"field path {field_path!r} has no segments")
    for part in parts:
        if not _FIELD_SEGMENT_RE.match(part):
            raise ComplianceError(
                f"field path segment {part!r} contains disallowed characters"
            )
    return parts


def extract_field(observation: Mapping[str, Any], field_path: str) -> Any:
    """Safely resolve a dotted/indexed field path against ``observation``.

    Returns the module-private "not found" sentinel (never ``None``, which
    is a legitimate value) when any segment is absent, out of range, or the
    current value is neither a ``Mapping`` nor a ``Sequence``. Never uses
    ``eval``/``getattr``/dynamic imports -- only ``Mapping`` key lookup and
    ``Sequence`` integer indexing.
    """
    parts = _split_field_path(field_path)
    current: Any = observation
    for part in parts:
        if isinstance(current, Mapping):
            if part in current:
                current = current[part]
            else:
                return _MISSING
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes)):
            if not part.isdigit():
                return _MISSING
            index = int(part)
            if index < 0 or index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


# ---------------------------------------------------------------------------
# Bounded value helpers
# ---------------------------------------------------------------------------


def _truncate(value: Any, max_chars: int) -> Any:
    """Truncate a string to *at most* ``max_chars`` characters, marker
    included -- the returned string's length never exceeds ``max_chars``.

    This is the single choke point every bounded string (rule/result
    "message", "actual" leaf strings, observation/rule/policy ids) passes
    through, so a value bounded here to this module's own
    ``MAX_MESSAGE_CHARS`` always also satisfies a downstream artifact
    contract's matching hard ceiling (e.g.
    ``hpe_networking_mcp.pipeline.artifact_contracts.MAX_COMPLIANCE_MESSAGE_CHARS``) exactly
    -- never a few characters over it from an appended "...[truncated N
    chars]" suffix.
    """
    if not isinstance(value, str) or len(value) <= max_chars:
        return value
    if max_chars <= 0:
        return ""
    marker = "...[truncated]"
    if len(marker) >= max_chars:
        return value[:max_chars]
    return value[: max_chars - len(marker)] + marker


# ---------------------------------------------------------------------------
# Sensitive/tenant key semantics -- mirrors (does not import, to keep this
# module's "never imports hpe_networking_mcp.mcp_servers.*, and never imports any other
# pipeline module" invariant intact and dependency-cycle-free)
# `hpe_networking_mcp.mcp_servers.shared._is_sensitive_key`'s credential-shaped key set and
# `hpe_networking_mcp.pipeline.artifact_contracts._is_tenant_key`'s tenant/workspace/account/
# scope-shaped key set. Kept as exact copies (not a superset/subset) so all
# three call sites agree on what counts as sensitive/tenant. This module's
# own result capture (see `_bound_actual` below) is the only place that
# extracts a caller-controlled leaf value out of a field path and must
# never rely solely on the generic "actual" wrapper key for redaction --
# the artifact-level, key-based redaction in `write_artifact` only ever
# inspects container keys, never the field path that produced a scalar
# leaf.
# ---------------------------------------------------------------------------

_SENSITIVE_KEY_EXACT = {
    "auth",
    "authorization",
    "key",
    "community_string",
    "snmp_read",
    "snmp_write",
}
_SENSITIVE_KEY_SUFFIXES = (
    "api_key",
    "apikey",
    "credential",
    "credentials",
    "_key",
    "passphrase",
    "password",
    "psk",
    "secret",
    "token",
)
_TENANT_KEY_EXACT = {
    "tenant",
    "tenant_id",
    "workspace",
    "workspace_id",
    "glp_workspace_id",
    "account",
    "account_id",
    "customer",
    "customer_id",
    "org_id",
    "organization_id",
    "subscription_id",
    "scope_id",
    "scope_name",
    "device_scope_id",
    "cluster_scope_id",
    "cluster_name",
}
_TENANT_KEY_SUFFIXES = (
    "tenant_id",
    "workspace_id",
    "account_id",
    "customer_id",
    "org_id",
    "organization_id",
    "scope_id",
    "scope_name",
)
_REDACTED_SENSITIVE = "**REDACTED**"
_REDACTED_TENANT = "**REDACTED-TENANT**"
# Deterministic, explicit marker used whenever a bounded "actual" value
# still cannot be shown in full -- either because recursive depth/
# collection-size/string-size bounding alone was not enough to fit inside
# the compliance_report artifact contract's own serialized-size ceiling
# (see src/hpe_networking_mcp/pipeline/artifact_contracts.py MAX_COMPLIANCE_VALUE_CHARS), or
# because the value is not JSON-serializable at all. Never a silent
# truncation to `None`/empty -- the caller can always tell a value was
# capped.
_TRUNCATED_ACTUAL_MARKER = "**TRUNCATED-ACTUAL**"

MAX_ACTUAL_DEPTH = 4
MAX_ACTUAL_COLLECTION_ITEMS = 6
MAX_ACTUAL_STRING_CHARS = 100
# Comfortably under hpe_networking_mcp.pipeline.artifact_contracts's own
# MAX_COMPLIANCE_VALUE_CHARS * 4 (2000 bytes) ceiling for one result's
# serialized "actual" -- computed with the same `json.dumps(..., default=
# str)` call the artifact contract itself uses, so a bounded value that
# passes this check is always guaranteed to also pass the artifact's own
# check (see ComplianceRuleResult.__post_init__), and an
# evaluator-produced report always builds successfully for a valid
# observation.
MAX_ACTUAL_JSON_BYTES = 1600


def _normalize_key(key: Any) -> str:
    key_text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key).strip())
    return re.sub(r"[^a-z0-9]+", "_", key_text.lower()).strip("_")


def _is_sensitive_key(key: Any) -> bool:
    normalized = _normalize_key(key)
    return normalized in _SENSITIVE_KEY_EXACT or any(
        normalized.endswith(suffix) for suffix in _SENSITIVE_KEY_SUFFIXES
    )


def _is_tenant_key(key: Any) -> bool:
    normalized = _normalize_key(key)
    return normalized in _TENANT_KEY_EXACT or any(
        normalized.endswith(suffix) for suffix in _TENANT_KEY_SUFFIXES
    )


def _redact_and_bound_container(value: Any, *, depth: int) -> Any:
    """Recursively redact sensitive/tenant-shaped keys and bound depth,
    collection size, and string size. Never raises -- an unrecognized/
    non-JSON-shaped value is simply returned as-is for the caller's final
    JSON-serializability/size check to catch."""
    if isinstance(value, str):
        return _truncate(value, MAX_ACTUAL_STRING_CHARS)
    if depth >= MAX_ACTUAL_DEPTH:
        if isinstance(value, (Mapping, list, tuple, set, frozenset)):
            return _TRUNCATED_ACTUAL_MARKER
        return value
    if isinstance(value, Mapping):
        items = list(value.items())
        bounded: dict[str, Any] = {}
        for key, item in items[:MAX_ACTUAL_COLLECTION_ITEMS]:
            key_text = _truncate(str(key), MAX_ACTUAL_STRING_CHARS)
            if item is None:
                bounded[key_text] = None
            elif _is_sensitive_key(key):
                bounded[key_text] = _REDACTED_SENSITIVE
            elif _is_tenant_key(key):
                bounded[key_text] = _REDACTED_TENANT
            else:
                bounded[key_text] = _redact_and_bound_container(item, depth=depth + 1)
        if len(items) > MAX_ACTUAL_COLLECTION_ITEMS:
            bounded["__truncated_keys__"] = len(items) - MAX_ACTUAL_COLLECTION_ITEMS
        return bounded
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        bounded_items = [
            _redact_and_bound_container(item, depth=depth + 1)
            for item in items[:MAX_ACTUAL_COLLECTION_ITEMS]
        ]
        if len(items) > MAX_ACTUAL_COLLECTION_ITEMS:
            bounded_items.append(
                f"... [truncated {len(items) - MAX_ACTUAL_COLLECTION_ITEMS} more items]"
            )
        return bounded_items
    return value


def _bound_actual(value: Any, *, field_path: str | None = None) -> Any:
    """Bound (and redact) a scalar/collection actual value before it goes
    into a result -- the single choke point every extracted field value
    passes through on its way into a rule result / artifact "actual".

    Never trusts the generic "actual" wrapper key alone for redaction
    (artifact-level, key-based redaction cannot see the field path that
    produced a scalar leaf): a field path containing *any* sensitive/
    tenant-shaped segment -- not only its leaf -- (e.g. "credentials.password",
    "account.tenant_id", "auth.value", "token.raw", "credentials[0]",
    "account.tenant_id.v") is redacted outright regardless of its extracted
    value's shape. Checking every segment (never only the leaf) is
    deliberate: a sensitive/tenant ancestor segment must not be defeated by
    a non-sensitive-looking leaf segment appended after it (e.g. the
    trailing ".value"/".raw"/".v" above), and a purely numeric list-index
    segment (e.g. the "0" in "credentials[0]", normalized to
    "credentials.0") is simply skipped over rather than ever short-
    circuiting the scan of the remaining segments. Any dict/list actual
    value is also walked recursively so a nested sensitive/tenant key
    (e.g. extracting the whole "credentials" object) is redacted too --
    never only the top-level container.

    Always returns a value that fits within
    hpe_networking_mcp.pipeline.artifact_contracts's own compliance_report "actual"
    serialized-size ceiling (falling back to the deterministic
    `_TRUNCATED_ACTUAL_MARKER` otherwise), so an evaluator-produced report
    always builds successfully for a valid observation.
    """
    if field_path:
        try:
            segments = _split_field_path(field_path)
        except ComplianceError:
            segments = []
        # Sensitive takes priority over tenant when a path mixes both
        # shapes (e.g. "credentials.tenant_id"): either match alone is
        # already sufficient reason to redact outright.
        if any(_is_sensitive_key(segment) for segment in segments):
            return _REDACTED_SENSITIVE
        if any(_is_tenant_key(segment) for segment in segments):
            return _REDACTED_TENANT

    bounded = _redact_and_bound_container(value, depth=0)
    try:
        serialized_size = len(json.dumps(bounded, default=str))
    except TypeError:
        return _TRUNCATED_ACTUAL_MARKER
    if serialized_size > MAX_ACTUAL_JSON_BYTES:
        return _TRUNCATED_ACTUAL_MARKER
    return bounded


_OBSERVATION_ID_KEYS = ("id", "serial", "serial_number", "name", "hostname", "mac", "device_id")


def _observation_identifier(observation: Mapping[str, Any]) -> str | None:
    for key in _OBSERVATION_ID_KEYS:
        value = observation.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return _truncate(str(value), MAX_OBSERVATION_ID_CHARS)
    return None


# ---------------------------------------------------------------------------
# Version comparison -- bounded, dotted-numeric versions only (e.g.
# "8.10.0"). No pre-release/build-metadata parsing (that would need a
# third-party dependency to do safely); a non-conforming value is a
# ComplianceError, not a best-effort guess.
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){0,9}$")


def _parse_version(value: Any, *, label: str) -> tuple[int, ...]:
    # Never echo the raw `value` (caller-supplied "actual"/"expected"
    # content, which may be a redaction-bypassing secret/tenant identifier
    # extracted from an observation, e.g. a credential mistakenly stored in
    # a "firmware.version"-shaped field) into any ComplianceError message
    # below -- every branch here describes *what was wrong* (type, length,
    # shape) using only `label` (a static, caller-independent string), never
    # the value itself. This is the single choke point every version-shaped
    # comparison target passes through, so this guarantee holds for both
    # "actual value" and "expected version"/"expected.min"/"expected.max".
    if not isinstance(value, str):
        raise ComplianceError(f"{label} must be a dotted-numeric version string")
    if len(value) > MAX_VERSION_CHARS:
        raise ComplianceError(
            f"{label} is empty or exceeds the {MAX_VERSION_CHARS}-character bound"
        )
    text = value.strip()
    if not text or len(text) > MAX_VERSION_CHARS:
        raise ComplianceError(
            f"{label} is empty or exceeds the {MAX_VERSION_CHARS}-character bound"
        )
    if not _VERSION_RE.match(text):
        raise ComplianceError(
            f"{label} is not a bounded dotted-numeric version (e.g. '8.10.0')"
        )
    segments = tuple(int(part) for part in text.split("."))
    if len(segments) > MAX_VERSION_SEGMENTS:
        raise ComplianceError(f"{label} has too many dotted segments")
    return segments


def _compare_versions(actual: Any, expected: Any) -> int:
    va = _parse_version(actual, label="actual value")
    vb = _parse_version(expected, label="expected version")
    length = max(len(va), len(vb))
    va = va + (0,) * (length - len(va))
    vb = vb + (0,) * (length - len(vb))
    if va < vb:
        return -1
    if va > vb:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Regex helpers -- bounded pattern/subject length, and (below) a fail-closed
# *safe regex subset* check that statically rejects the constructs that
# actually cause catastrophic/combinatorial backtracking -- a nested
# quantified group (e.g. "(a+)+b"), an alternation reachable under a
# quantifier, or *more than one quantifier opcode anywhere in the whole
# pattern at all* (this module's conservative "at most
# MAX_REGEX_TOTAL_QUANTIFIERS quantifiers total" ceiling -- see
# _count_regex_quantifiers -- which also catches non-nested sibling/
# sequential quantifiers such as "a*a*a*a*a*a*a*a*a*b" or ".*.*=.*") -- or
# that this module never needs to support at all (backreferences,
# lookarounds). Never uses a caller-supplied callable or code string, never
# falls back to eval/exec, and never uses a thread/subprocess/signal-based
# timeout as a substitute for actually rejecting a dangerous pattern.
# ---------------------------------------------------------------------------

# Opcode allow-list for the safe regex subset -- anything not explicitly
# listed here (most importantly GROUPREF/GROUPREF_EXISTS -- backreferences
# -- and ASSERT/ASSERT_NOT -- lookarounds) is rejected outright rather than
# silently permitted, so a future stdlib parser addition never widens this
# module's accepted regex surface by accident.
_REGEX_ALLOWED_LEAF_OPCODE_NAMES = ("LITERAL", "NOT_LITERAL", "ANY", "IN", "CATEGORY", "AT")
_REGEX_QUANTIFIER_OPCODE_NAMES = ("MAX_REPEAT", "MIN_REPEAT")
_REGEX_GROUP_OPCODE_NAMES = ("SUBPATTERN",)
_REGEX_BRANCH_OPCODE_NAMES = ("BRANCH",)


def _regex_opcode_name(opcode: Any) -> str:
    # `re._parser`/`sre_parse` opcodes are singleton, name-carrying int
    # constants (`_NamedIntConstant`) whose `str()` is already their bare
    # name (e.g. "LITERAL", "MAX_REPEAT") on every supported Python version.
    return str(opcode)


def _regex_subpattern_consumes_character(subpattern: Any) -> bool:
    """Return whether every route through ``subpattern`` consumes input.

    This is intentionally conservative. It is used only for repeated
    subpatterns, where allowing an empty/zero-width body can make a huge
    finite repeat allocate unbounded regex-engine state.
    """
    consumed = False
    for opcode, argument in subpattern:
        name = _regex_opcode_name(opcode)
        if name in ("LITERAL", "NOT_LITERAL", "ANY", "IN", "CATEGORY"):
            consumed = True
        elif name in _REGEX_GROUP_OPCODE_NAMES:
            consumed = consumed or _regex_subpattern_consumes_character(argument[-1])
        elif name in _REGEX_BRANCH_OPCODE_NAMES:
            branches = argument[1]
            if not branches or not all(
                _regex_subpattern_consumes_character(branch) for branch in branches
            ):
                return False
            consumed = True
        elif name == "AT":
            continue
        else:
            return False
    return consumed


def _scan_regex_ast(subpattern: Any, *, inside_quantifier: bool) -> None:
    """Recursively walk one parsed-regex `SubPattern`, raising
    :class:`ComplianceError` on the first backreference, lookaround, nested
    quantified group (a quantifier whose repeated subpattern itself contains
    another quantifier -- the shape that causes catastrophic backtracking,
    e.g. "(a+)+b", "(a*)+", "([ab]+)*"), or *any* alternation (a "BRANCH")
    reachable underneath a quantifier at all (e.g. "(a|a)*b", "(aa|a)*b",
    "(?:abc|def)+") -- deliberately a simple, conservative, fail-closed rule
    rather than an attempt to distinguish "ambiguous" alternation (branches
    that can match overlapping strings, e.g. "a|a"/"aa|a" -- the actual
    catastrophic-backtracking risk) from "unambiguous" alternation (e.g.
    "abc|def", whose branches share no possible overlap): reliably proving
    two branches can never overlap is its own small static-analysis problem,
    and this module's own stated architecture goal is to be a small, fully
    auditable, stdlib-only safe subset -- never a general-purpose regex
    safety analyzer. A single-character alternation (e.g. "(?:a|b)") is
    optimized by the stdlib parser itself into an "IN" character-class
    opcode rather than a "BRANCH" node, so it is unaffected by this rule
    and remains safe under a quantifier. Fails closed on any opcode this
    module does not explicitly recognize."""
    for opcode, argument in subpattern:
        name = _regex_opcode_name(opcode)
        if name in _REGEX_ALLOWED_LEAF_OPCODE_NAMES:
            continue
        if name in _REGEX_QUANTIFIER_OPCODE_NAMES:
            if inside_quantifier:
                raise ComplianceError(
                    "regex_fullmatch pattern contains a nested quantified group "
                    "(e.g. '(a+)+'), which is rejected outright as a catastrophic-"
                    "backtracking risk"
                )
            # argument is (min, max, subpattern) on every supported version.
            min_repeat, max_repeat, repeated = argument
            max_is_unbounded = (
                _regex_ast_maxrepeat is not None and max_repeat == _regex_ast_maxrepeat
            )
            if min_repeat > MAX_REGEX_FINITE_REPEAT or (
                not max_is_unbounded and max_repeat > MAX_REGEX_FINITE_REPEAT
            ):
                raise ComplianceError(
                    "regex_fullmatch finite repeat count exceeds the "
                    f"{MAX_REGEX_FINITE_REPEAT}-character subject bound"
                )
            _scan_regex_ast(repeated, inside_quantifier=True)
            if not _regex_subpattern_consumes_character(repeated):
                raise ComplianceError(
                    "regex_fullmatch quantified body must consume at least one "
                    "character; empty and zero-width repeated bodies are rejected"
                )
            continue
        if name in _REGEX_GROUP_OPCODE_NAMES:
            # argument is (group_number, add_flags, del_flags, subpattern).
            _scan_regex_ast(argument[-1], inside_quantifier=inside_quantifier)
            continue
        if name in _REGEX_BRANCH_OPCODE_NAMES:
            if inside_quantifier:
                raise ComplianceError(
                    "regex_fullmatch pattern contains an alternation (branch) "
                    "reachable under a quantified group (e.g. '(a|a)*', "
                    "'(aa|a)*', '(?:abc|def)+'), which is rejected outright as "
                    "an ambiguous-alternation catastrophic-backtracking risk"
                )
            # argument is (None, [subpattern, subpattern, ...]).
            for branch in argument[1]:
                _scan_regex_ast(branch, inside_quantifier=inside_quantifier)
            continue
        if name in ("GROUPREF", "GROUPREF_EXISTS"):
            raise ComplianceError(
                "regex_fullmatch pattern contains a backreference, which is not "
                "part of the supported safe regex subset"
            )
        if name in ("ASSERT", "ASSERT_NOT"):
            raise ComplianceError(
                "regex_fullmatch pattern contains a lookaround assertion, which is "
                "not part of the supported safe regex subset"
            )
        # Fail closed on any other/future opcode this module does not
        # explicitly recognize as safe (e.g. ATOMIC_GROUP, POSSESSIVE_REPEAT
        # additions) rather than silently permitting it.
        raise ComplianceError(
            f"regex_fullmatch pattern uses an unsupported construct ({name!r}); "
            "only literals, character classes, anchors, alternation, grouping, "
            "and single-level quantifiers are permitted"
        )


def _count_regex_quantifiers(subpattern: Any) -> int:
    """Count every quantifier opcode (`MAX_REPEAT`/`MIN_REPEAT` -- covering
    greedy, lazy, and bounded/unbounded quantifier syntax alike, e.g. `*`,
    `+`, `?`, `{m,n}`, and their lazy `*?`/`+?`/`{m,n}?` forms) anywhere in
    the parsed pattern, at any nesting/branch depth -- never per-group,
    never per-branch, a single running total for the whole pattern.

    Used by :func:`_validate_safe_regex_subset` to enforce this module's
    conservative "at most `MAX_REGEX_TOTAL_QUANTIFIERS` quantifiers in the
    entire pattern" policy -- deliberately blunt so that sibling/sequential
    quantifiers (e.g. `a*a*a*a*a*a*a*a*a*b`, `.*.*=.*`, `[a-z]*[a-z]*!`,
    `^\\d+\\.\\d+\\.\\d+$`) are rejected outright even though none of them
    individually nests one quantifier inside another and none contains an
    alternation: multiple independent quantifiers anywhere in one pattern
    is its own combinatorial-backtracking risk that the narrower nested-
    quantifier/branch-under-quantifier checks in :func:`_scan_regex_ast` do
    not catch on their own."""
    count = 0
    for opcode, argument in subpattern:
        name = _regex_opcode_name(opcode)
        if name in _REGEX_QUANTIFIER_OPCODE_NAMES:
            # argument is (min, max, subpattern) on every supported version.
            count += 1 + _count_regex_quantifiers(argument[2])
        elif name in _REGEX_GROUP_OPCODE_NAMES:
            # argument is (group_number, add_flags, del_flags, subpattern).
            count += _count_regex_quantifiers(argument[-1])
        elif name in _REGEX_BRANCH_OPCODE_NAMES:
            # argument is (None, [subpattern, subpattern, ...]).
            for branch in argument[1]:
                count += _count_regex_quantifiers(branch)
    return count


def _validate_safe_regex_subset(pattern: str) -> None:
    if _regex_ast_parse is None:
        # Fail closed: without the safe-regex AST analyzer, no
        # regex_fullmatch pattern can be accepted at all -- never silently
        # skip the nested-quantifier/backreference/lookaround/quantifier-
        # count check.
        raise ComplianceError(
            "regex_fullmatch is unavailable in this Python runtime (the safe "
            "regex AST analyzer module could not be imported)"
        )
    try:
        parsed = _regex_ast_parse(pattern)
    except re.error as exc:
        raise ComplianceError(f"regex_fullmatch pattern is invalid: {exc}") from exc
    # Specific, named rejections (nested quantified group, alternation
    # under a quantifier, backreference, lookaround, unrecognized opcode)
    # first, so the most actionable error message wins for those shapes.
    _scan_regex_ast(parsed, inside_quantifier=False)
    # Then the blanket, conservative "at most one quantifier in the whole
    # pattern" ceiling -- catches sibling/sequential multi-quantifier
    # patterns (e.g. "a*a*a*a*a*a*a*a*a*b", ".*.*=.*", "[a-z]*[a-z]*!",
    # "^\d+\.\d+\.\d+$") that `_scan_regex_ast` above does not flag on
    # its own because no single quantifier nests inside another and no
    # branch is involved.
    quantifier_total = _count_regex_quantifiers(parsed)
    if quantifier_total > MAX_REGEX_TOTAL_QUANTIFIERS:
        raise ComplianceError(
            f"regex_fullmatch pattern contains {quantifier_total} quantifier "
            f"opcodes, exceeding this module's {MAX_REGEX_TOTAL_QUANTIFIERS}-"
            "quantifier-per-pattern ceiling; at most one quantifier (*, +, "
            "?, {m,n}, or a lazy/possessive form of one) is permitted "
            "anywhere in a regex_fullmatch pattern -- simplify the pattern "
            "or split it into multiple rules"
        )


def _validate_regex_pattern(pattern: Any) -> None:
    if not isinstance(pattern, str):
        raise ComplianceError("regex_fullmatch expected value must be a string pattern")
    if not pattern:
        raise ComplianceError("regex_fullmatch expected pattern must not be empty")
    if len(pattern) > MAX_REGEX_PATTERN_CHARS:
        raise ComplianceError(
            f"regex_fullmatch pattern exceeds the {MAX_REGEX_PATTERN_CHARS}-character bound"
        )
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ComplianceError(f"regex_fullmatch pattern is invalid: {exc}") from exc
    # Fail-closed safe-subset check -- rejects nested quantified groups,
    # branches under a quantifier, backreferences, lookarounds, more than
    # one quantifier anywhere in the whole pattern, and any other
    # unrecognized construct even though `re.compile` above accepts them.
    _validate_safe_regex_subset(pattern)


def _regex_fullmatch(actual: Any, pattern: str) -> bool:
    if not isinstance(actual, str):
        raise ComplianceError("regex_fullmatch requires a string actual value")
    if len(actual) > MAX_STRING_COMPARE_CHARS:
        raise ComplianceError(
            f"actual value exceeds the {MAX_STRING_COMPARE_CHARS}-character comparison bound"
        )
    return re.fullmatch(pattern, actual) is not None


# ---------------------------------------------------------------------------
# Scalar/type guards shared by validation and evaluation
# ---------------------------------------------------------------------------

_SCALAR_TYPES = (str, int, float, bool, type(None))


def _require_scalar(value: Any, label: str) -> None:
    if not isinstance(value, _SCALAR_TYPES):
        raise ComplianceError(f"{label} must be a string, number, bool, or null")
    if isinstance(value, str) and len(value) > MAX_STRING_COMPARE_CHARS:
        raise ComplianceError(
            f"{label} exceeds the {MAX_STRING_COMPARE_CHARS}-character bound"
        )


def _require_numeric(value: Any, label: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ComplianceError(f"{label} must be a number (int/float, not bool)")
    return value


# ---------------------------------------------------------------------------
# Structural (policy-time) validation of the "expected" shape per operator --
# raised eagerly by validate_policy, never deferred to evaluation time.
# ---------------------------------------------------------------------------


def _validate_expected_shape(operator: str, expected: Any, index: int) -> None:
    where = f"policy[{index}] (operator={operator!r})"
    if operator in ("eq", "ne"):
        _require_scalar(expected, f"{where} expected")
    elif operator in ("lt", "le", "gt", "ge"):
        _require_numeric(expected, f"{where} expected")
    elif operator == "contains":
        _require_scalar(expected, f"{where} expected")
    elif operator == "in":
        if isinstance(expected, (str, bytes)) or not isinstance(expected, Sequence):
            raise ComplianceError(f"{where} expected must be a list for operator 'in'")
        if len(expected) > MAX_EXPECTED_LIST_ITEMS:
            raise ComplianceError(
                f"{where} expected list exceeds the {MAX_EXPECTED_LIST_ITEMS}-item bound"
            )
        for item_index, item in enumerate(expected):
            _require_scalar(item, f"{where} expected[{item_index}]")
    elif operator == "regex_fullmatch":
        _validate_regex_pattern(expected)
    elif operator == "version_gte":
        _parse_version(expected, label=f"{where} expected")
    elif operator == "version_range":
        if not isinstance(expected, Mapping):
            raise ComplianceError(
                f"{where} expected must be an object with 'min' and/or 'max' for version_range"
            )
        has_min = "min" in expected and expected["min"] is not None
        has_max = "max" in expected and expected["max"] is not None
        if not has_min and not has_max:
            raise ComplianceError(f"{where} expected must set 'min' and/or 'max'")
        if has_min:
            _parse_version(expected["min"], label=f"{where} expected.min")
        if has_max:
            _parse_version(expected["max"], label=f"{where} expected.max")
        if has_min and has_max:
            min_v = _parse_version(expected["min"], label=f"{where} expected.min")
            max_v = _parse_version(expected["max"], label=f"{where} expected.max")
            length = max(len(min_v), len(max_v))
            min_v = min_v + (0,) * (length - len(min_v))
            max_v = max_v + (0,) * (length - len(max_v))
            if min_v > max_v:
                raise ComplianceError(f"{where} expected.min must be <= expected.max")
    else:  # exists / not_exists never reach here (skipped by caller)
        raise ComplianceError(f"{where} unsupported operator {operator!r}")


def validate_policy(policy: Any) -> list[dict[str, Any]]:
    """Parse and validate a raw policy into a normalized, bounded rule list.

    Fails closed (:class:`ComplianceError`) on any structural problem --
    wrong container type, too many rules, a duplicate rule id, an unknown
    operator, a malformed field path, or an "expected" value shape that
    does not match its operator (including an unparsable regex pattern or
    version string). Never evaluated lazily against an observation.
    """
    if isinstance(policy, (str, bytes)) or not isinstance(policy, Sequence):
        raise ComplianceError("policy must be a list of rule objects")
    if not policy:
        raise ComplianceError("policy must contain at least one rule")
    if len(policy) > MAX_POLICY_RULES:
        raise ComplianceError(
            f"policy has {len(policy)} rules, exceeding the {MAX_POLICY_RULES}-rule bound"
        )

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_rule in enumerate(policy):
        if not isinstance(raw_rule, Mapping):
            raise ComplianceError(f"policy[{index}] must be an object")

        rule_id = str(raw_rule.get("id") or f"rule_{index}")
        if len(rule_id) > MAX_RULE_ID_CHARS:
            raise ComplianceError(
                f"policy[{index}] id exceeds the {MAX_RULE_ID_CHARS}-character bound"
            )
        if rule_id in seen_ids:
            raise ComplianceError(f"duplicate rule id: {rule_id!r}")
        seen_ids.add(rule_id)

        field_path = raw_rule.get("field")
        _split_field_path(field_path)  # raises on malformed syntax

        operator = raw_rule.get("operator")
        if not isinstance(operator, str):
            raise ComplianceError(f"policy[{index}] operator must be a string")
        if len(operator) > MAX_RULE_ID_CHARS:
            raise ComplianceError(
                f"policy[{index}] operator exceeds the {MAX_RULE_ID_CHARS}-character bound"
            )
        if operator not in OPERATORS:
            raise ComplianceError(f"policy[{index}] operator is not one of {OPERATORS}")

        raw_severity = raw_rule.get("severity") or "error"
        if not isinstance(raw_severity, str):
            raise ComplianceError(f"policy[{index}] severity must be a string")
        if len(raw_severity) > MAX_RULE_ID_CHARS:
            raise ComplianceError(
                f"policy[{index}] severity exceeds the {MAX_RULE_ID_CHARS}-character bound"
            )
        severity = raw_severity.strip().lower()
        if severity not in SEVERITIES:
            raise ComplianceError(f"policy[{index}] severity is not one of {SEVERITIES}")

        optional = raw_rule.get("optional", False)
        if not isinstance(optional, bool):
            raise ComplianceError(f"policy[{index}] optional must be a bool")

        expected = raw_rule.get("expected")
        if operator in _OPERATORS_REQUIRING_EXPECTED:
            if "expected" not in raw_rule:
                raise ComplianceError(
                    f"policy[{index}] operator {operator!r} requires an 'expected' value"
                )
            _validate_expected_shape(operator, expected, index)

        normalized.append(
            {
                "id": rule_id,
                "field": field_path,
                "operator": operator,
                "expected": expected,
                "optional": optional,
                "severity": severity,
            }
        )
    return normalized


# ---------------------------------------------------------------------------
# Per-rule evaluation
# ---------------------------------------------------------------------------


def _evaluate_operator(operator: str, actual: Any, expected: Any) -> bool:
    if operator == "eq":
        return actual == expected
    if operator == "ne":
        return actual != expected
    if operator in ("lt", "le", "gt", "ge"):
        actual_number = _require_numeric(actual, "actual value")
        expected_number = _require_numeric(expected, "expected value")
        if operator == "lt":
            return actual_number < expected_number
        if operator == "le":
            return actual_number <= expected_number
        if operator == "gt":
            return actual_number > expected_number
        return actual_number >= expected_number
    if operator == "contains":
        if isinstance(actual, Mapping):
            return expected in actual
        if isinstance(actual, (list, tuple, set, frozenset)):
            return expected in actual
        if isinstance(actual, str):
            if not isinstance(expected, str):
                raise ComplianceError(
                    "contains against a string actual value requires a string expected value"
                )
            return expected in actual
        raise ComplianceError("contains requires actual to be a string, list, or mapping")
    if operator == "in":
        return actual in expected
    if operator == "regex_fullmatch":
        return _regex_fullmatch(actual, expected)
    if operator == "version_gte":
        return _compare_versions(actual, expected) >= 0
    if operator == "version_range":
        if "min" in expected and expected["min"] is not None:
            if _compare_versions(actual, expected["min"]) < 0:
                return False
        if "max" in expected and expected["max"] is not None:
            if _compare_versions(actual, expected["max"]) > 0:
                return False
        return True
    raise ComplianceError(f"unsupported operator {operator!r}")


def _rule_result(
    rule: Mapping[str, Any],
    *,
    status: str,
    actual: Any,
    message: str = "",
) -> dict[str, Any]:
    return {
        "rule_id": rule["id"],
        "field": rule["field"],
        "operator": rule["operator"],
        "severity": rule["severity"],
        "status": status,
        "actual": actual,
        "message": _truncate(message, MAX_MESSAGE_CHARS),
    }


def evaluate_rule(observation: Mapping[str, Any], rule: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate one already-normalized rule (see :func:`validate_policy`)
    against one observation. Never raises -- a type mismatch or missing
    field becomes an "error" (or "skipped", for an ``optional`` rule with
    an absent field) result instead."""
    field_path = rule["field"]
    operator = rule["operator"]
    expected = rule.get("expected")

    try:
        actual = extract_field(observation, field_path)
    except ComplianceError as exc:
        return _rule_result(rule, status="error", actual=None, message=str(exc))

    found = actual is not _MISSING

    if operator == "not_exists":
        return _rule_result(
            rule,
            status="pass" if not found else "fail",
            actual=_bound_actual(actual, field_path=field_path) if found else None,
        )

    if not found:
        if operator == "exists":
            return _rule_result(rule, status="fail", actual=None, message="field not present")
        if rule.get("optional"):
            return _rule_result(
                rule, status="skipped", actual=None, message="field not present (optional rule)"
            )
        return _rule_result(rule, status="error", actual=None, message="field not present")

    if operator == "exists":
        return _rule_result(
            rule, status="pass", actual=_bound_actual(actual, field_path=field_path)
        )

    bounded_actual = _bound_actual(actual, field_path=field_path)
    try:
        outcome = _evaluate_operator(operator, actual, expected)
    except ComplianceError as exc:
        return _rule_result(rule, status="error", actual=bounded_actual, message=str(exc))
    return _rule_result(rule, status="pass" if outcome else "fail", actual=bounded_actual)


# ---------------------------------------------------------------------------
# Aggregate report
# ---------------------------------------------------------------------------

_EMPTY_COUNTS = {"pass": 0, "fail": 0, "error": 0, "skipped": 0}


def evaluate_policy(
    observations: Any,
    policy: Any,
    *,
    policy_id: str = "ad-hoc",
    max_result_entries: int = MAX_RESULT_ENTRIES,
) -> dict[str, Any]:
    """Evaluate ``observations`` against ``policy``; return a bounded,
    aggregate compliance report.

    Fails closed with :class:`ComplianceError` for a structurally invalid
    ``observations``/``policy`` input *before* evaluating anything. Once
    evaluation starts, a per-rule/per-observation problem (missing field,
    type mismatch, malformed regex/version comparison target) always
    becomes an "error" result for that one rule -- it never raises and
    never aborts the rest of the report.
    """
    if not isinstance(policy_id, str) or not policy_id.strip():
        raise ComplianceError("policy_id must be a non-empty string")
    if len(policy_id) > MAX_POLICY_ID_CHARS:
        raise ComplianceError(
            f"policy_id exceeds the {MAX_POLICY_ID_CHARS}-character bound"
        )
    if isinstance(observations, (str, bytes)) or not isinstance(observations, Sequence):
        raise ComplianceError("observations must be a list of objects")
    if not observations:
        raise ComplianceError("observations must contain at least one entry")
    if len(observations) > MAX_OBSERVATIONS:
        raise ComplianceError(
            f"observations has {len(observations)} entries, exceeding the "
            f"{MAX_OBSERVATIONS}-entry bound"
        )
    for index, observation in enumerate(observations):
        if not isinstance(observation, Mapping):
            raise ComplianceError(f"observations[{index}] must be an object")

    normalized_policy = validate_policy(policy)
    if isinstance(max_result_entries, bool) or not isinstance(max_result_entries, int):
        raise ComplianceError("max_result_entries must be an integer")
    bounded_max_result_entries = max(1, min(max_result_entries, MAX_RESULT_ENTRIES))

    results: list[dict[str, Any]] = []
    results_total = 0
    counts = dict(_EMPTY_COUNTS)
    observation_summaries: list[dict[str, Any]] = []

    for obs_index, observation in enumerate(observations):
        observation_id = _observation_identifier(observation)
        obs_counts = dict(_EMPTY_COUNTS)
        for rule in normalized_policy:
            outcome = evaluate_rule(observation, rule)
            counts[outcome["status"]] += 1
            obs_counts[outcome["status"]] += 1
            results_total += 1
            if len(results) < bounded_max_result_entries:
                entry = dict(outcome)
                entry["observation_index"] = obs_index
                entry["observation_id"] = observation_id
                results.append(entry)
        compliant = obs_counts["fail"] == 0 and obs_counts["error"] == 0
        observation_summaries.append(
            {
                "observation_index": obs_index,
                "observation_id": observation_id,
                "compliant": compliant,
                "counts": obs_counts,
            }
        )

    overall_compliant = all(summary["compliant"] for summary in observation_summaries)

    return {
        "policy_id": str(policy_id),
        "rule_count": len(normalized_policy),
        "observation_count": len(observations),
        "compliant": overall_compliant,
        "counts": counts,
        "observations": observation_summaries,
        "results": results,
        "results_total": results_total,
        "results_truncated": results_total > len(results),
    }


# ---------------------------------------------------------------------------
# Artifact payload shaping -- a plain dict ready for
# hpe_networking_mcp.pipeline.artifact_contracts.build_artifact/write_artifact under the
# compliance_report kind. Kept here (rather than duplicated at each call
# site) so the router tool and any offline report-generation script always
# build the identical shape.
# ---------------------------------------------------------------------------


def build_compliance_report_payload(
    *,
    policy_id: str,
    compliant: bool,
    counts: Mapping[str, int],
    observations: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
    results_total: int,
    generated_at: str | None = None,
) -> dict[str, Any]:
    return {
        "generated_at": generated_at or now_iso(),
        "policy_id": policy_id,
        "compliant": compliant,
        "counts": dict(counts),
        "observations": [dict(summary) for summary in observations],
        "results": [dict(result) for result in results],
        "results_total": results_total,
    }
