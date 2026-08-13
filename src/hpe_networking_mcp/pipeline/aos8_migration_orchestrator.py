"""Atomic, resumable orchestration for AOS8-to-Central migration candidates."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hpe_networking_mcp.pipeline.aos8_target_adapters import (
    MAX_SECRET_LENGTH,
    BaseCentralTargetAdapter,
    ConflictPolicy,
    TargetContext,
    TargetType,
    WriteGateError,
)

MAX_CANDIDATES = 500
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_RESULT_ITEMS = 50
MAX_HISTORY_ITEMS = 10
# Bounds for the explicit, non-secret operator-context maps
# (`external_object_references`, `ap_group_target_map`,
# `ap_group_device_serials`) accepted at the MCP/orchestrator boundary --
# these are operator-declared reference data (an already-existing Classic
# auth-server name; an AP-group -> Classic-group mapping; device serials),
# never secrets, but still caller-controlled input that must be bounded
# before it is ever used to build a `TargetContext`.
#
# Fail-closed contract: these three maps are accepted only by the
# stateless `preview()` path, which persists nothing and may use them
# transiently to build the returned dry-run preview. Every persistent
# workflow (`create_run`, `MigrationRunStore.save`, `apply`, stored
# get/list/history/checkpoint output) rejects any non-empty map outright
# with a clear error -- see `_reject_persisted_operator_context` -- rather
# than storing the raw values, a hash/fingerprint, a count, or any other
# resupply metadata derived from them. There is no verifier for these
# free-form operator identifiers, so persisting even a hash would create
# an offline-guessing surface; not persisting anything at all removes that
# surface entirely.
MAX_OPERATOR_CONTEXT_ENTRIES = 100
MAX_OPERATOR_CONTEXT_STRING_LENGTH = 256
MAX_AP_GROUP_SERIALS_PER_GROUP = 64
MAX_SERIAL_STRING_LENGTH = 64
# `_sanitize`'s truncated-display bound for ordinary (non-secret-context)
# strings: a sanitized leaf string never returns more than this many
# characters, regardless of the original text's length (see
# `_sanitize`'s string branch). This bound is irrelevant whenever
# `secret_values` is non-empty -- in that case every string leaf is
# replaced wholesale by a small, fixed marker, never truncated -- so it
# only ever applies to ordinary backend diagnostics with no runtime
# secret in scope.
_OUTPUT_LIMIT = 1000
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|[_-])(?:credential|key|passphrase|password|psk|"
    r"secret|token)(?:$|[_-])",
    re.IGNORECASE,
)
_SAFE_SECRET_METADATA_KEYS = {
    "requires_secret_input",
    "required_secret_names",
    "secret_fields",
    "secrets_persisted",
}
# Presence-only boolean flags emitted by `src/hpe_networking_mcp/pipeline/aos8_migration.py`
# (`_wlan_security_intent`'s `security.passphrase_present` /
# `security.psk_hexkey_present`). They never carry secret material -- only
# whether a credential field was populated in the AOS8 source -- but their
# names trip `_SENSITIVE_KEY_RE`'s "passphrase"/"psk" tokens. They are
# allowlisted by exact name *and* gated on an actual `bool` value in
# `_is_presence_metadata` below, so a same-named field holding a real secret
# string would still be redacted.
_PRESENCE_ONLY_BOOLEAN_METADATA_KEYS = {
    "passphrase_present",
    "psk_hexkey_present",
}
_TERMINAL_SUCCESS = {"applied", "skipped"}
# A candidate's own `status` never becomes "rolled_back": rollback
# progress is tracked separately, per run, in `run["rollback"]["resume_state"]`
# (see `AOS8MigrationOrchestrator.rollback_plan`/`.execute_rollback` and
# `hpe_networking_mcp.pipeline.aos8_rollback`), never by mutating this field -- so this apply()
# terminal-state machine is unaffected by rollback execution existing.
_TERMINAL = {*_TERMINAL_SUCCESS, "unsupported"}


class MigrationRunError(ValueError):
    """Base error for migration-run validation or persistence."""


class MigrationRunNotFoundError(MigrationRunError):
    """The requested migration run does not exist."""


class MalformedMigrationStateError(MigrationRunError):
    """A persisted migration run cannot be decoded safely."""


AdapterFactory = Callable[[TargetContext], BaseCentralTargetAdapter]


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def validate_run_id(run_id: str) -> str:
    """Validate a run identifier before deriving any state path."""
    value = str(run_id).strip()
    if (
        not _RUN_ID_RE.fullmatch(value)
        or ".." in value
        or "/" in value
        or "\\" in value
    ):
        raise MigrationRunError(
            "run_id must be 1-64 characters using only letters, numbers, '.', '_', "
            "or '-', and may not contain path traversal"
        )
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")


def _is_sensitive_key(key: Any) -> bool:
    normalized = _normalized_key(key)
    if normalized in _SAFE_SECRET_METADATA_KEYS:
        return False
    return bool(
        _SENSITIVE_KEY_RE.search(normalized)
        or normalized.endswith(
            (
                "apikey",
                "credential",
                "credentials",
                "passphrase",
                "passwd",
                "password",
                "privatekey",
                "psk",
                "pwd",
                "secret",
                "sharedkey",
                "token",
            )
        )
        or normalized in {"community_string", "snmp_read", "snmp_write"}
    )


def _is_presence_metadata(key: Any, value: Any) -> bool:
    """Return True only for a known presence-only boolean metadata field.

    Narrow by design: both the exact (normalized) key name must be
    allowlisted *and* the value must actually be a `bool`. This is
    intentionally not a suffix/prefix exception -- a field sharing one of
    these names but holding a non-bool (e.g. an actual secret string) is
    still redacted by `_is_sensitive_key`.
    """
    return (
        isinstance(value, bool)
        and _normalized_key(key) in _PRESENCE_ONLY_BOOLEAN_METADATA_KEYS
    )


# Fixed, generic marker substituted for *every* backend-originated value
# -- of any type or shape -- whenever `_sanitize` is called with a
# non-empty `secret_values` (i.e. at least one real runtime credential --
# a PSK, RADIUS/TACACS+ shared secret, LDAP bind password, ... -- was
# supplied for this call), except for the short list of safe primitive
# scalars preserved unchanged (see `_sanitize`). This is generated
# constant metadata, never echoed secret material: no part of any
# original value ever survives in the output, so there is nothing of the
# caller's choosing left in it to compare the marker against, and no
# need to prove the marker text itself differs from an arbitrary secret
# a caller might choose.
_SECRET_CONTEXT_MARKER = "<redacted:runtime-secret-context>"


def _sanitize(
    value: Any,
    *,
    secret_values: Iterable[str] = (),
    max_depth: int = 8,
    _depth: int = 0,
) -> Any:
    """Sanitize `value` for return/persistence with a simple, provably
    fail-closed rule:

    - If `secret_values` is non-empty -- meaning at least one real
      runtime credential was supplied for this call -- `value` is never
      traversed key-by-key or item-by-item, iterated, consumed, `len()`-
      measured, or passed to `repr()`/`str()`. Exactly one `type(value)
      is ...` check decides the outcome (never `isinstance`, which would
      also admit subclasses), with no further classification of the
      rejected value's category, type, or shape:
        * `None`, a value whose type is *exactly* `bool`, a value whose
          type is *exactly* `int`, and a value whose type is *exactly*
          `float` and is finite, passed *directly* to this call, are
          preserved unchanged -- there is nothing in one of these that
          could carry a mapping key, sequence shape, or secret-shaped
          text, and being an exact-type match (not merely
          `isinstance`-compatible) means no subclass can smuggle a
          secret-bearing attribute, override a dunder, or otherwise piggy-
          back on the primitive fast path.
        * every other value -- a `str`, a `Mapping` (dict or custom
          subclass), a `list`/`tuple`/`set`/`frozenset`/`deque`/`range`,
          a generator or other iterator, `bytes`/`bytearray`/
          `memoryview` or any other buffer provider, a custom `Sequence`/
          `Iterable`/`Iterator`, a non-finite `float` (`inf`/`-inf`/
          `nan`), a `pathlib.Path`, a plain `Enum` member, an int-subtype
          `Enum` member (e.g. `IntEnum`), a numeric type's subclass
          (custom or third-party, e.g. a numpy scalar), a `bool`
          subclass (impossible in CPython but still fails closed rather
          than being assumed unreachable), a `dataclass` instance, a
          `complex`/`Decimal`/`Fraction`, or any other custom object
          (including one whose `__repr__`/`__str__` is itself
          secret-shaped, or one carrying secret-bearing attributes on an
          otherwise-numeric subclass) -- is replaced by the exact same
          fixed `_SECRET_CONTEXT_MARKER` scalar. None of these is ever
          inspected, iterated, indexed, `len()`-measured, scanned,
          matched, encoded/decoded, hashed, or `repr()`/`str()`-ed, and no
          method or attribute of a rejected value -- including one
          defined only on a subclass -- is ever invoked to decide *how*
          to redact it, and no envelope, `_kind`, or other field
          distinguishes one rejected category from another: the mere
          presence of a real secret anywhere in this call's input is
          reason enough to discard every non-exact-primitive value
          outright as one indistinguishable marker, whether or not that
          specific value happens to contain a secret. This makes leakage
          of the secret -- raw, percent-/form-encoded, Unicode,
          mixed-case, serialized (e.g. embedded in a JSON string), stored
          as a mapping key or set/sequence element, stored in a custom
          object's `__repr__`, stored as an attribute on a primitive
          subclass, or prefixed with the marker text itself -- provably
          impossible, because none of those forms, nor even the rejected
          value's type, is ever read or observable in the output.
    - If `secret_values` is empty, mappings/sequences are traversed and
      bounded (`MAX_RESULT_ITEMS`) exactly as before, string leaves are
      returned unchanged (bounded only by `_OUTPUT_LIMIT`, to keep an
      oversized backend diagnostic from growing the response/state file
      without limit), and mapping keys are still redacted by name via
      `_is_sensitive_key` (e.g. a `shared_secret`/`admin_password` field
      found in an arbitrary backend response). This is the normal,
      no-secret-in-scope diagnostic path used by `preview()`, `verify()`,
      and any `apply()` call that was not supplied a real secret.

    Both branches remain depth-limited (`max_depth`) so a huge or
    deeply-nested backend payload cannot force unbounded recursion here
    regardless of whether a secret is present.
    """
    secrets = tuple(secret for secret in secret_values if secret)
    if _depth >= max_depth:
        return "<bounded:max-depth>"
    if secrets:
        # Only the exact-type safe primitive scalars remain unchanged:
        # None, a value whose type is exactly bool, a value whose type
        # is exactly int, and a value whose type is exactly float and is
        # finite. `type(value) is ...` is used deliberately instead of
        # isinstance() so that no subclass -- an int-subtype Enum member
        # (e.g. IntEnum), a bool subclass (impossible in CPython, but
        # not assumed unreachable), a numpy scalar or other third-party
        # numeric subclass, or any custom int/float/bool subclass that
        # carries secret-bearing attributes or overrides __repr__/__eq__/
        # __index__/etc. -- can piggyback on the primitive fast path by
        # merely being isinstance()-compatible. Every other value -- str,
        # Mapping, list/tuple/set/frozenset/deque/range, a generator or
        # other iterator, bytes/bytearray/memoryview or any other buffer
        # provider, a custom Sequence/Iterable/Iterator, a non-finite
        # float, a Path, a plain Enum member, a dataclass instance,
        # Decimal, Fraction, complex, or any other unknown/custom/
        # subclassed object -- fails closed to the same fixed marker
        # without ever calling isinstance against Mapping/Iterable/
        # Iterator, iterating, indexing, len()-ing, or invoking any
        # method/attribute (including one defined only on a subclass) on
        # it.
        if value is None:
            return value
        if type(value) is bool or type(value) is int:
            return value
        if type(value) is float:
            return value if math.isfinite(value) else _SECRET_CONTEXT_MARKER
        return _SECRET_CONTEXT_MARKER
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:MAX_RESULT_ITEMS]:
            key = str(raw_key)
            out[key] = (
                item
                if _is_presence_metadata(key, item)
                else "******"
                if _is_sensitive_key(key)
                else _sanitize(
                    item,
                    max_depth=max_depth,
                    _depth=_depth + 1,
                )
            )
        if len(value) > MAX_RESULT_ITEMS:
            out["_bounded"] = {
                "total_keys": len(value),
                "returned_keys": MAX_RESULT_ITEMS,
            }
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        bounded = [
            _sanitize(
                item,
                max_depth=max_depth,
                _depth=_depth + 1,
            )
            for item in items[:MAX_RESULT_ITEMS]
        ]
        if len(items) > MAX_RESULT_ITEMS:
            bounded.append(
                {
                    "_bounded": {
                        "total_items": len(items),
                        "returned_items": MAX_RESULT_ITEMS,
                    }
                }
            )
        return bounded
    if isinstance(value, str):
        if len(value) > _OUTPUT_LIMIT:
            omitted = len(value) - _OUTPUT_LIMIT
            return f"{value[:_OUTPUT_LIMIT]}... [truncated {omitted} chars]"
        return value
    return value


def _redact_full(value: Any, *, _depth: int = 0) -> Any:
    if _depth >= 12:
        raise MigrationRunError("Migration candidate nesting exceeds the safe limit.")
    if isinstance(value, Mapping):
        return {
            str(key): (
                item
                if _is_presence_metadata(key, item)
                else "******"
                if _is_sensitive_key(key)
                else _redact_full(item, _depth=_depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_full(item, _depth=_depth + 1) for item in value]
    if isinstance(value, set):
        return sorted(_redact_full(item, _depth=_depth + 1) for item in value)
    return value


def _safe_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    safe = _redact_full(candidate)
    if not isinstance(safe, dict):
        raise MigrationRunError("Each migration candidate must be an object.")
    return safe


def _candidate_key(candidate: Mapping[str, Any]) -> str:
    object_type = str(candidate.get("object_type", "")).strip()
    identifier = str(candidate.get("identifier", "")).strip()
    if not object_type or not identifier:
        raise MigrationRunError(
            "Each migration candidate requires non-empty object_type and identifier."
        )
    return f"{object_type}:{identifier}"


def _required_secret_names(candidate: Mapping[str, Any]) -> list[str]:
    if not candidate.get("requires_secret_input"):
        return []
    if candidate.get("object_type") == "auth_server":
        # Type-aware: LDAP's New Central secret is the flat `admin-password`
        # bind-password field (`admin_password`); RADIUS/TACACS both use the
        # nested `shared-secret-config` object (`shared_secret`). See
        # src/hpe_networking_mcp/pipeline/aos8_target_adapters.py `_map_auth_server`/`_auth_server_body`.
        server_type = str((candidate.get("payload") or {}).get("server_type") or "").lower()
        if server_type == "ldap":
            return ["admin_password"]
        return ["shared_secret"]
    names = {
        _normalized_key(str(path).split(".")[-1].split("[", 1)[0])
        for path in candidate.get("secret_fields", [])
    }
    return sorted(name for name in names if name) or ["target_secret"]


def _placeholder_secret_inputs(
    candidates: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, str]]:
    return {
        _candidate_key(candidate): {
            name: "__runtime_secret_placeholder__"
            for name in _required_secret_names(candidate)
        }
        for candidate in candidates
        if candidate.get("requires_secret_input")
    }


def _validate_runtime_secret_lengths(
    supplied_secrets: Mapping[str, Mapping[str, str]],
) -> None:
    """Reject any caller-supplied runtime secret (PSK, RADIUS/TACACS+
    shared secret, LDAP bind password, ...) longer than
    `MAX_SECRET_LENGTH` up front, before it reaches candidate mapping or
    any write invocation.

    This is `apply()`'s runtime counterpart to
    `aos8_target_adapters._secret_value`/`_secret_bundle_error`, which
    enforce the same bound once a `TargetContext` has been built for a
    specific candidate; this check runs first, over every supplied
    secret for the whole request, so an oversized value is refused
    outright rather than discovered only candidate-by-candidate partway
    through a run.
    """
    oversized = sorted(
        f"{key}.{name}"
        for key, bundle in supplied_secrets.items()
        if isinstance(bundle, Mapping)
        for name, value in bundle.items()
        if isinstance(value, str) and len(value) > MAX_SECRET_LENGTH
    )
    if oversized:
        raise MigrationRunError(
            "Target secret inputs exceed the "
            f"{MAX_SECRET_LENGTH}-character runtime secret bound: {oversized}."
        )


def _bounded_operator_string(
    value: Any,
    field_name: str,
    *,
    max_length: int = MAX_OPERATOR_CONTEXT_STRING_LENGTH,
) -> str:
    """Structurally canonicalize and bound one free-form operator-context
    string: type, surrounding-whitespace trim, non-empty, and length only.
    Deliberately never a content/secret-word heuristic -- a legitimate
    Classic group or AP group literally named "Token-Group" or
    "private-key-infra" must be accepted unchanged (see the module note
    above `_validate_external_object_references`).
    """
    if not isinstance(value, str):
        raise MigrationRunError(f"{field_name} must be a string.")
    text = value.strip()
    if not text:
        raise MigrationRunError(f"{field_name} must be a non-empty string.")
    if len(text) > max_length:
        raise MigrationRunError(f"{field_name} exceeds {max_length} characters.")
    return text


# `external_object_references`, `ap_group_target_map`, and
# `ap_group_device_serials` carry operator-declared *reference* strings (an
# existing object's name, an AP-group/Classic-group name, a device serial).
# Their names and values are arbitrary caller-chosen identifiers -- e.g. a
# Classic auth-server profile literally named "Token-Group", or an AP group
# named "private-key-infra" -- so they must never be screened with
# secret-keyword/secret-shaped-content heuristics (`_is_sensitive_key`):
# those heuristics are only sound against dictionary field names with a
# known, fixed schema (e.g. a candidate payload's "shared_secret" field),
# not against free-form operator identifiers. Structural bounds (type,
# non-empty, whitespace-trimmed, length, count) are the only validation
# applied here. The actual secret-persistence risk is eliminated by
# accepting these three maps only from the stateless `preview()` path and
# rejecting any non-empty map outright everywhere else (see
# `_reject_persisted_operator_context` below) -- never by storing the
# values, a hash, a count, or any other resupply metadata.


def _validate_external_object_references(
    value: Any,
) -> dict[str, dict[str, str]]:
    """Bound and validate the explicit, non-secret object-reference map
    (e.g. an already-existing Classic auth-server name for a conditional
    WPA3-Enterprise WLAN). Backward compatible with persisted 0.4 target
    dictionaries, which never had this key: absent/empty input returns {}.
    """
    if not value:
        return {}
    if not isinstance(value, Mapping):
        raise MigrationRunError("external_object_references must be an object.")
    if len(value) > MAX_OPERATOR_CONTEXT_ENTRIES:
        raise MigrationRunError(
            "external_object_references may not exceed "
            f"{MAX_OPERATOR_CONTEXT_ENTRIES} candidate keys."
        )
    bounded: dict[str, dict[str, str]] = {}
    for candidate_key, refs in value.items():
        key_str = _bounded_operator_string(
            candidate_key, "external_object_references key"
        )
        if not isinstance(refs, Mapping):
            raise MigrationRunError(
                f"external_object_references[{key_str!r}] must be an object "
                "of reference name -> value."
            )
        if len(refs) > MAX_OPERATOR_CONTEXT_ENTRIES:
            raise MigrationRunError(
                f"external_object_references[{key_str!r}] may not exceed "
                f"{MAX_OPERATOR_CONTEXT_ENTRIES} entries."
            )
        bounded_refs: dict[str, str] = {}
        for ref_name, ref_value in refs.items():
            ref_name_str = _bounded_operator_string(
                ref_name, "external_object_references reference name"
            )
            bounded_value = _bounded_operator_string(
                ref_value,
                f"external_object_references[{key_str!r}][{ref_name_str!r}]",
            )
            bounded_refs[ref_name_str] = bounded_value
        bounded[key_str] = bounded_refs
    return bounded


def _validate_ap_group_target_map(value: Any) -> dict[str, str]:
    """Bound and validate the explicit, operator-provided AOS8 ap_group name
    -> Classic Central group name mapping. Backward compatible with
    persisted 0.4 target dictionaries: absent/empty input returns {}.
    """
    if not value:
        return {}
    if not isinstance(value, Mapping):
        raise MigrationRunError("ap_group_target_map must be an object.")
    if len(value) > MAX_OPERATOR_CONTEXT_ENTRIES:
        raise MigrationRunError(
            f"ap_group_target_map may not exceed {MAX_OPERATOR_CONTEXT_ENTRIES} entries."
        )
    bounded: dict[str, str] = {}
    for ap_group, classic_group in value.items():
        ap_group_str = _bounded_operator_string(ap_group, "ap_group_target_map key")
        bounded_value = _bounded_operator_string(
            classic_group, f"ap_group_target_map[{ap_group_str!r}]"
        )
        bounded[ap_group_str] = bounded_value
    return bounded


def _validate_ap_group_device_serials(value: Any) -> dict[str, tuple[str, ...]]:
    """Bound and validate the explicit, operator-provided AOS8 ap_group name
    -> device serial numbers mapping. Backward compatible with persisted 0.4
    target dictionaries: absent/empty input returns {}.
    """
    if not value:
        return {}
    if not isinstance(value, Mapping):
        raise MigrationRunError("ap_group_device_serials must be an object.")
    if len(value) > MAX_OPERATOR_CONTEXT_ENTRIES:
        raise MigrationRunError(
            "ap_group_device_serials may not exceed "
            f"{MAX_OPERATOR_CONTEXT_ENTRIES} entries."
        )
    bounded: dict[str, tuple[str, ...]] = {}
    for ap_group, serials in value.items():
        ap_group_str = _bounded_operator_string(ap_group, "ap_group_device_serials key")
        if not isinstance(serials, (list, tuple)):
            raise MigrationRunError(
                f"ap_group_device_serials[{ap_group_str!r}] must be a list "
                "of serial number strings."
            )
        if len(serials) > MAX_AP_GROUP_SERIALS_PER_GROUP:
            raise MigrationRunError(
                f"ap_group_device_serials[{ap_group_str!r}] may not exceed "
                f"{MAX_AP_GROUP_SERIALS_PER_GROUP} serial numbers."
            )
        bounded_serials = []
        for serial in serials:
            bounded_serial = _bounded_operator_string(
                serial,
                f"ap_group_device_serials[{ap_group_str!r}] entry",
                max_length=MAX_SERIAL_STRING_LENGTH,
            )
            bounded_serials.append(bounded_serial)
        bounded[ap_group_str] = tuple(bounded_serials)
    return bounded


def _target_context(
    target: Mapping[str, Any],
    *,
    secret_inputs: Mapping[str, Mapping[str, str]] | None = None,
) -> TargetContext:
    try:
        return TargetContext(
            target_type=TargetType(str(target["type"])),
            scope_id=target.get("scope_id"),
            scope_name=target.get("scope_name"),
            persona=target.get("persona"),
            cluster_name=target.get("cluster_name"),
            cluster_scope_id=target.get("cluster_scope_id"),
            gateway_name=target.get("gateway_name"),
            gateway_scope_id=target.get("gateway_scope_id"),
            conflict_policy=ConflictPolicy(
                str(target.get("conflict_policy", ConflictPolicy.FAIL.value))
            ),
            secret_inputs=secret_inputs or {},
            external_object_references=_validate_external_object_references(
                target.get("external_object_references")
            ),
            ap_group_target_map=_validate_ap_group_target_map(
                target.get("ap_group_target_map")
            ),
            ap_group_device_serials=_validate_ap_group_device_serials(
                target.get("ap_group_device_serials")
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MigrationRunError(f"Invalid persisted target context: {exc}") from exc


# `external_object_references`, `ap_group_target_map`, and
# `ap_group_device_serials` are accepted only by the stateless `preview()`
# path. Every persistent workflow rejects any non-empty map outright (see
# `_reject_persisted_operator_context`) instead of storing the values, a
# hash, a count, or any other resupply metadata -- there is no verifier
# for these free-form operator identifiers, so persisting even a hash
# would create an offline-guessing surface. `_without_operator_context`
# simply drops the (guaranteed-empty, by the time it is called) keys
# before a target dict is persisted, so a persisted `target` never even
# carries the keys -- matching the 0.4 shape these fields did not exist in.
_OPERATOR_CONTEXT_FIELDS = (
    "external_object_references",
    "ap_group_target_map",
    "ap_group_device_serials",
)


def _reject_persisted_operator_context(
    target: Mapping[str, Any], *, workflow: str
) -> None:
    """Fail closed, with a clear and actionable error, if a persistent
    workflow (`create_run`, and by construction anything that only ever
    operates on an already-persisted run's stored target) is asked to use
    a non-empty operator-context map. These maps may be used transiently
    to construct a stateless `preview()` response only; call
    `aos8_preview_migration_run` for that instead.
    """
    offending = [field for field in _OPERATOR_CONTEXT_FIELDS if target.get(field)]
    if offending:
        raise MigrationRunError(
            f"{workflow} cannot accept a non-empty "
            f"{', '.join(sorted(offending))}: operator-context maps are "
            "accepted only by aos8_preview_migration_run's stateless "
            "preview, which does not persist a migration run. Remove "
            "them from this call, or use aos8_preview_migration_run to "
            "review the same mapping without creating a run."
        )


def _without_operator_context(target: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in target.items() if key not in _OPERATOR_CONTEXT_FIELDS
    }


# Generic, count-free/value-free markers used in place of the raw
# `external_object_references`/`ap_group_target_map`/
# `ap_group_device_serials` maps in a stateless preview's echoed
# `target` -- they must show *that* a runtime mapping was (or was not)
# supplied for structural/status purposes, never the map's keys, values,
# or size.
_RUNTIME_CONTEXT_SUPPLIED = "runtime mapping supplied"
_RUNTIME_CONTEXT_NOT_SUPPLIED = "runtime mapping not supplied"


def _operator_context_marker(mapping: Mapping[str, Any]) -> str:
    return _RUNTIME_CONTEXT_SUPPLIED if mapping else _RUNTIME_CONTEXT_NOT_SUPPLIED


# The only fields `_redact_operator_context_operation` ever copies from a
# real operation-preview dict (see
# `BaseCentralTargetAdapter._action_preview`) into its redacted output.
# Every one is a small, controlled value computed from preflight/
# compatibility checks or static adapter-type metadata -- never built
# from, or an echo of, a caller-supplied `external_object_references`/
# `ap_group_target_map`/`ap_group_device_serials` value.
_OPERATOR_CONTEXT_SAFE_OPERATION_FIELDS = (
    "candidate",
    "object_type",
    "status",
    "conflict",
    "write_gate_required",
    "dry_run",
    "dry_run_only",
    "dry_run_only_reason",
)


def _redact_operator_context_operation(operation: Mapping[str, Any]) -> dict[str, Any]:
    """Return a structural, value-free preview of one operation entry for
    a `preview()` call made with a non-empty operator-context map
    (`external_object_references`/`ap_group_target_map`/
    `ap_group_device_serials`).

    Deliberately never a generic string-substitution/scanning pass over
    the operation text (unlike `_sanitize`'s wholesale secret marker for
    real runtime secrets): an operator-context value can legitimately be
    as short as a single character (see `_bounded_operator_string`), so
    no scan over arbitrary operation text could ever be proven safe
    against corrupting unrelated prose that merely shares characters
    with it. Instead, every field that could possibly have been built
    from -- or merely echo -- a runtime operator-context value
    (`operations`/`payload`/`arguments`/`endpoint`, `read_operation`,
    `update_operations`, `delete_operations`, `read_back`, `rollback`,
    `warnings`, `unsupported_warnings`, `blockers`, ...) is omitted
    outright, and only `_OPERATOR_CONTEXT_SAFE_OPERATION_FIELDS` survives:
    the candidate's own key/object type (already known to the caller from
    the source candidate list), status/conflict (computed from
    preflight/compatibility checks), and dry-run/write-gate flags
    (adapter-type-only, static per candidate). No operator value, hash,
    count, prefix/suffix, or derived endpoint/argument/payload is ever
    included.
    """
    redacted = {
        field: operation.get(field)
        for field in _OPERATOR_CONTEXT_SAFE_OPERATION_FIELDS
        if field in operation
    }
    redacted["supported"] = operation.get("status") != "unsupported"
    redacted["runtime_context_details_redacted"] = True
    return redacted


def _run_fingerprint(
    candidates: list[dict[str, Any]],
    target: Mapping[str, Any],
    selected: Iterable[str] | None,
) -> str:
    material = {
        "candidates": candidates,
        "target": dict(target),
        "selected": sorted(selected or ()),
    }
    return hashlib.sha256(_canonical_json(material).encode()).hexdigest()


_LEGACY_OPERATOR_CONTEXT_MARKER = "legacy_operator_context_sanitized"
_LEGACY_CANDIDATE_BLOCKED_MESSAGE = (
    "This candidate's prior result predates this run's operator-context "
    "sanitization and cannot be trusted; recreate the run with "
    "aos8_create_migration_run."
)
# Run-level activity timestamps that record *when* a dry-run/apply/verify
# was last attempted against this run. Each one is set from a candidate
# write/verify pass that may have used the now-removed operator-context
# state to build its payload, so -- like each candidate's own
# `attempts`/`attempt_history`/`last_result`/`verification` -- they are
# untrusted execution history and must be reset to `None`, not merely left
# in place, when healing a stale run. `checkpoint_and_rollback` is
# deliberately excluded: it is static, adapter-type-only guidance (see
# `BaseCentralTargetAdapter.checkpoint_guidance`) that never varies with
# operator-context values or candidate data, so it carries no
# pre-sanitization context to reset.
_LEGACY_RUN_ACTIVITY_FIELDS = (
    "dry_run_attempted_at",
    "last_apply_at",
    "last_verification_at",
)


def _heal_legacy_candidate_entry(entry: Any) -> Any:
    """Reset every field on one candidate entry that could carry a result,
    attempt count, error, or verification record computed while this run
    still held (or was built from) unsafe operator-context state.

    Unlike the raw `target` operator-context values, these cannot be
    exact-match-redacted: the original operator-context values that may
    have shaped a prior write's payload/result are already gone by the
    time this runs, so there is nothing left to match against. They are
    cleared outright -- including the numeric `attempts` counter, which
    is untrusted execution history in its own right (it reflects retries
    made against a payload built from the now-removed operator-context
    values) -- and the candidate is marked durably blocked with a
    generic, value-free message, rather than left holding possibly-tainted
    data. Only the creation identity fields needed to identify the
    candidate (`key`, `candidate`, `requires_secret_input`,
    `required_secret_names`) survive unchanged.
    """
    if not isinstance(entry, Mapping):
        return entry
    healed = dict(entry)
    healed["status"] = "blocked"
    healed["retryable"] = False
    healed["attempts"] = 0
    healed["dry_run_ok"] = False
    healed["last_error"] = _LEGACY_CANDIDATE_BLOCKED_MESSAGE
    healed["last_result"] = None
    healed["attempt_history"] = []
    healed["verification"] = None
    return healed


def _sanitize_legacy_operator_context(
    value: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Heal a genuinely stale state file written before this fail-closed
    contract existed: one that still carries raw
    `external_object_references`/`ap_group_target_map`/
    `ap_group_device_serials` values directly on `target`, and/or the
    non-reversible `operator_context_metadata` fingerprint/count metadata
    an earlier revision persisted instead of the raw values.

    Removing those two fields is not enough on its own: any candidate
    result/error/attempt/attempt-history/verification record recorded
    while the run held that unsafe state, the run-level
    `dry_run_attempted_at`/`last_apply_at`/`last_verification_at`
    activity timestamps from those same attempts, and the run's own
    `fingerprint` (itself derived from `target`+candidates and therefore
    potentially an operator-derived hash), could still encode
    operator-supplied values or counts. All of that is removed/reset
    here too -- never just the two triggering fields -- and the run and
    every candidate are marked durably blocked/recreate-required with a
    generic message. Only creation identity/source metadata needed to
    identify the run (`run_id`, `schema_version`, `created_at`, the
    sanitized `target`) survives untouched. Never mutates `value` in
    place; returns `(possibly-sanitized run, whether anything changed)`.
    """
    changed = False
    sanitized = dict(value)
    target = sanitized.get("target")
    if isinstance(target, dict) and any(
        field in target for field in _OPERATOR_CONTEXT_FIELDS
    ):
        sanitized["target"] = _without_operator_context(target)
        changed = True
    if "operator_context_metadata" in sanitized:
        del sanitized["operator_context_metadata"]
        changed = True
    if changed:
        healed_target = (
            sanitized["target"] if isinstance(sanitized.get("target"), dict) else {}
        )
        healed_candidates = [
            _heal_legacy_candidate_entry(entry)
            for entry in sanitized.get("candidates", [])
        ]
        sanitized["candidates"] = healed_candidates
        sanitized["status"] = "blocked"
        sanitized["updated_at"] = _now()
        # Every run-level activity timestamp reflects an attempt made
        # against the now-removed operator-context state; reset each one
        # to `None` (the same "never attempted" value `create_run` uses),
        # exactly like each candidate's own reset attempt/history fields
        # above -- these are untrusted execution history, not identity.
        for field in _LEGACY_RUN_ACTIVITY_FIELDS:
            sanitized[field] = None
        # The stored `fingerprint` (and the removed
        # `operator_context_metadata`'s embedded hash, if present) may
        # have been derived, directly or indirectly, from the now-removed
        # raw operator-context values -- recompute it purely from the
        # sanitized target/candidates so no operator-derived hash
        # survives on disk; there is no raw value left to reuse, so this
        # is a fresh, independent fingerprint, not the old one.
        sanitized["fingerprint"] = _run_fingerprint(
            [
                entry.get("candidate", {})
                for entry in healed_candidates
                if isinstance(entry, Mapping)
            ],
            healed_target,
            None,
        )
        sanitized[_LEGACY_OPERATOR_CONTEXT_MARKER] = {
            "removed_at": _now(),
            "reason": (
                "This run's on-disk state was written by a prior revision "
                "that could persist operator-supplied "
                "external_object_references/ap_group_target_map/"
                "ap_group_device_serials values, or a resupply fingerprint "
                "derived from them. Those fields, this run's fingerprint, "
                "every candidate's prior attempts/attempt-history/"
                "result/error/verification record, and this run's "
                "dry_run_attempted_at/last_apply_at/last_verification_at "
                "activity timestamps have all been removed or reset -- "
                "none of them can be trusted to be free of "
                "operator-derived values or counts. This run is durably "
                "blocked and cannot be applied: recreate it with "
                "aos8_create_migration_run, and use a fresh "
                "aos8_preview_migration_run first for any "
                "context-dependent mapping (e.g. WPA3-Enterprise, "
                "AP-group)."
            ),
        }
    return sanitized, changed


class MigrationRunStore:
    """Per-run JSON state under ``state/``, persisted by atomic replacement."""

    _run_locks_guard = threading.Lock()
    _run_locks: dict[tuple[str, str], threading.RLock] = {}

    def __init__(self, state_dir: str | Path = "state/aos8_migrations") -> None:
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def path_for(self, run_id: str) -> Path:
        validated = validate_run_id(run_id)
        path = (self.state_dir / f"{validated}.json").resolve()
        if path.parent != self.state_dir:
            raise MigrationRunError("run_id resolved outside the migration state directory")
        return path

    @contextmanager
    def lock_run(self, run_id: str) -> Iterator[None]:
        """Serialize state transitions for one run across store instances."""
        validated = validate_run_id(run_id)
        key = (str(self.state_dir), validated)
        with self._run_locks_guard:
            lock = self._run_locks.setdefault(key, threading.RLock())
        with lock:
            yield

    def load(self, run_id: str) -> dict[str, Any]:
        validated = validate_run_id(run_id)
        with self.lock_run(validated):
            return self._load_locked(validated)

    def _load_locked(self, run_id: str) -> dict[str, Any]:
        path = self.path_for(run_id)
        if not path.exists():
            raise MigrationRunNotFoundError(f"Migration run {run_id!r} was not found.")
        try:
            if path.stat().st_size > MAX_STATE_BYTES:
                raise MalformedMigrationStateError(
                    f"Migration run {run_id!r} exceeds the state-size limit."
                )
            value = json.loads(path.read_text(encoding="utf-8"))
        except MalformedMigrationStateError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MalformedMigrationStateError(
                f"Migration run {run_id!r} is malformed: {exc}"
            ) from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != 1
            or value.get("run_id") != validate_run_id(run_id)
            or not isinstance(value.get("candidates"), list)
        ):
            raise MalformedMigrationStateError(
                f"Migration run {run_id!r} has an invalid state schema."
            )
        # A state file written before this fail-closed contract existed
        # (or hand-edited) could still carry raw operator-context values on
        # `target`, or the non-reversible fingerprint/count metadata an
        # earlier revision persisted instead. Heal it now: rewrite the
        # actual file with those fields removed and a durable warning
        # marker added, so a stale on-disk value is never served back
        # through `get_run`/`list_runs`/`apply`/`verify`, and the healed
        # version -- not just an in-memory copy -- is what is on disk
        # afterward.
        sanitized, changed = _sanitize_legacy_operator_context(value)
        if changed:
            self._write_locked(sanitized)
        return sanitized

    def save(self, run: Mapping[str, Any]) -> None:
        run_id = validate_run_id(str(run.get("run_id", "")))
        # Hard backstop, not a silent sanitizer: normal code paths
        # (`create_run`) must reject a non-empty operator-context map
        # before ever calling `save()` -- see
        # `_reject_persisted_operator_context`. If one somehow reaches
        # here regardless, that is a bug in the caller and `save()` fails
        # loudly rather than quietly persisting or dropping it.
        target = run.get("target")
        if isinstance(target, dict):
            offending = [
                field for field in _OPERATOR_CONTEXT_FIELDS if target.get(field)
            ]
            if offending:
                raise MigrationRunError(
                    f"Refusing to persist run {run_id!r}: {', '.join(offending)} "
                    "is non-empty. Operator-context maps must be rejected "
                    "before a persistent workflow ever calls save()."
                )
            if any(field in target for field in _OPERATOR_CONTEXT_FIELDS):
                run = {**run, "target": _without_operator_context(target)}
        with self.lock_run(run_id):
            self._write_locked(run)

    def _write_locked(self, run: Mapping[str, Any]) -> None:
        run_id = validate_run_id(str(run.get("run_id", "")))
        payload = _canonical_json(run).encode("utf-8")
        if len(payload) > MAX_STATE_BYTES:
            raise MigrationRunError(
                f"Migration run {run_id!r} exceeds the {MAX_STATE_BYTES}-byte state limit."
            )
        destination = self.path_for(run_id)
        temporary = destination.with_name(
            f".{destination.name}.{uuid.uuid4().hex}.new"
        )
        with self._lock:
            try:
                with temporary.open("xb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
                try:
                    directory_fd = os.open(self.state_dir, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
            finally:
                if temporary.exists():
                    temporary.unlink()

    def list_runs(
        self,
        *,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        paths = sorted(
            self.state_dir.glob("*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        summaries: list[dict[str, Any]] = []
        malformed: list[dict[str, str]] = []
        for path in paths:
            try:
                run = self.load(path.stem)
            except MigrationRunError as exc:
                if len(malformed) < MAX_RESULT_ITEMS:
                    malformed.append({"run_id": path.stem, "error": str(exc)})
                continue
            summaries.append(_run_summary(run))
        bounded_limit = max(1, min(int(limit), 100))
        bounded_offset = max(0, int(offset))
        return {
            "runs": summaries[bounded_offset : bounded_offset + bounded_limit],
            "pagination": {
                "offset": bounded_offset,
                "limit": bounded_limit,
                "total": len(summaries),
                "truncated": bounded_offset + bounded_limit < len(summaries),
            },
            "malformed_state_count": len(malformed),
            "malformed_states": malformed[:10],
        }


def _status_counts(run: Mapping[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in run.get("candidates", []):
        status = str(entry.get("status", "pending"))
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _refresh_run_status(run: dict[str, Any]) -> None:
    statuses = [str(entry.get("status", "pending")) for entry in run["candidates"]]
    if statuses and all(status in _TERMINAL for status in statuses):
        run["status"] = (
            "completed_with_issues" if "unsupported" in statuses else "completed"
        )
    elif "failed" in statuses:
        run["status"] = (
            "partial" if any(status in _TERMINAL_SUCCESS for status in statuses) else "failed"
        )
    elif any(status in _TERMINAL_SUCCESS for status in statuses):
        run["status"] = "partial"
    elif run.get("dry_run_attempted_at"):
        run["status"] = "dry-run-complete"
    else:
        run["status"] = "pending"
    run["updated_at"] = _now()


def _run_summary(run: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "target": run.get("target"),
        # Present only when `MigrationRunStore.load()` healed a genuinely
        # stale state file that predated this fail-closed contract (see
        # `_sanitize_legacy_operator_context`). A durable warning, never a
        # value or hash: this run cannot be applied and must be recreated.
        "legacy_operator_context_sanitized": run.get(
            _LEGACY_OPERATOR_CONTEXT_MARKER
        ),
        "candidate_count": len(run.get("candidates", [])),
        "status_counts": _status_counts(run),
        "dry_run_attempted_at": run.get("dry_run_attempted_at"),
        "last_apply_at": run.get("last_apply_at"),
        "last_verification_at": run.get("last_verification_at"),
        # 0.5: no rollback execution path exists; `checkpoint_and_rollback`
        # below is the pre-existing, unrelated New Central/Classic Central
        # device checkpoint guidance (see BaseCentralTargetAdapter.
        # checkpoint_guidance), not a claim about this orchestrator's own
        # (nonexistent) rollback capability.
        "checkpoint_and_rollback": run.get("checkpoint_and_rollback"),
    }


def _entry_summary(entry: Mapping[str, Any], *, include_details: bool) -> dict[str, Any]:
    out = {
        "candidate": entry.get("key"),
        "object_type": entry.get("candidate", {}).get("object_type"),
        "identifier": entry.get("candidate", {}).get("identifier"),
        "dependencies": entry.get("candidate", {}).get("dependencies", []),
        "status": entry.get("status"),
        "retryable": entry.get("retryable", False),
        "attempts": entry.get("attempts", 0),
        "requires_secret_input": entry.get("requires_secret_input", False),
        "required_secret_names": entry.get("required_secret_names", []),
        "last_error": entry.get("last_error"),
        "dry_run_ok": entry.get("dry_run_ok", False),
        "verification": entry.get("verification"),
    }
    if include_details:
        out["source_candidate"] = entry.get("candidate")
        out["last_result"] = entry.get("last_result")
        out["attempt_history"] = entry.get("attempt_history", [])
    return out


class AOS8MigrationOrchestrator:
    """Create, apply, resume, and verify bounded AOS8 migration runs."""

    def __init__(
        self,
        store: MigrationRunStore,
        adapter_factory: AdapterFactory,
    ) -> None:
        self.store = store
        self.adapter_factory = adapter_factory

    def _adapter(
        self,
        target: Mapping[str, Any],
        candidates: list[Mapping[str, Any]],
        *,
        secret_inputs: Mapping[str, Mapping[str, str]] | None = None,
        placeholders: bool = False,
    ) -> BaseCentralTargetAdapter:
        secrets = (
            _placeholder_secret_inputs(candidates)
            if placeholders
            else dict(secret_inputs or {})
        )
        return self.adapter_factory(_target_context(target, secret_inputs=secrets))

    def preview(
        self,
        candidates: Iterable[Mapping[str, Any]],
        target: Mapping[str, Any],
        *,
        selected: Iterable[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        safe_candidates = self._validate_candidates(candidates)
        selected_set = set(selected) if selected is not None else None
        adapter = self._adapter(target, safe_candidates, placeholders=True)
        preview = adapter.preview(safe_candidates, selected=selected_set)
        operations = preview.get("operations", [])
        bounded_limit = max(1, min(int(limit), 200))
        bounded_offset = max(0, int(offset))
        preview["operations"] = operations[
            bounded_offset : bounded_offset + bounded_limit
        ]
        preview["pagination"] = {
            "offset": bounded_offset,
            "limit": bounded_limit,
            "total": len(operations),
            "truncated": bounded_offset + bounded_limit < len(operations),
        }
        preview["candidate_count"] = len(operations)
        preview["secrets_persisted"] = False
        # `preview()` is stateless -- nothing it returns is written to disk
        # -- but that does not mean the raw operator-context values (an
        # already-existing Classic auth-server name, an AP-group target
        # group name, device serials) are safe to echo back: they are
        # still runtime-supplied, potentially-identifying data. This flag
        # documents only that they are never *persisted*, not that they
        # are shown unredacted below.
        preview["operator_context_persisted"] = False
        context = adapter.context
        # Replace the raw `target.external_object_references`/
        # `ap_group_target_map`/`ap_group_device_serials` echo with a
        # generic, value-free/count-free marker: the caller can see
        # *whether* a runtime mapping was supplied for this preview, never
        # its keys, values, or size.
        preview["target"] = {
            **preview.get("target", {}),
            "external_object_references": _operator_context_marker(
                context.external_object_references
            ),
            "ap_group_target_map": _operator_context_marker(
                context.ap_group_target_map
            ),
            "ap_group_device_serials": _operator_context_marker(
                context.ap_group_device_serials
            ),
        }
        # Defense in depth: the same raw operator-context values may still
        # have been used transiently to construct operation payloads
        # elsewhere in this preview (e.g. WPA3-Enterprise's `auth_server1`
        # in a create/update payload). Never attempt a generic string
        # substitution/scan over that text to find and mask it -- an
        # operator-context value can legitimately be as short as a single
        # character (see `_bounded_operator_string`), so no scan could
        # ever be proven safe against corrupting unrelated prose that
        # merely shares characters with it. Instead, every operation entry
        # is replaced outright by a small, fixed, controlled structural
        # summary (`_redact_operator_context_operation`) that omits every
        # field that could possibly have been built from -- or merely
        # echo -- a runtime operator-context value (arguments, payloads,
        # endpoints, read/update/delete operation details, blockers,
        # warnings, results), keeping only status/supported/conflict and
        # dry-run/write-gate flags.
        has_operator_context = bool(
            context.external_object_references
            or context.ap_group_target_map
            or context.ap_group_device_serials
        )
        if has_operator_context:
            preview["operations"] = [
                _redact_operator_context_operation(operation)
                for operation in preview["operations"]
                if isinstance(operation, Mapping)
            ]
            preview["runtime_context_details_redacted"] = True
        # No secrets are ever real here (`placeholders=True` injects only
        # the fixed, non-secret `__runtime_secret_placeholder__` literal --
        # see `_placeholder_secret_inputs`), so `_sanitize` runs with no
        # `secret_values`: it only bounds/depth-limits the structure and
        # masks any stray sensitive-named key, exactly as it would for any
        # other no-secret-in-scope diagnostic.
        return _sanitize(preview)

    def create_run(
        self,
        candidates: Iterable[Mapping[str, Any]],
        target: Mapping[str, Any],
        *,
        selected: Iterable[str] | None = None,
        run_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        # Fail closed before doing anything else: `external_object_references`/
        # `ap_group_target_map`/`ap_group_device_serials` are accepted only
        # by the stateless `preview()` path. A persistent run must never be
        # created from a target that carries a non-empty one.
        _reject_persisted_operator_context(
            target, workflow="aos8_create_migration_run"
        )
        safe_candidates = self._validate_candidates(candidates)
        selected_set = set(selected) if selected is not None else None
        adapter = self._adapter(target, safe_candidates, placeholders=True)
        full_preview = adapter.preview(safe_candidates, selected=selected_set)
        persisted_target = _without_operator_context(full_preview["target"])
        operation_by_key = {
            str(operation["candidate"]): operation
            for operation in full_preview.get("operations", [])
        }
        candidate_by_key = {
            _candidate_key(candidate): candidate for candidate in safe_candidates
        }
        selected_candidates = [
            candidate_by_key[str(operation["candidate"])]
            for operation in full_preview.get("operations", [])
        ]
        fingerprint = _run_fingerprint(
            selected_candidates,
            persisted_target,
            operation_by_key,
        )
        resolved_run_id = validate_run_id(
            run_id or f"aos8-{fingerprint[:16]}"
        )
        path = self.store.path_for(resolved_run_id)
        if path.exists():
            existing = self.store.load(resolved_run_id)
            if existing.get("fingerprint") != fingerprint:
                raise MigrationRunError(
                    f"Migration run {resolved_run_id!r} already exists with different input."
                )
            return self.get_run(
                resolved_run_id,
                limit=limit,
                offset=offset,
                include_details=False,
            )

        entries: list[dict[str, Any]] = []
        for candidate in selected_candidates:
            key = _candidate_key(candidate)
            operation = operation_by_key[key]
            initial_status = str(operation.get("status", "pending"))
            if initial_status == "ready":
                initial_status = "pending"
            retryable = initial_status in {"pending", "blocked", "failed"}
            errors = [
                *operation.get("unsupported_warnings", []),
                *operation.get("blockers", []),
            ]
            joined_errors = "; ".join(errors) if errors else None
            entries.append(
                {
                    "key": key,
                    "candidate": candidate,
                    "status": initial_status,
                    "retryable": retryable,
                    "attempts": 0,
                    "requires_secret_input": bool(
                        candidate.get("requires_secret_input")
                    ),
                    "required_secret_names": _required_secret_names(candidate),
                    "dry_run_ok": initial_status == "skipped",
                    "last_error": _sanitize(joined_errors) if joined_errors else None,
                    "last_result": None,
                    "attempt_history": [],
                    "verification": None,
                }
            )
        created_at = _now()
        run: dict[str, Any] = {
            "schema_version": 1,
            "run_id": resolved_run_id,
            "fingerprint": fingerprint,
            "status": "pending",
            "created_at": created_at,
            "updated_at": created_at,
            "target": persisted_target,
            "checkpoint_and_rollback": full_preview["checkpoint_and_rollback"],
            "dry_run_attempted_at": None,
            "last_apply_at": None,
            "last_verification_at": None,
            "candidates": entries,
        }
        _refresh_run_status(run)
        self.store.save(run)
        return self.get_run(
            resolved_run_id,
            limit=limit,
            offset=offset,
            include_details=False,
        )

    def get_run(
        self,
        run_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        include_details: bool = False,
    ) -> dict[str, Any]:
        run = self.store.load(run_id)
        bounded_limit = max(1, min(int(limit), 200))
        bounded_offset = max(0, int(offset))
        entries = run["candidates"]
        return {
            **_run_summary(run),
            "fingerprint": run.get("fingerprint"),
            "secrets_persisted": False,
            "candidates": [
                _entry_summary(entry, include_details=include_details)
                for entry in entries[
                    bounded_offset : bounded_offset + bounded_limit
                ]
            ],
            "pagination": {
                "offset": bounded_offset,
                "limit": bounded_limit,
                "total": len(entries),
                "truncated": bounded_offset + bounded_limit < len(entries),
            },
        }

    def list_runs(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        return self.store.list_runs(limit=limit, offset=offset)

    def apply(
        self,
        run_id: str,
        *,
        dry_run: bool,
        confirmation: bool,
        target_secrets: Mapping[str, Mapping[str, str]] | None = None,
        retry_failed: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        # Fail closed before touching the run at all: an oversized secret
        # is a caller-input error independent of any specific run's
        # state, so it is rejected up front -- before mapping, any write
        # invocation, or `_sanitize` -- rather than partway through
        # per-candidate processing below.
        _validate_runtime_secret_lengths(target_secrets or {})
        with self.store.lock_run(run_id):
            return self._apply_locked(
                run_id,
                dry_run=dry_run,
                confirmation=confirmation,
                target_secrets=target_secrets,
                retry_failed=retry_failed,
                limit=limit,
                offset=offset,
            )

    def _apply_locked(
        self,
        run_id: str,
        *,
        dry_run: bool,
        confirmation: bool,
        target_secrets: Mapping[str, Mapping[str, str]] | None,
        retry_failed: bool,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        run = self.store.load(run_id)
        # A run healed from a genuinely stale, pre-fix state file (see
        # `_sanitize_legacy_operator_context`) may have been created with
        # operator context that could change how its candidates map --
        # that context is gone now (never stored, never resuppliable), so
        # this run must not be applied at all: it must be recreated.
        if run.get(_LEGACY_OPERATOR_CONTEXT_MARKER):
            raise MigrationRunError(
                f"Migration run {run_id!r} contained unsafe legacy "
                "operator-context data that has been removed from its "
                "on-disk state (see run['legacy_operator_context_sanitized'])."
                " It cannot be applied: recreate it with "
                "aos8_create_migration_run."
            )
        effective_target = run["target"]
        supplied_secrets = dict(target_secrets or {})
        secret_values = tuple(
            value
            for bundle in supplied_secrets.values()
            for value in bundle.values()
            if isinstance(value, str) and value
        )
        if not dry_run and not confirmation:
            raise WriteGateError(
                "Real migration apply requires confirmation=True."
            )
        if not dry_run and not run.get("dry_run_attempted_at"):
            raise WriteGateError(
                "Run aos8_apply_migration_run with dry_run=True before real writes."
            )

        candidates = [entry["candidate"] for entry in run["candidates"]]
        adapter = self._adapter(
            effective_target,
            candidates,
            secret_inputs=supplied_secrets,
        )
        by_key = {entry["key"]: entry for entry in run["candidates"]}
        attempted_keys: list[str] = []
        for entry in run["candidates"]:
            key = str(entry["key"])
            status = str(entry.get("status", "pending"))
            if status in _TERMINAL or status == "applied":
                continue
            if status == "failed" and not retry_failed:
                continue
            if status == "blocked" and not entry.get("retryable", False):
                continue
            if status not in {"pending", "blocked", "failed"}:
                continue

            attempted_keys.append(key)
            entry["attempts"] = int(entry.get("attempts", 0)) + 1
            if entry.get("requires_secret_input"):
                missing = [
                    name
                    for name in entry.get("required_secret_names", [])
                    if not isinstance(supplied_secrets.get(key, {}).get(name), str)
                    or not supplied_secrets[key][name].strip()
                ]
                if missing:
                    self._record_entry(
                        run,
                        entry,
                        mode="dry-run" if dry_run else "apply",
                        status="blocked",
                        error=(
                            "Caller must supply target secrets again for this "
                            f"attempt: {missing}"
                        ),
                        result=None,
                        retryable=True,
                        secret_values=secret_values,
                    )
                    continue

            inline_dependencies = adapter.candidate_action(
                entry["candidate"]
            ).inline_dependencies
            dependency_success = {
                dependency
                for dependency in entry["candidate"].get("dependencies", [])
                if dependency in inline_dependencies
                or (
                    dependency in by_key
                    and (
                        by_key[dependency].get("status") in _TERMINAL_SUCCESS
                        if not dry_run
                        else bool(by_key[dependency].get("dry_run_ok"))
                    )
                )
            }
            dependency_failures = [
                dependency
                for dependency in entry["candidate"].get("dependencies", [])
                if dependency not in dependency_success
            ]
            if dependency_failures:
                self._record_entry(
                    run,
                    entry,
                    mode="dry-run" if dry_run else "apply",
                    status="blocked",
                    error=(
                        "Dependencies have not completed successfully: "
                        f"{sorted(dependency_failures)}"
                    ),
                    result=None,
                    retryable=True,
                    secret_values=secret_values,
                )
                continue
            if not dry_run and not entry.get("dry_run_ok"):
                self._record_entry(
                    run,
                    entry,
                    mode="apply",
                    status="blocked",
                    error="A successful dry-run is required before applying this candidate.",
                    result=None,
                    retryable=True,
                    secret_values=secret_values,
                )
                continue

            options = {
                "selected": {key},
                "include_dependency_closure": False,
                "allow_unresolved_blockers": True,
                "satisfied_dependencies": dependency_success,
            }
            try:
                result = (
                    adapter.dry_run(candidates, **options)
                    if dry_run
                    else adapter.execute(
                        candidates,
                        dry_run=False,
                        confirmation=True,
                        **options,
                    )
                )
                candidate_result = next(
                    (
                        item
                        for item in result.get("results", [])
                        if item.get("candidate") == key
                    ),
                    {
                        "candidate": key,
                        "status": "failed",
                        "errors": ["Adapter returned no candidate result."],
                        "results": [],
                    },
                )
                result_status = str(candidate_result.get("status", "failed"))
                if dry_run and result_status == "dry-run":
                    entry["dry_run_ok"] = True
                    persisted_status = "pending"
                    retryable = True
                else:
                    persisted_status = result_status
                    retryable = result_status in {"failed", "blocked"}
                error = "; ".join(
                    str(item) for item in candidate_result.get("errors", []) if item
                ) or None
                self._record_entry(
                    run,
                    entry,
                    mode="dry-run" if dry_run else "apply",
                    status=persisted_status,
                    error=error,
                    result=candidate_result,
                    retryable=retryable,
                    secret_values=secret_values,
                )
            except Exception as exc:
                self._record_entry(
                    run,
                    entry,
                    mode="dry-run" if dry_run else "apply",
                    status="failed",
                    error=str(exc),
                    result=None,
                    retryable=True,
                    secret_values=secret_values,
                )

        if dry_run:
            run["dry_run_attempted_at"] = _now()
        else:
            run["last_apply_at"] = _now()
        _refresh_run_status(run)
        self.store.save(run)
        response = self.get_run(
            run_id,
            limit=limit,
            offset=offset,
            include_details=True,
        )
        response["dry_run"] = dry_run
        response["attempted_candidates"] = attempted_keys[:MAX_RESULT_ITEMS]
        response["retry_failed"] = retry_failed
        # Every backend-originated string in this response was already
        # redacted wholesale, in place, at the moment it was computed --
        # see `_record_entry`'s `_sanitize(..., secret_values=...)` calls
        # for `last_error`/`last_result`/`attempt_history`, using this
        # call's actual `secret_values`. This final pass runs with no
        # `secret_values` of its own: it only applies the ordinary
        # bounding/depth-limiting/sensitive-key masking every apply()
        # response gets (the same "normal bounded backend diagnostics"
        # a no-secret apply call returns), never a second wholesale
        # secret pass over the whole response -- which would otherwise
        # also blast orchestrator-controlled fields that never came from
        # the backend at all (`status`, `run_id`, candidate keys,
        # timestamps, ...) even though they cannot carry the secret.
        return _sanitize(response)

    def _record_entry(
        self,
        run: dict[str, Any],
        entry: dict[str, Any],
        *,
        mode: str,
        status: str,
        error: str | None,
        result: Any,
        retryable: bool,
        secret_values: Iterable[str],
    ) -> None:
        safe_error = _sanitize(error, secret_values=secret_values) if error else None
        safe_result = _sanitize(result, secret_values=secret_values)
        entry["status"] = status
        entry["retryable"] = retryable
        entry["last_error"] = safe_error
        entry["last_result"] = safe_result
        history = list(entry.get("attempt_history", []))
        history.append(
            {
                "at": _now(),
                "mode": mode,
                "status": status,
                "error": safe_error,
                "result": safe_result,
            }
        )
        entry["attempt_history"] = history[-MAX_HISTORY_ITEMS:]
        _refresh_run_status(run)
        self.store.save(run)

    def verify(
        self,
        run_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        with self.store.lock_run(run_id):
            return self._verify_locked(run_id, limit=limit, offset=offset)

    def _verify_locked(
        self,
        run_id: str,
        *,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        run = self.store.load(run_id)
        candidates = [entry["candidate"] for entry in run["candidates"]]
        # Persistent runs never carry `external_object_references`/
        # `ap_group_target_map`/`ap_group_device_serials` (rejected at
        # `create_run()` time -- see `_reject_persisted_operator_context`),
        # so no operator context is available or needed here regardless:
        # WPA3-Enterprise is unconditionally `dry_run_only` (real execution
        # always refused) and AP-group mappings never leave `unsupported`
        # (contract matrix §5/§6.11), so neither family can ever reach the
        # terminal-success state `verify()` inspects.
        adapter = self._adapter(run["target"], candidates, placeholders=True)
        bounded_limit = max(1, min(int(limit), 200))
        bounded_offset = max(0, int(offset))
        selected_entries = run["candidates"][
            bounded_offset : bounded_offset + bounded_limit
        ]
        comparisons: list[dict[str, Any]] = []
        for entry in selected_entries:
            verification = self._verify_entry(adapter, entry)
            entry["verification"] = verification
            comparisons.append(verification)
            _refresh_run_status(run)
            self.store.save(run)
        run["last_verification_at"] = _now()
        _refresh_run_status(run)
        self.store.save(run)
        return {
            "run_id": run_id,
            "read_only": True,
            "verification_scope": (
                "Identity presence plus directly comparable returned fields only; "
                "this does not claim full semantic equivalence."
            ),
            "comparisons": comparisons,
            "pagination": {
                "offset": bounded_offset,
                "limit": bounded_limit,
                "total": len(run["candidates"]),
                "truncated": bounded_offset + bounded_limit < len(run["candidates"]),
            },
            "checkpoint_and_rollback": run["checkpoint_and_rollback"],
        }

    def _verify_entry(
        self,
        adapter: BaseCentralTargetAdapter,
        entry: Mapping[str, Any],
    ) -> dict[str, Any]:
        key = str(entry["key"])
        status = str(entry.get("status"))
        source = _sanitize(entry.get("candidate"))
        base = {
            "candidate": key,
            "apply_status": status,
            "source_candidate_intent": source,
            "apply_result": _sanitize(entry.get("last_result")),
        }
        action = adapter.candidate_action(entry["candidate"])
        if status == "unsupported":
            # Matrix-driven fail-closed families (policies, AP groups,
            # routes, VRRP, controllers, and every other candidate this
            # target has no verified write mapping for) are never
            # "unverifiable" (that term is reserved below for a *supported*
            # mapping that simply has no read path) -- they get their own
            # distinct terminal status so a caller can never mistake "no
            # mapping exists" for "a mapping exists but couldn't be read".
            return {
                **base,
                "verification_status": "unsupported",
                "reason": "Candidate is unsupported and remains unapplied.",
                "target_state": None,
                "field_comparison": [],
            }
        if status not in _TERMINAL_SUCCESS:
            # `pending`/`blocked`/`failed`: this candidate was never
            # successfully applied by hpe-networking-mcp, so none of
            # "verified"/"partially_verified"/"failed" is reachable here --
            # every one of those presupposes a completed write this
            # orchestrator actually made. This is precisely the blocked
            # auth-server/server-group/AAA/dot1x/macauth-profile case
            # (contract matrix SS6.3-SS6.8): those mappings are held at
            # "blocked" specifically because their SHARED config-assignment
            # binding is unverified, and must never be reported as
            # applied/verified here either. The only thing safely offered
            # is a bounded, best-effort, read-only diagnostic of whether an
            # object sharing this identity already exists at the target --
            # useful operator context, never a verification claim.
            diagnostic = _diagnostic_read(adapter, action, entry["candidate"])
            return {
                **base,
                "verification_status": "not_applied",
                "reason": (
                    f"Candidate is not successfully applied (status={status}); "
                    "no field-level verification is performed for a candidate "
                    "that was never applied. See `diagnostic` for a read-only, "
                    "best-effort presence check -- informational only, never "
                    "a verification result."
                ),
                "diagnostic": diagnostic,
                "target_state": None,
                "field_comparison": [],
            }
        if action.read_operation is None:
            return {
                **base,
                "verification_status": "unverifiable",
                "reason": "No verified read operation exists for this mapping.",
                "target_state": None,
                "field_comparison": [],
            }
        try:
            target_state = adapter.read_invoker(action.read_operation)
        except Exception as exc:
            return {
                **base,
                "verification_status": "failed",
                "reason": f"Target verification read failed: {exc}",
                "target_state": None,
                "field_comparison": [],
            }
        safe_target = _sanitize(target_state)
        identifier = action.read_operation.match_identifier or str(
            entry["candidate"].get("identifier")
        )
        # Finding (items 8/9): a collection-shaped read response (e.g.
        # `list_roles`, `list_config_assignments`) is never assumed fully
        # inspected just because this process happened to read *a* page of
        # it. `_resolve_flatten_source` recognizes a collection envelope,
        # detects backend-declared pagination metadata and this process's
        # own `MAX_RESULT_ITEMS`/`_sanitize` bounding, pages forward
        # (bounded) when the read operation's own arguments make that
        # safe, and falls back to the whole response as the comparison
        # source for an ordinary single-object read (e.g. `get_ssid`) or a
        # nested/scalar array *property* of one (e.g. a server-group's own
        # `servers` list) exactly as before this existed.
        resolved = _resolve_flatten_source(
            adapter, action.read_operation, safe_target, identifier
        )
        if resolved["status"] == "ambiguous":
            return {
                **base,
                "verification_status": "failed",
                "reason": (
                    "Target read returned more than one entry matching this "
                    "candidate's identity; refusing to guess which one is "
                    f"authoritative ({resolved['match_count']} matches)."
                ),
                "target_state": safe_target,
                "field_comparison": [],
            }
        if resolved["status"] == "indeterminate":
            # A truncated collection read must never report a definitive
            # "not found" -- the candidate's identity may simply be among
            # the unseen entries. This is exactly the finding: a match
            # past `MAX_RESULT_ITEMS` (or a page boundary the backend
            # declared but this process could not safely page past) was
            # previously indistinguishable from a genuine absence.
            return {
                **base,
                "verification_status": "unverifiable",
                "reason": (
                    "Target collection read is bounded and the candidate "
                    "identity was not found among the inspected entries; unseen "
                    f"entries remain, so absence cannot be safely concluded "
                    f"({resolved['note']})."
                ),
                "target_state": safe_target,
                "field_comparison": [],
            }
        if resolved["status"] == "not_found":
            return {
                **base,
                "verification_status": "failed",
                "reason": "Target verification did not find the candidate identity.",
                "target_state": safe_target,
                "field_comparison": [],
            }
        flatten_source = resolved["flatten_source"]
        truncated = resolved["truncated"]
        truncation_note = resolved["note"]
        expected, secret_fields = _expected_fields(action, entry["candidate"])
        target_fields = _flatten_fields(flatten_source)
        # Item 3: some Classic full_wlan expected fields genuinely disagree
        # across containers under the same bare name once the `wlan.`/
        # `access_rule.` prefix is stripped -- e.g. `wlan.blacklist` is
        # always the constant `True` and `access_rule.blacklist` is always
        # the constant `False` (`_base_full_wlan_body`). A flat response
        # collapses both to one `blacklist` key that can only agree with
        # one of them, which is a structural ambiguity, not a genuine
        # mismatch, for either field. Precomputed once so the per-field
        # loop can tell that apart from a real single-source mismatch like
        # `wlan.essid` (only ever expected under `wlan`, so a disagreeing
        # flat `essid` is unambiguous evidence of a real problem).
        bare_expected_values: dict[str, list[Any]] = {}
        for candidate_field, candidate_value in expected.items():
            if "." in candidate_field and "[" not in candidate_field:
                prefix, _, remainder = candidate_field.partition(".")
                if prefix in _CLASSIC_FULL_WLAN_CONTAINER_KEYS and remainder:
                    bare_expected_values.setdefault(remainder, []).append(candidate_value)
        comparisons: list[dict[str, Any]] = []
        mismatches: list[str] = []
        verified_fields: list[str] = []
        unverifiable_fields: list[str] = []
        for field, expected_value in expected.items():
            if field in target_fields:
                # The exact qualified expected path is itself present in
                # the actual flattened response -- compare it exclusively,
                # whether it matches or not. A bare/stripped alias (e.g.
                # `_field_aliases`' Classic `wlan.`/`access_rule.`
                # bare-remainder alias) must never be consulted once the
                # exact path is present: an unrelated sibling field that
                # happens to share the same bare remainder name (e.g. a
                # different container's `essid`) could otherwise silently
                # paper over a genuine mismatch in the exact expected path.
                # Aliases are only ever a fallback for when the exact
                # qualified path itself was not returned at all (e.g. a
                # flat Classic response with no `wlan`/`access_rule`
                # wrapper) -- see the `else` branch below.
                matches = [target_fields[field]]
            else:
                aliases = _field_aliases(field)
                matches = [
                    target_fields[alias]
                    for alias in aliases
                    if alias in target_fields
                ]
            if not matches:
                # Explicitly reported, not silently skipped: the target read
                # simply did not return this field (e.g. it is write-only, or
                # the read shape differs from the write shape).
                comparisons.append(
                    {
                        "field": field,
                        "expected": expected_value,
                        "actual": None,
                        "status": "unverifiable",
                        "reason": "field was not present in the target read response",
                    }
                )
                unverifiable_fields.append(field)
                continue
            matched = any(_comparable_equal(expected_value, actual) for actual in matches)
            if not matched and field not in target_fields and "." in field and "[" not in field:
                prefix, _, remainder = field.partition(".")
                distinct_values = {
                    str(v) for v in bare_expected_values.get(remainder, [])
                }
                if prefix in _CLASSIC_FULL_WLAN_CONTAINER_KEYS and len(distinct_values) > 1:
                    # This field's own qualified key is absent (flat
                    # response) and the only evidence found was the bare
                    # container-prefix alias -- but that same bare name is
                    # *also* independently expected, with a genuinely
                    # different value, by a sibling Classic container
                    # field. A flat single-valued response cannot satisfy
                    # both, so this disagreement is not reliable evidence
                    # of a real mismatch for this field specifically --
                    # report unverifiable rather than a false "mismatch".
                    comparisons.append(
                        {
                            "field": field,
                            "expected": expected_value,
                            "actual": None,
                            "status": "unverifiable",
                            "reason": (
                                "field's own qualified key was not present in the "
                                "flat target read response, and the shared bare "
                                f"{remainder!r} name is ambiguous across Classic "
                                "wlan/access_rule containers with different "
                                "expected values"
                            ),
                        }
                    )
                    unverifiable_fields.append(field)
                    continue
            comparisons.append(
                {
                    "field": field,
                    "expected": expected_value,
                    "actual": matches[0],
                    "status": "match" if matched else "mismatch",
                }
            )
            if matched:
                verified_fields.append(field)
            else:
                mismatches.append(field)
        for field in sorted(secret_fields):
            # Secrets are never returned by a GET -- report as unverifiable,
            # never as a mismatch (which would be a false negative for every
            # secret-bearing candidate).
            comparisons.append(
                {
                    "field": field,
                    "expected": "***",
                    "actual": None,
                    "status": "unverifiable",
                    "reason": "secret field is not returned by target reads and cannot be verified",
                }
            )
            unverifiable_fields.append(field)

        comparable_fields = [f for f in expected if f not in secret_fields]
        # "identifier" is the identity field already confirmed by the
        # `_contains_identifier` gate above; it must not, by itself, count
        # as a verified *payload* field for the purposes of this decision --
        # otherwise a candidate with real payload fields that are all
        # unverifiable would still be reported "verified" on identity alone.
        payload_fields = [f for f in comparable_fields if f != "identifier"]
        # Finding #3: if ANY expected non-secret payload field is absent or
        # otherwise unverifiable against the target read, status must be
        # "partially_verified" -- never "verified" -- even when other
        # payload fields did match. Full "verified" now requires every
        # non-secret payload field to be individually confirmed.
        payload_unverifiable = [f for f in payload_fields if f in unverifiable_fields]
        if mismatches:
            primary_status = "failed"
            primary_reason = f"Directly comparable fields differed: {sorted(mismatches)}"
        elif payload_unverifiable:
            primary_status = "partially_verified"
            primary_reason = (
                "Candidate identity was present, but one or more non-secret "
                "payload fields could not be confirmed against the target "
                f"read response: {sorted(payload_unverifiable)}"
            )
        else:
            primary_status = "verified"
            primary_reason = (
                "Candidate identity was present; directly comparable returned "
                "fields matched."
                + (
                    f" Unverifiable fields (not returned by the read, or secret "
                    f"and never returned): {sorted(unverifiable_fields)}."
                    if unverifiable_fields
                    else " Unreturned fields were not asserted."
                )
            )
        if truncated and primary_status == "verified":
            # Item 9: a single visible match within a truncated/bounded
            # collection read is never conclusively unique -- an unseen
            # entry beyond the inspection window (or backend page) might
            # also match this candidate's identity. "Verified" asserts
            # both correctness *and* uniqueness; only the former was
            # actually confirmed here, so this can never be reported
            # stronger than "partially_verified".
            primary_status = "partially_verified"
            primary_reason = (
                f"{primary_reason} However, the target collection read is "
                "bounded and additional unseen entries might also match this "
                "candidate's identity, so uniqueness cannot be guaranteed "
                f"({truncation_note})."
            )
        result: dict[str, Any] = {
            **base,
            "verification_status": primary_status,
            "reason": primary_reason,
            "target_state": safe_target,
            "field_comparison": comparisons,
        }

        if action.assignment_read_operation is not None:
            # Finding (item 4): a role/SHARED-profile library object and
            # its scope+device-function config-assignment are independent
            # facts -- an object can be a real, field-verified object while
            # its assignment is missing/mismatched (an orphaned object,
            # unusable at the target). Never collapse that distinction by
            # reporting only the stronger of the two; aggregate
            # conservatively -- overall status is never stronger than
            # either half.
            assignment_verification = _verify_assignment(adapter, action)
            result["assignment_verification"] = assignment_verification
            assignment_status = assignment_verification["status"]
            # "unverifiable" (a truncated assignment collection read that
            # never found the candidate's identity at all) is at least as
            # inconclusive as "partially_verified" and must downgrade the
            # aggregate the same way; unrecognized statuses default to the
            # most conservative ("failed"-equivalent) severity.
            _severity = {
                "verified": 0,
                "partially_verified": 1,
                "unverifiable": 1,
                "failed": 2,
            }
            result["reason"] = (
                f"{primary_reason} Object verification: {primary_status}; "
                f"assignment verification: {assignment_status} -- "
                f"{assignment_verification['reason']}"
            )
            if _severity.get(assignment_status, 2) > _severity.get(primary_status, 0):
                result["verification_status"] = assignment_status
        return result

    def rollback_plan(
        self,
        run_id: str,
        *,
        target_secrets: Mapping[str, Mapping[str, str]] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Build a reverse-dependency-order rollback plan for one migration
        run's already-*applied* candidates (see `hpe_networking_mcp.pipeline.aos8_rollback`).

        Read-only and stateless: never writes to the run's persisted
        state. `target_secrets` is used only transiently to construct the
        target adapter -- some mappings need a caller-supplied secret to
        re-derive a candidate's `CandidateAction` at all (e.g. a WLAN
        mapping requires `security.mode`'s passphrase-required check) --
        and is never persisted, matching `apply()`'s own secret-handling
        contract.
        """
        from hpe_networking_mcp.pipeline.aos8_rollback import plan_rollback

        _validate_runtime_secret_lengths(target_secrets or {})
        run = self.store.load(run_id)
        applied = [
            entry["candidate"]
            for entry in run["candidates"]
            if entry.get("status") == "applied"
        ]
        adapter = self._adapter(
            run["target"], applied, secret_inputs=target_secrets or {}
        )
        plan = plan_rollback(applied, adapter.candidate_action)
        serialized = plan.to_dict()
        steps = serialized["steps"]
        bounded_limit = max(1, min(int(limit), 200))
        bounded_offset = max(0, int(offset))
        serialized["steps"] = steps[bounded_offset : bounded_offset + bounded_limit]
        serialized["pagination"] = {
            "offset": bounded_offset,
            "limit": bounded_limit,
            "total": len(steps),
            "truncated": bounded_offset + bounded_limit < len(steps),
        }
        serialized["run_id"] = run_id
        secret_values = tuple(
            value
            for bundle in (target_secrets or {}).values()
            for value in bundle.values()
            if isinstance(value, str) and value
        )
        return _sanitize(serialized, secret_values=secret_values)

    def execute_rollback(
        self,
        run_id: str,
        *,
        dry_run: bool,
        confirmation: bool,
        target_secrets: Mapping[str, Mapping[str, str]] | None = None,
        conflict_policy: str = "abort",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Dry-run or execute a reverse-dependency-order rollback of one
        migration run's already-*applied* candidates, resuming from any
        prior partial rollback attempt persisted on this same run (see
        `hpe_networking_mcp.pipeline.aos8_rollback.execute_rollback_plan`).

        Real (non-dry-run) execution requires `confirmation=True` *and*
        `hpe_networking_mcp.pipeline.aos8_rollback.rollback_writes_enabled()` (a gate
        separate from, and in addition to, the ordinary migration-apply
        write gate governing `adapter.write_invoker` itself) *and* the
        ordinary per-target write gate
        (`adapter.writes_enabled(adapter.target_type)`) -- rollback is
        never authorized by only one of these. `target_secrets` is never
        persisted (same contract as `apply()`); only which steps
        completed (`resume_state`, by candidate key) is persisted, so a
        resumed rollback never re-issues a delete against an object it
        already confirmed gone.
        """
        from hpe_networking_mcp.pipeline.aos8_rollback import (
            RollbackConflictPolicy,
            execute_rollback_plan,
            plan_rollback,
        )

        try:
            policy = RollbackConflictPolicy(conflict_policy)
        except ValueError as exc:
            raise MigrationRunError(
                f"Unknown rollback conflict_policy {conflict_policy!r}; expected "
                f"one of {[item.value for item in RollbackConflictPolicy]}"
            ) from exc
        _validate_runtime_secret_lengths(target_secrets or {})
        secret_values = tuple(
            value
            for bundle in (target_secrets or {}).values()
            for value in bundle.values()
            if isinstance(value, str) and value
        )

        with self.store.lock_run(run_id):
            run = self.store.load(run_id)
            applied = [
                entry["candidate"]
                for entry in run["candidates"]
                if entry.get("status") == "applied"
            ]
            adapter = self._adapter(
                run["target"], applied, secret_inputs=target_secrets or {}
            )
            if not dry_run and not adapter.writes_enabled(adapter.target_type):
                raise WriteGateError(
                    f"Platform writes are disabled for {adapter.target_type.value}."
                )
            plan = plan_rollback(applied, adapter.candidate_action)
            resume_state = dict(run.get("rollback", {}).get("resume_state", {}))
            result = execute_rollback_plan(
                plan,
                dry_run=dry_run,
                confirmation=confirmation,
                write_invoker=adapter.write_invoker,
                conflict_policy=policy,
                resume_from=resume_state,
            )
            if not dry_run:
                for entry in result["results"]:
                    if entry["status"] in {"applied", "already_applied"}:
                        resume_state[entry["candidate"]] = {
                            "status": "applied",
                            "completed_operations": entry["operation_count"],
                        }
                    elif entry["status"] == "failed" and entry["completed_operations"]:
                        resume_state[entry["candidate"]] = {
                            "status": "failed",
                            "completed_operations": entry["completed_operations"],
                        }
                run["rollback"] = {
                    "resume_state": resume_state,
                    "last_run_at": _now(),
                    "last_summary": result["summary"],
                }
                self.store.save(run)

        result["run_id"] = run_id
        steps = result["results"]
        bounded_limit = max(1, min(int(limit), 200))
        bounded_offset = max(0, int(offset))
        result["results"] = steps[bounded_offset : bounded_offset + bounded_limit]
        result["pagination"] = {
            "offset": bounded_offset,
            "limit": bounded_limit,
            "total": len(steps),
            "truncated": bounded_offset + bounded_limit < len(steps),
        }
        return _sanitize(result, secret_values=secret_values)

    @staticmethod
    def _validate_candidates(
        candidates: Iterable[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        values = list(candidates)
        if not values:
            raise MigrationRunError("At least one migration candidate is required.")
        if len(values) > MAX_CANDIDATES:
            raise MigrationRunError(
                f"Migration runs are limited to {MAX_CANDIDATES} candidates."
            )
        safe = [_safe_candidate(candidate) for candidate in values]
        keys = [_candidate_key(candidate) for candidate in safe]
        if len(set(keys)) != len(keys):
            raise MigrationRunError("Migration candidate keys must be unique.")
        return safe


def _contains_identifier(value: Any, identifier: str) -> bool:
    if value is None:
        return False
    if isinstance(value, list):
        return any(_contains_identifier(item, identifier) for item in value)
    if isinstance(value, Mapping):
        if value.get("found") is False:
            return False
        error = str(value.get("error", ""))
        if "404" in error or "not found" in error.lower():
            return False
        identity_fields = (
            "name",
            "ssid",
            "vlan",
            "vlan_id",
            "vlan-id",
            "profile-name",
            "profile-instance",
            "id",
        )
        if any(str(value.get(field)) == identifier for field in identity_fields):
            return True
        return any(_contains_identifier(item, identifier) for item in value.values())
    return False


# Bounded, best-effort extra paging for a truncated collection-shaped
# verification read (see `_bounded_collection_read`). Both bounds exist so
# a pathological/adversarial "always more pages" backend can never turn a
# single verification read into unbounded work -- paging is a strictly
# safer alternative to reporting an indeterminate result, never a way to
# defeat these limits.
MAX_VERIFICATION_EXTRA_PAGES = 3
MAX_VERIFICATION_TOTAL_ITEMS = 200

# Sibling-key names (normalized via `_normalized_key`, so hyphen/
# underscore/case variants all match) this repository has evidence for as
# backend-declared "more entries exist than were returned" signals --
# `bound_collection_response`'s own `_pagination.total`, and the more
# generic raw-backend `total`/`count` envelope shapes.
_PAGINATION_TOTAL_KEYS = (
    "total",
    "total_count",
    "totalcount",
    "count",
    "total_items",
    "totalitems",
)
# Sibling-key names for an explicit "there is a next page" cursor/flag.
_PAGINATION_CURSOR_KEYS = (
    "next",
    "next_offset",
    "nextoffset",
    "next_page",
    "nextpage",
    "cursor",
)


def _pagination_metadata_truncated(
    container: Mapping[str, Any], returned_count: int
) -> bool:
    """Best-effort detection that `container` -- the sibling keys of a
    collection envelope, e.g. `bound_collection_response`'s own
    `{"items": [...], "_pagination": {"offset":.., "limit":.., "total":..,
    "truncated":..}}` shape, or a raw backend `total`/`count`/`next`
    envelope -- declares more entries exist than were actually returned in
    this read. Checks both `container` itself and a nested `_pagination`
    mapping so a raw, un-wrapped backend shape is recognized just as
    reliably as this repository's own bounding helper.
    """
    candidates: list[Mapping[str, Any]] = [container]
    nested = container.get("_pagination")
    if isinstance(nested, Mapping):
        candidates.append(nested)
    for meta in candidates:
        normalized = {_normalized_key(k): v for k, v in meta.items()}
        truncated_flag = normalized.get("truncated")
        if isinstance(truncated_flag, bool) and truncated_flag:
            return True
        for key in _PAGINATION_CURSOR_KEYS:
            if normalized.get(key):
                return True
        offset_value = normalized.get("offset")
        offset = (
            offset_value
            if isinstance(offset_value, (int, float))
            and not isinstance(offset_value, bool)
            else 0
        )
        for key in _PAGINATION_TOTAL_KEYS:
            total = normalized.get(key)
            if (
                isinstance(total, (int, float))
                and not isinstance(total, bool)
                and total > offset + returned_count
            ):
                return True
    return False


def _strip_bounding_marker(
    items: list[Any],
) -> tuple[list[Any], Mapping[str, Any] | None]:
    """Strip the trailing `{"_bounded": {"total_items":.., "returned_items":
    ..}}` marker `_sanitize` appends to a list it truncated to
    `MAX_RESULT_ITEMS`, returning the real entries and the marker's own
    metadata (`None` if the list was not marker-truncated). Without this,
    a `_sanitize`-truncated list is indistinguishable from a genuinely
    complete one of the same length.
    """
    if items and isinstance(items[-1], Mapping) and set(items[-1].keys()) == {"_bounded"}:
        return items[:-1], items[-1]["_bounded"]
    return items, None


def _extract_collection_container(
    value: Any,
) -> tuple[list[Any], Mapping[str, Any]] | None:
    """Return `(raw_items, container)` if `value` is recognizably a single,
    unambiguous collection envelope (a bare list, or a mapping with
    exactly one top-level list-valued key), or `None` otherwise -- the
    same narrow recognition contract the old `_collection_candidates`
    implemented directly, split out so pagination/truncation detection can
    run on the *unbounded* item list before any `MAX_RESULT_ITEMS` slicing,
    and so a bare list still exposes an (empty) `container` for
    pagination-metadata lookups without a special case at every call site.
    A response with more than one top-level list-valued key (an envelope
    shape this repository has no evidence for) is left unclassified rather
    than guessed.
    """
    if isinstance(value, list):
        return value, {}
    if isinstance(value, Mapping):
        list_values = [item for item in value.values() if isinstance(item, list)]
        if len(list_values) == 1:
            return list_values[0], value
    return None


def _collection_read(value: Any) -> dict[str, Any] | None:
    """Return the bounded read state of `value` as a collection response,
    or `None` if `value` is not recognizably a single, unambiguous
    collection envelope (see `_extract_collection_container`).

    The returned dict has:
      - `items`: the (`MAX_RESULT_ITEMS`-bounded) entries actually
        available for comparison.
      - `truncated`: True whenever entries beyond `items` might exist --
        this process's own `_sanitize`/`MAX_RESULT_ITEMS` bounding, a raw
        response that itself exceeded `MAX_RESULT_ITEMS`, or
        backend-declared pagination metadata
        (`_pagination_metadata_truncated`) -- so a caller can never report
        a definitive not-found/verified/unique conclusion from only a
        partial view of the collection.
      - `note`: a human-readable explanation of every truncation signal
        detected, or `None` when the full collection was inspected.
    """
    extracted = _extract_collection_container(value)
    if extracted is None:
        return None
    raw_items, container = extracted
    items, marker = _strip_bounding_marker(raw_items)
    notes: list[str] = []
    truncated = False
    if marker is not None:
        truncated = True
        notes.append(
            "response bounding already truncated "
            f"{marker.get('total_items')} entries to "
            f"{marker.get('returned_items')} before verification inspected them"
        )
    if len(items) > MAX_RESULT_ITEMS:
        truncated = True
        notes.append(
            f"{len(items)} entries exceeded the {MAX_RESULT_ITEMS}-item "
            "verification inspection bound"
        )
    if _pagination_metadata_truncated(container, len(items)):
        truncated = True
        notes.append(
            "backend pagination metadata indicates additional unread entries exist"
        )
    return {
        "items": items[:MAX_RESULT_ITEMS],
        "truncated": truncated,
        "note": "; ".join(notes) if notes else None,
    }


def _pageable_operation(operation: Any) -> bool:
    """True when `operation` explicitly declares numeric `limit`/`offset`
    arguments (and not `full_list=True`), so an additional bounded page can
    safely be requested with the same tool by advancing `offset` -- never
    guessed for an operation whose arguments give no such signal. The
    production `list_roles`/`list_config_assignments` mappings
    (`aos8_target_adapters.NewCentralAdapter._map_role`) always declare an
    explicit bounded `limit`/`offset` page for exactly this reason -- an
    operation that instead requested `full_list=True` would have already
    had the backend return everything it has in one call, so re-issuing
    the identical call would not surface anything new; only `_sanitize`'s
    own safety bounding would limit what this process inspects in that
    case, and paging could not fix that.
    """
    if operation.invocation != "tool":
        return False
    arguments = operation.arguments or {}
    if arguments.get("full_list"):
        return False
    limit = arguments.get("limit")
    offset = arguments.get("offset")
    return (
        isinstance(limit, int)
        and not isinstance(limit, bool)
        and limit > 0
        and isinstance(offset, int)
        and not isinstance(offset, bool)
    )


def _bounded_collection_read(
    adapter: BaseCentralTargetAdapter, operation: Any, safe_target: Any
) -> dict[str, Any] | None:
    """Resolve the bounded, truncation-aware collection state for a
    verification read, paging forward (bounded by
    `MAX_VERIFICATION_EXTRA_PAGES`/`MAX_VERIFICATION_TOTAL_ITEMS`) when
    `operation` declares explicit `limit`/`offset` arguments and the
    already-read page is truncated -- preferring a safely-bounded
    additional read over reporting an indeterminate result whenever more
    of the collection can still be fetched. Returns `None` when
    `safe_target` is not recognizably a collection response at all (an
    ordinary single-object read, e.g. `get_ssid`); the caller falls back
    to its existing non-collection path unchanged.
    """
    state = _collection_read(safe_target)
    if state is None:
        return None
    items = list(state["items"])
    truncated = state["truncated"]
    notes = [state["note"]] if state["note"] else []
    pages_fetched = 0
    current_operation = operation
    while (
        truncated
        and pages_fetched < MAX_VERIFICATION_EXTRA_PAGES
        and len(items) < MAX_VERIFICATION_TOTAL_ITEMS
        and _pageable_operation(current_operation)
    ):
        arguments = dict(current_operation.arguments)
        next_offset = int(arguments["offset"]) + int(arguments["limit"])
        current_operation = replace(
            current_operation, arguments={**arguments, "offset": next_offset}
        )
        try:
            next_target = adapter.read_invoker(current_operation)
        except Exception:
            # A follow-up page failing is not itself a verification
            # failure of the evidence already gathered -- stop paging and
            # report the truncated/indeterminate state built so far.
            notes.append(
                f"a follow-up page at offset={next_offset} could not be read; "
                "paging stopped"
            )
            break
        pages_fetched += 1
        next_state = _collection_read(_sanitize(next_target))
        if next_state is None:
            notes.append(
                f"a follow-up page at offset={next_offset} was not a recognizable "
                "collection response; paging stopped"
            )
            break
        items.extend(next_state["items"])
        truncated = next_state["truncated"]
        if next_state["note"]:
            notes.append(next_state["note"])
    if truncated and pages_fetched >= MAX_VERIFICATION_EXTRA_PAGES:
        notes.append(
            f"verification paging is bounded to {MAX_VERIFICATION_EXTRA_PAGES} "
            "additional page(s); further entries may remain unread"
        )
    if truncated and len(items) >= MAX_VERIFICATION_TOTAL_ITEMS:
        notes.append(
            f"verification paging is bounded to {MAX_VERIFICATION_TOTAL_ITEMS} "
            "total items; further entries may remain unread"
        )
    return {
        "items": items[:MAX_VERIFICATION_TOTAL_ITEMS],
        "truncated": truncated,
        "note": "; ".join(dict.fromkeys(notes)) if notes else None,
    }


def _resolve_flatten_source(
    adapter: BaseCentralTargetAdapter,
    operation: Any,
    safe_target: Any,
    identifier: str,
) -> dict[str, Any]:
    """Resolve the single object `_flatten_fields` should compare against
    for `safe_target`/`identifier`, sharing one truncation-aware
    resolution between `_verify_entry` and `_verify_assignment` (items
    8/9 of the aos8-verification contract).

    Returns a dict with `status` of:
      - `"not_found"` -- the identity is confirmed absent: not found
        anywhere in `safe_target`, and (if `safe_target` is recognizably a
        collection response at all) the collection was fully inspected
        (never truncated). This is the only status that may report a
        definitive "not found".
      - `"indeterminate"` -- the identity was not found among the
        inspected entries of a *collection* response, but that collection
        was bounded/truncated -- unseen entries might still contain it, so
        absence can never be safely concluded.
      - `"ambiguous"` -- more than one entry within the inspected
        collection window matches the identity; refuse to guess which is
        authoritative.
      - `"ok"` -- a single comparison source was resolved: either the one
        collection entry whose identity matched (`truncated`/`note`
        describe whether unseen entries might duplicate it), or the whole
        `safe_target` response verbatim when it is not recognizably a
        collection-of-objects response at all, or when it is but no
        distinct collection entry matched even though the identity was
        confirmed present elsewhere in the response (e.g. a scalar-valued
        array property of an otherwise ordinary single-object read, such
        as `get_ssid`'s `vlan_ids`, or a nested object's own array field,
        such as a server-group's `servers` list) -- in both of the latter
        cases `truncated` is always False, since the whole-object
        fallback makes no uniqueness claim for `_bounded_collection_read`
        to have bounded in the first place.
    """
    found_anywhere = _contains_identifier(safe_target, identifier)
    collection_state = _bounded_collection_read(adapter, operation, safe_target)
    if collection_state is not None:
        matches = [
            item
            for item in collection_state["items"]
            if isinstance(item, Mapping) and _contains_identifier(item, identifier)
        ]
        if len(matches) > 1:
            return {
                "status": "ambiguous",
                "match_count": len(matches),
                "target_state": safe_target,
            }
        if len(matches) == 1:
            return {
                "status": "ok",
                "flatten_source": matches[0],
                "truncated": collection_state["truncated"],
                "note": collection_state["note"],
                "target_state": safe_target,
            }
        # Zero entries within the recognized collection matched. This is
        # never itself "not found" -- the recognized "collection" may
        # simply be a scalar-valued or nested-object array property of an
        # otherwise ordinary single-object response (see docstring), not a
        # list of independently-identified candidate objects to search
        # among; whether the identity is genuinely present is decided by
        # `found_anywhere` below exactly as if no collection had been
        # recognized at all.
        if not found_anywhere and collection_state["truncated"]:
            return {
                "status": "indeterminate",
                "note": collection_state["note"],
                "target_state": safe_target,
            }
    if not found_anywhere:
        return {"status": "not_found", "target_state": safe_target}
    return {
        "status": "ok",
        "flatten_source": safe_target,
        "truncated": False,
        "note": None,
        "target_state": safe_target,
    }


def _diagnostic_read(
    adapter: BaseCentralTargetAdapter,
    action: Any,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Best-effort, bounded, read-only presence check for a candidate that
    was never successfully applied by hpe-networking-mcp (`pending`/`blocked`/
    `failed`). Never claims verification or application -- only whether an
    object sharing this identity is already, independently present at the
    target, which is useful context for an operator deciding how to
    unblock the candidate (contract matrix SS6.3-SS6.8: a blocked
    auth-server/server-group/AAA/dot1x/macauth-profile mapping must never
    be reported applied/verified, but a safe read-only diagnostic is
    still offered whenever a verified read path exists).
    """
    if action.read_operation is None:
        return {
            "attempted": False,
            "target_object_present": None,
            "note": (
                "No verified read operation exists for this mapping; manual "
                "verification is required."
            ),
        }
    try:
        target_state = adapter.read_invoker(action.read_operation)
    except Exception as exc:
        return {
            "attempted": True,
            "target_object_present": None,
            "note": f"Diagnostic read failed: {exc}",
        }
    safe_target = _sanitize(target_state)
    identifier = action.read_operation.match_identifier or str(
        candidate.get("identifier")
    )
    # Shares `_verify_entry`'s truncation-aware resolution (items 8/9): a
    # truncated/paginated collection read can never rule the candidate
    # absent, and a scalar/nested-object array property of an ordinary
    # single-object response (e.g. a server-group's own `servers` list)
    # must never be mistaken for "no candidate object present" either.
    resolved = _resolve_flatten_source(adapter, action.read_operation, safe_target, identifier)
    note = (
        "Read-only diagnostic only; this candidate was not applied by "
        "hpe-networking-mcp and no field-level verification was performed."
    )
    if resolved["status"] == "indeterminate":
        return {
            "attempted": True,
            "target_object_present": None,
            "note": f"{note} Bounded/truncated collection read: {resolved['note']}.",
        }
    if resolved["status"] == "not_found":
        return {"attempted": True, "target_object_present": False, "note": note}
    return {"attempted": True, "target_object_present": True, "note": note}


def _verify_assignment(
    adapter: BaseCentralTargetAdapter, action: Any
) -> dict[str, Any]:
    """Bounded, read-only verification of a SHARED library object's
    scope+device-function config-assignment tuple (`scope-id`,
    `device-function`, `profile-type`, `profile-instance`) -- independent
    of, and never conflated with, the library object's own field
    verification performed by `_verify_entry` (item 4 of the
    aos8-verification contract: the object can be verified while its
    assignment fails/is partial, or vice versa).
    """
    operation = action.assignment_read_operation
    expected = {
        _normalized_key(name): value
        for name, value in dict(getattr(action, "assignment_expected", {}) or {}).items()
    }
    identifier = operation.match_identifier or (
        str(expected["profile_instance"]) if "profile_instance" in expected else None
    )
    if identifier is None:
        return {
            "status": "failed",
            "reason": "No config-assignment entry was found for this candidate's identity.",
            "field_comparison": [],
        }
    try:
        target_state = adapter.read_invoker(operation)
    except Exception as exc:
        return {
            "status": "failed",
            "reason": f"Assignment verification read failed: {exc}",
            "field_comparison": [],
        }
    safe_target = _sanitize(target_state)
    # Same truncation-aware collection resolution `_verify_entry` uses for
    # the primary object read (items 8/9): a config-assignment list is
    # bounded/paginated exactly the same way, so "not found" and "exactly
    # one match" must never be reported as definitive conclusions from
    # only a partial view of it.
    resolved = _resolve_flatten_source(adapter, operation, safe_target, str(identifier))
    if resolved["status"] == "ambiguous":
        return {
            "status": "failed",
            "reason": (
                "Target returned more than one config-assignment entry "
                "matching this candidate's identity; refusing to guess "
                f"which is authoritative ({resolved['match_count']} matches)."
            ),
            "field_comparison": [],
        }
    if resolved["status"] == "indeterminate":
        return {
            "status": "unverifiable",
            "reason": (
                "Target config-assignment read is bounded and this candidate's "
                "identity was not found among the inspected entries; unseen "
                f"entries remain, so absence cannot be safely concluded "
                f"({resolved['note']})."
            ),
            "field_comparison": [],
        }
    if resolved["status"] == "not_found":
        return {
            "status": "failed",
            "reason": "No config-assignment entry was found for this candidate's identity.",
            "field_comparison": [],
        }
    flatten_source = resolved["flatten_source"]
    truncated = resolved["truncated"]
    truncation_note = resolved["note"]
    target_fields = _flatten_fields(flatten_source)
    comparisons: list[dict[str, Any]] = []
    mismatches: list[str] = []
    missing: list[str] = []
    for field, expected_value in expected.items():
        if field not in target_fields:
            comparisons.append(
                {
                    "field": field,
                    "expected": expected_value,
                    "actual": None,
                    "status": "unverifiable",
                    "reason": "field was not present in the target read response",
                }
            )
            missing.append(field)
            continue
        actual = target_fields[field]
        matched = _comparable_equal(expected_value, actual)
        comparisons.append(
            {
                "field": field,
                "expected": expected_value,
                "actual": actual,
                "status": "match" if matched else "mismatch",
            }
        )
        if not matched:
            mismatches.append(field)
    if mismatches:
        status = "failed"
        reason = f"Config-assignment fields differed: {sorted(mismatches)}"
    elif missing:
        status = "partially_verified"
        reason = f"Config-assignment fields could not be confirmed: {sorted(missing)}"
    else:
        status = "verified"
        reason = (
            "Config-assignment tuple (scope-id/device-function/profile-type/"
            "profile-instance) matched."
        )
    if truncated and status == "verified":
        status = "partially_verified"
        reason = (
            f"{reason} However, the target config-assignment read is bounded "
            "and additional unseen entries might also match this candidate's "
            f"identity, so uniqueness cannot be guaranteed ({truncation_note})."
        )
    return {"status": status, "reason": reason, "field_comparison": comparisons}


def _flatten_fields(
    value: Any, out: dict[str, Any] | None = None, *, prefix: str = ""
) -> dict[str, Any]:
    """Flatten a nested payload/response into a comparable dict of fields.

    Every scalar leaf is recorded under BOTH its bare (unqualified) key --
    preserving the original, backward-compatible first-seen-wins matching
    used for simple envelope wrappers like `{"items": [{...}]}` or
    `{"config-assignment": [{...}]}` where exactly one element is relevant
    -- AND its fully index-qualified path (e.g. `servers[0].server-name`,
    `servers[1].position`), with deterministic (source-order) indices.

    Finding #3: the qualified paths are what catch a reordered, truncated,
    or extended array that the bare-key form alone would silently mask
    (e.g. a `servers` array missing its second entry would still
    "bare-key match" on the first entry's fields even though a real
    element is missing) -- without regressing any existing bare-key-based
    comparison for object/response envelopes that only ever expose one
    real "item".
    """
    fields = out if out is not None else {}
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = _normalized_key(key)
            qualified = f"{prefix}.{normalized}" if prefix else normalized
            if isinstance(item, (Mapping, list, tuple)):
                _flatten_fields(item, fields, prefix=qualified)
            else:
                fields.setdefault(normalized, item)
                fields.setdefault(qualified, item)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value[:MAX_RESULT_ITEMS]):
            qualified = f"{prefix}[{index}]" if prefix else f"[{index}]"
            if isinstance(item, (Mapping, list, tuple)):
                _flatten_fields(item, fields, prefix=qualified)
            else:
                fields.setdefault(qualified, item)
    return fields


# The only two top-level container keys the verified Classic `full_wlan`
# create/update body (`_base_full_wlan_body`) ever nests fields under. See
# `_field_aliases` -- restricted to exactly these two, real, evidenced
# names so this alias can never accidentally strip a New Central (or any
# other platform's) genuinely-meaningful qualifier.
_CLASSIC_FULL_WLAN_CONTAINER_KEYS = {"wlan", "access_rule"}


_VERIFICATION_IGNORED_KEYS = {
    "dry_run",
    "scope_id",
    "persona",
    "cluster_scope_id",
    "cluster_name",
    "gateway_scope_id",
    "gateway_name",
    # `invocation="endpoint"` Operation.arguments wrapper keys -- never a
    # verifiable target field in their own right. Only relevant when an
    # operation has no `.payload` and we fall back to raw `.arguments`
    # (tool-invocation operations); endpoint operations always have
    # `.payload` populated and never reach this fallback.
    "method",
    "endpoint",
    "data",
}


def _secret_leaf_names(sensitive_fields: Iterable[str]) -> set[str]:
    """Return every bare and dotted-qualified leaf name a
    `sensitive_argument_fields` entry could appear as once flattened by
    `_flatten_fields`.

    e.g. `"wlan.wpa_passphrase"` yields both the bare leaf
    `"wpa_passphrase"` (the common single-scalar-secret case) and the
    fully qualified `"wlan.wpa_passphrase"` (each dot-separated segment
    normalized independently, matching `_flatten_fields`'s own
    qualified-path construction exactly) -- so a nested Classic
    `full_wlan` secret (`sensitive_argument_fields=("wlan.wpa_passphrase",)`)
    is excluded from `expected` no matter which of the two flattened keys
    it is looked up under, not only when it happens to be a bare top-level
    argument (as New Central's flat `passphrase` argument already was).
    """
    names: set[str] = set()
    for field in sensitive_fields:
        segments = [segment for segment in str(field).split(".") if segment]
        if not segments:
            continue
        normalized_segments = [_normalized_key(segment) for segment in segments]
        names.add(normalized_segments[-1])
        names.add(".".join(normalized_segments))
    return names


def _expected_fields(
    action: Any, candidate: Mapping[str, Any]
) -> tuple[dict[str, Any], set[str]]:
    """Return (expected non-secret fields, secret field names) for `action`.

    Sourced from `Operation.payload` when the primary operation is an
    `invocation="endpoint"` write (the exact request body New Central will
    receive) -- never from `method`/`endpoint`/the wrapper `data` argument.
    Tool-invocation operations have no `.payload`; their top-level
    `.arguments` (minus admin/context keys) are used instead.

    Secret exclusion runs at two independent levels, both required: a
    whole top-level container named/declared sensitive (e.g. New Central
    auth-server's `shared-secret-config` object) is dropped before ever
    being flattened, so no nested field of it -- secret-shaped or not,
    e.g. a generic `plaintext-value`/`value` leaf -- can leak; and every
    already-flattened leaf key (bare and qualified) is checked again
    against `Operation.sensitive_argument_fields`
    (`_secret_leaf_names`, handling a nested dotted declaration like
    Classic's `"wlan.wpa_passphrase"`) and `_is_sensitive_key`, so a secret
    nested underneath an otherwise-unremarkable container key (e.g.
    Classic's `wlan.wpa_passphrase`, where `"wlan"` itself is not a
    secret-named key) is still excluded. GET responses omit secret
    material either way, so excluded fields are reported unverifiable
    rather than mismatched.
    """
    read_operation = getattr(action, "read_operation", None)
    # Use the qualified `match_identifier` (the short, unqualified name New
    # Central actually returns, e.g. "ldap1") rather than the raw candidate
    # identifier (e.g. "ldap:ldap1", qualified by auth-server type) --
    # otherwise this synthetic field would never match a real target read
    # even when the true object identity check above already succeeded.
    qualified_identifier = (
        getattr(read_operation, "match_identifier", None)
        if read_operation is not None
        else None
    ) or candidate.get("identifier")
    raw: dict[str, Any] = {"identifier": qualified_identifier}
    secret_fields: set[str] = set()
    secret_leaf_names: set[str] = set()
    if action.operations:
        primary = action.operations[0]
        secret_leaf_names = _secret_leaf_names(primary.sensitive_argument_fields)
        source = primary.payload if primary.payload is not None else primary.arguments
        for key, value in source.items():
            normalized = _normalized_key(key)
            if normalized in _VERIFICATION_IGNORED_KEYS:
                continue
            if value in (None, "", [], {}):
                continue
            if normalized in secret_leaf_names or _is_sensitive_key(normalized):
                secret_fields.add(normalized)
                continue
            raw[normalized] = value
    flattened = _flatten_fields(raw)
    expected: dict[str, Any] = {}
    for key, value in flattened.items():
        if key in secret_leaf_names or _is_sensitive_key(key):
            secret_fields.add(key)
            continue
        # An empty leaf (e.g. Classic full_wlan's default `auth_server1=""`
        # -- "no auth server attached") is not a meaningful assertion: a
        # real GET response is free to omit a field it considers "unset"
        # entirely rather than echo it back as an explicit empty string,
        # and there is nothing to verify about an intentionally-blank
        # value anyway. Filtered here (post-flatten, not only at the
        # top level of `source.items()` above) so this applies uniformly
        # to every nesting depth, not only a top-level argument/payload
        # key.
        if value in (None, "", [], {}):
            continue
        expected[key] = _sanitize(value)
    return expected, secret_fields


def _field_aliases(field: str) -> set[str]:
    aliases = {field}
    if field == "identifier":
        aliases.update({"name", "ssid", "vlan", "vlan_id", "id", "profile_name"})
    if field in {"vlan_name", "ssid_name"}:
        aliases.add("name")
    if field == "auth_server_address":
        aliases.update({"auth_server_address", "address", "host"})
    # Item 3 (Classic flat/nested `full_wlan` equivalence): Classic's
    # create/update payload is nested (`{"wlan": {...}, "access_rule":
    # {...}}`), so `_expected_fields`/`_flatten_fields` produce qualified
    # paths like `wlan.essid`/`wlan.vlan`/`access_rule.name` alongside the
    # bare leaf (`_flatten_fields` always sets both). A Classic `full_wlan`
    # GET is not guaranteed to mirror that nesting -- it may return a flat
    # envelope (bare `essid`/`vlan`/`name`/... with no `wlan`/`access_rule`
    # wrapper at all). Without this alias, the qualified path alone would
    # be reported "unverifiable" against an authoritative flat response
    # even though the bare leaf for the exact same value already matched,
    # incorrectly downgrading "verified" to "partially_verified" solely
    # because of that duplicate bare+qualified expectation.
    #
    # Deliberately narrow and platform-specific, not a generic lossy
    # prefix strip: only the single top-level Classic container segment is
    # aliased away, and never for an indexed array path (`wlan.
    # servers[0]....`) -- `_flatten_fields` deliberately keeps indexed
    # qualification so a reordered/truncated/extended array is still
    # caught; stripping it here would defeat that.
    if "." in field and "[" not in field:
        prefix, _, remainder = field.partition(".")
        if prefix in _CLASSIC_FULL_WLAN_CONTAINER_KEYS and remainder:
            aliases.add(remainder)
    return aliases


def _comparable_equal(expected: Any, actual: Any) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual
    if isinstance(expected, (int, float)) or isinstance(actual, (int, float)):
        return str(expected) == str(actual)
    if isinstance(expected, list):
        return [str(item) for item in expected] == (
            [str(item) for item in actual] if isinstance(actual, list) else [str(actual)]
        )
    return str(expected) == str(actual)
