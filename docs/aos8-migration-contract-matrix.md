# ArubaOS 8 → Central migration contract matrix

**Status: gating document for hpe-networking-mcp 0.5.0.** No parser, schema, or adapter
change may claim broader migration coverage than what this matrix records as
`exact` or a bounded `conditional`. Rows marked `manual` or `unsupported`
remain out of scope for automatic writes until a follow-up revision of this
file records new verified evidence.

This matrix is the authoritative reference for the hpe-networking-mcp 0.5.0
implementation plan's first milestone ("Build the authoritative migration
contract matrix"). It does not
change `src/hpe_networking_mcp/pipeline/aos8_schema.py`, `src/hpe_networking_mcp/pipeline/aos8_parsers.py`,
`src/hpe_networking_mcp/pipeline/aos8_migration.py`, or `src/hpe_networking_mcp/pipeline/aos8_target_adapters.py` — it
records what those files do today, what is provably supported by local
OpenAPI/spec evidence, and exactly what must change before coverage can
broaden.

## 1. How to read this matrix

### 1.1 Classification legend

| Classification | Meaning |
|---|---|
| `exact` | A verified, tested, source-to-target field mapping with a confirmed method/path/schema and no silent loss. |
| `conditional` | A mapping that is schema-expressible and has partial/official evidence, but requires a specific precondition (context, live confirmation, narrower value set) before it can be trusted as lossless. |
| `manual` | No safe automatic object-level write exists; the operator must recreate the object by hand on the target, using the retained source data as a reference. |
| `unsupported` | No target API/tool exists for this AOS8 concept in the audited surface, or the existing code path explicitly rejects the candidate (`AdapterError`). |

### 1.2 Context glossary (New Central)

- **SHARED vs LOCAL** — `object-type=SHARED` objects are library profiles (roles, WLAN SSIDs, VLANs, policies, server groups, auth servers, AAA/dot1x/macauth profiles) that exist independently of any device and must be bound to a scope + device-function through a separate config-assignment. `object-type=LOCAL` objects (network profiles: BGP, OSPF, VRF, VSX, VRRP-global, telemetry, app-bandwidth-contract, config-checkpoint) require `scope-id` and `device-function` directly on the create/update call and need no separate assignment step. This is documented verbatim in `src/hpe_networking_mcp/mcp_servers/config.py:2115-2134` and matches the official "Working with Library Profiles" pattern.
- **scope-id / device-function** — Required identifiers for both LOCAL writes and SHARED config-assignments. `device-function` is a closed enum (`MOBILITY_GW`, `BRANCH_GW`, `VPNC`, `CAMPUS_AP`, `MICROBRANCH_AP`, `ACCESS_SWITCH`, `ALL`, `SERVICE_PERSONA`, `BRIDGE`, `IOT`, `HYBRID_NAC`, `CORE_SWITCH`, `AGG_SWITCH`, `AOSS_ACCESS_SWITCH`, `AOSS_CORE_SWITCH`, `AOSS_AGG_SWITCH`, `EC_VPNC`, `EC_BRANCH_GW`) per `ArubaConfigAssignment_ConfigAssignmentsSchema` in `ingestion/sources/openapi_specs/config-assignment.json`.
- **persona** — The adapter's internal name for `device-function` (`src/hpe_networking_mcp/pipeline/aos8_target_adapters.py:39-51` `TargetContext.persona`); `BaseCentralTargetAdapter.__init__` (`:265-297`) rejects any context missing a resolved scope/persona.
- **gateway / cluster context** — Tunneled (overlay) WLANs require `cluster_name` and `cluster_scope_id` in addition to `scope_id` (`src/hpe_networking_mcp/pipeline/aos8_target_adapters.py:881-895`). No AOS8 candidate object currently carries a target gateway serial; this remains a gap noted per-family below.

### 1.3 Context glossary (Classic Central)

- **group / scope reference** — Classic Central's `full_wlan` object REST is scoped by a URL path segment (`{target}` — a group or device serial), not a JSON body field. The adapter derives this from `context.scope_name` (`src/hpe_networking_mcp/pipeline/aos8_target_adapters.py:1039`, `:1076`), which must be a Classic *group* name, never a New Central `scope_id`.
- **AOS8 AP groups are not Central groups** — An AOS8 `ap_groups` profile is a set of virtual-AP/WLAN bindings inside one controller config, while a Classic Central "group" and a New Central Device Group/site/scope are both device-container objects. There is no automatic 1:1 creation; an operator must select the target Device Group/scope explicitly.

## 2. Authoritative sources and provenance policy

1. **New Central**: `/network-config/v1alpha1` generated operations/specs under `ingestion/sources/openapi_specs/*.json` are authoritative. When a curated `/v1` (or `/v1alpha1`) tool in `src/hpe_networking_mcp/mcp_servers/*.py` diverges from the generated spec's method/path/schema, the generated spec wins and the divergence is called out below as a verification blocker (see §2.1).
2. **Classic Central**: the only verified object REST in this repository is `POST/GET/PUT/DELETE /configuration/full_wlan/{target}/{wlan}` (`src/hpe_networking_mcp/pipeline/aos8_target_adapters.py:1039-1085`), citing `developer.arubanetworks.com/central/reference/apifull_wlancreate_wlan` and `apifull_wlanget_wlan_list`, plus the community `central-python-workflows` `Classic-Central/wlan_config/configurations/open_network.yaml` sample as secondary/reference-only evidence — never a primary contract.
3. **Official HPE developer URLs cited throughout**: `developer.arubanetworks.com/new-central/docs/getting-started-with-rest-apis`, `developer.arubanetworks.com/new-central/docs/introduction-to-configuration-apis`, `developer.arubanetworks.com/new-central-config/reference/config-checkpoint`, `developer.arubanetworks.com/central/reference/apifull_wlancreate_wlan` (see `docs/release-indexes.md:118-125` for the indexed source list).
4. **Community/example repositories** (`central-python-workflows`, any GitHub sample) may be cited as *secondary* corroboration of an official Aruba-maintained example only. They never define a contract by themselves and never justify treating Classic and New Central payloads as interchangeable.

### 2.1 Known curated-tool vs. generated-spec divergence (verification blocker)

`create_config_assignment` now matches the generated
`config-assignment.json` contract: `POST
/network-config/v1alpha1/config-assignments` with a `config-assignment` array
body. `delete_config_assignment` uses the spec-declared instance path
`DELETE /config-assignments/{scope-id}/{device-function}/{profile-type}/{profile-instance}`.
The committed GET schema declares only `scope-id` and `device-function`, while
`list_config_assignments` can also send `profile-type`. A live 0.6 A/B read
confirmed the service honors that filter (`profile-type=roles` returned only
role tuples). Verification still inspects every returned tuple client-side
rather than trusting server-side filtering.

`src/hpe_networking_mcp/mcp_servers/config.py:1250-1292` (`gateway_config_static_route`) uses
`PUT /network-config/v1/static-route/{name}` with a legacy-shaped
`{"network", "nexthop": [{"ip-address", "admin-distance"}]}` payload. The
generated `static-route.json` contract instead declares
`POST/GET/PATCH/DELETE /network-config/v1alpha1/static-route/{name}` and uses
the `ArubaStaticRoute_Ipv4RouteCfg`/`Ipv6RouteCfg` field families. These two
surfaces are not treated as interchangeable. The generic
`get_network_profile` helper exposes `static-route` and `vrrp-interface` for
bounded read-only evidence, while `set_network_profile` and
`delete_network_profile` intentionally reject both types until live target
reads reconcile the route versions and prove VRRP VLAN/interface attachment.

## 3. Audited source-model findings — actionable prerequisites

These were confirmed defects/gaps in the parser and redaction logic. Items 1, 4, 5,
and 6 are **RESOLVED** by the `aos8-source-enrichment` todo (parser/migration-level
fixes only, verified by new regression tests in `tests/unit/test_aos8_parsers.py`
and `tests/unit/test_aos8_migration.py`). Item 7 is **RESOLVED** by the
`aos8-source-review-fixes` todo (a follow-up code-review pass covering the
migration and orchestrator layers, verified by new regression tests in
`tests/unit/test_aos8_migration.py` and `tests/unit/test_aos8_migration_orchestrator.py`).
Item 2 is **partially resolved**: a
bounded, fail-closed source-side security-intent signal now exists, and a New
Central adapter mapping for `OPEN`/`WPA2_PERSONAL`/`WPA3_SAE`/`ENHANCED_OPEN` now
exists in `src/hpe_networking_mcp/pipeline/aos8_target_adapters.py` (added after this section was first
written, by the `aos8-verification` todo). The `aos8-live-dryrun-eval` todo
(2026-07-25, `docs/aos8-live-dryrun-evaluation.md`) confirmed this mapping live at
the `preview()`/preflight-read level against a configured New Central tenant, with
correct enum rendering and secret masking. §6.2's classifications are **unchanged**
by this — WPA2 Personal/WPA3-SAE/Enhanced Open remain `conditional`, not `exact`,
until a live `apply()` + secret read-back is performed (out of scope for a
read-only evaluation). Item 3 is unchanged by design (see below).
Item 8 is **RESOLVED** by the `aos8-companion-repo-fixes` todo (a read-only audit
of a third-party same-owner migration tool that surfaced one classifier bug fixed
here, plus corroborating secondary evidence for design decisions already made in
items 6/7 — no code was copied; the referenced repository is unlicensed).

1. **RESOLVED — `mac_server_group` alias miss.** `parse_aaa_profiles`
   (`src/hpe_networking_mcp/pipeline/aos8_parsers.py`) now aliases the literal key `mac_server_group` in
   addition to `mba_server_group`/`mac-server-group`. Regression:
   `test_parse_aaa_profiles_recognizes_literal_mac_server_group_alias`
   (`tests/unit/test_aos8_parsers.py`).
2. **PARTIALLY RESOLVED — WLAN security normalization.** `parse_wlans`
   (`src/hpe_networking_mcp/pipeline/aos8_parsers.py`) now also extracts three evidenced AOS8 `ssid_prof`
   signals (`src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/aos8.json`
   `aos8_post_object_ssid_prof` request-body properties): the `wpa3_transition`
   boolean flag and *presence-only* booleans for `wpa_passphrase`/`wpa_hexkey`
   (`AOS8Wlan.wpa3_transition`/`.passphrase_present`/`.psk_hexkey_present` — never
   the secret value itself). `build_migration_plan`
   (`src/hpe_networking_mcp/pipeline/aos8_migration.py` `_wlan_security_intent`) combines these with the
   raw `opmode` string and a cross-reference against the WLAN's attached
   `aaa_profile` (dot1x/MAC-auth chain) to emit a bounded
   `payload["security"]` structure on every WLAN candidate with
   `mode ∈ {open, wpa2_personal, wpa3_sae, wpa3_transition_personal,
   enhanced_open, enterprise_dot1x, mac_auth_only, mac_auth_psk, unknown}` plus
   an explicit `ambiguous` flag. Any combination not covered by this evidence
   (e.g. WEP, an unresolved `aaa_profile` reference, or a bare WPA2/WPA3
   opmode string with no PSK/passphrase signal) is reported as `unknown` with
   an explicit warning — never guessed or defaulted. **No AOS8-side source
   opmode enum has official documented evidence in this repository**, so this
   remains keyword/field-presence-based classification, not a verified
   1:1 enum mapping; the original raw `opmode` is always preserved unchanged
   alongside the normalized `mode`. Regression tests:
   `test_wlan_security_intent_classifies_*` and
   `test_wlan_security_intent_reports_unknown_*`
   (`tests/unit/test_aos8_migration.py`). A New Central adapter mapping
   now exists for `open`/`wpa2_personal`/`wpa3_sae`/`enhanced_open`
   (`NewCentralAdapter._map_wlan`, `src/hpe_networking_mcp/pipeline/aos8_target_adapters.py`,
   added by the `aos8-verification` todo after this item was first
   written, and confirmed live at the `preview()`/preflight-read level
   by the `aos8-live-dryrun-eval` todo, 2026-07-25 — see
   `docs/aos8-live-dryrun-evaluation.md`); MAC-auth and enterprise 802.1X
   (the AAA-profile-attach gap) remain `unsupported` on every target,
   and no target has a live-confirmed apply + secret read-back for any
   secured mode yet — see §6.2 for the full per-mode classification,
   which this item does not change.
3. **Auth profiles/servers/server-groups/routes/VRRP retain undecoded detail in `settings`/raw.** `AOS8AuthProfile.settings`, `AOS8ServerGroup.settings`, `AOS8AuthServer.settings`, `AOS8Route.settings`, `AOS8VRRP.settings` (`src/hpe_networking_mcp/pipeline/aos8_schema.py`) hold every field not explicitly named in the dataclass. These are propagated as `unsupported_fields` on every candidate (`src/hpe_networking_mcp/pipeline/aos8_migration.py` `_append_for_both` calls throughout `build_migration_plan`), each with a mandatory unmapped-field warning (`_unsupported_warnings`). Any adapter that tries to apply these candidates today (`NewCentralAdapter._reject_unmapped`) will raise `AdapterError` unless the field is in a narrow `allowed` allow-list per object type — this is intentional fail-closed behavior, not a bug, and is unchanged by this revision.
4. **RESOLVED — server-group dependency resolution is now type-aware.**
   `build_migration_plan` (`src/hpe_networking_mcp/pipeline/aos8_migration.py`) now keys
   `server_ids_by_name` by `dict[str, dict[server_type, identifier]]` instead of
   `dict[str, list[identifier]]`. A server-group's per-entry auth-server
   reference resolves to a dependency only when exactly one server type
   matches that name; if a RADIUS/LDAP/TACACS name collision exists in the
   export (AOS8 stores them in separate `radius_servers`/`ldap_servers`/
   `tacacs_servers` sections, so this is possible), the candidate emits a
   `unsupported_fields["auth_server_type_collisions"]` entry and an explicit
   fail-closed warning instead of silently selecting one candidate — the
   dependency is left unresolved. Deterministic ID/order guarantees are
   preserved (`build_migration_plan` sorts every dependency list and the
   overall plan is still fully reproducible across runs). Regression tests:
   `test_server_group_dependency_resolution_is_type_aware_across_radius_and_ldap`,
   `test_server_group_dependency_collision_fails_closed_with_warning`,
   `test_server_group_dependencies_remain_deterministic_across_runs`
   (`tests/unit/test_aos8_migration.py`).
5. **RESOLVED — AP-group VAP and role-policy dependencies now warn explicitly.**
   The AP-group loop and the role loop in `build_migration_plan`
   (`src/hpe_networking_mcp/pipeline/aos8_migration.py`) each now emit a specific, per-reference
   warning the moment a virtual-AP→WLAN or role→policy reference cannot be
   resolved against the export (`ap_group:<name>: virtual AP '<vap>' does not
   match any parsed WLAN profile...`; `role:<name>: referenced policy
   '<acl>' was not present in the export...`), in addition to the existing
   generic end-of-plan dependency-not-present check. No target object is
   invented in either case — the dependency remains unresolved and the
   candidate stays unapplied. Regression tests:
   `test_ap_group_warns_explicitly_on_unresolved_vap_to_wlan_dependency`,
   `test_role_warns_explicitly_on_missing_policy_dependency`
   (`tests/unit/test_aos8_migration.py`).
6. **RESOLVED — `ldap_admindn` is no longer over-redacted.**
   `src/hpe_networking_mcp/pipeline/aos8_migration.py` `_SENSITIVE_EXACT_KEYS` no longer lists
   `ldap_admindn`/`ldap_admin_dn`; `_redact_sensitive_values` now leaves the
   LDAP bind/admin distinguished name (e.g. `cn=admin,dc=example,dc=com`)
   visible in the candidate payload/`unsupported_fields`, while the
   accompanying bind **password** (`ldap_adminpasswd`/`ldap_adminpwd`, still
   listed) remains redacted, transient, and flagged
   `requires_secret_input`. The same AOS8 `ssid_prof` PSK/WEP key-material
   fields (`wpa_hexkey`, `wepkey1`-`wepkey4`) were added to the secret list as
   a related defensive fix so WLAN key material is never persisted either.
   Regression tests: `test_ldap_admin_dn_stays_visible_while_bind_password_is_redacted`,
   `test_build_migration_plan_never_serializes_auth_secrets`,
   `test_sensitive_key_detection_covers_credentials_without_false_positives`
   (`tests/unit/test_aos8_migration.py`).
7. **RESOLVED — flattened path-like keys evaded the item-6 secret list, and
   WPA2-personal classification was over-eager.** A follow-up code-review
   pass found that `_wlan_payload`'s `unsupported_fields` flattening (e.g.
   `f"ssid_profile.{key}"` for every unmapped `ssid_prof`/`virtual_ap` field)
   produced path-like keys such as `ssid_profile.wpa_hexkey` /
   `ssid_profile.wepkey1`-`wepkey4` that no longer matched
   `_SENSITIVE_EXACT_KEYS`/the prefix+suffix checks once normalized, because
   normalization collapses the non-secret path prefix and the secret leaf
   token into one indistinguishable underscore-joined string — an
   unredacted-secret-leak regression on top of item 6's fix.
   `src/hpe_networking_mcp/pipeline/aos8_migration.py` `_is_sensitive_key` now also evaluates the
   final `.`/`/`-separated path component alone against the same rules, so a
   non-secret prefix can no longer dilute a secret leaf out of the check.
   Separately, `_wlan_security_intent`'s final `wpa2_personal` branch
   classified *any* passphrase/PSK-hexkey presence or a bare `"psk"` token in
   `opmode` as verified WPA2-personal, which misclassified legacy WPA1/TKIP
   opmodes (e.g. `wpa-psk-tkip`, `wpa-tkip`) or any other unrecognized
   personal mode as WPA2; it now additionally requires the opmode to
   explicitly contain `wpa2`, falling through to `mode="unknown"` with a
   warning otherwise (see §6.2's WPA2 Personal row). Finally, the
   orchestrator's `_safe_candidate`/`_sanitize` redaction in
   `src/hpe_networking_mcp/pipeline/aos8_migration_orchestrator.py` was masking the non-secret
   `passphrase_present`/`psk_hexkey_present` presence booleans to the literal
   string `"******"` in previews and persisted migration-run state; a narrow,
   type-checked allowlist (`_is_presence_metadata` — exact key name *and* an
   actual `bool` value) now preserves them as real booleans while still
   redacting any actual secret value. Regression tests:
   `test_sensitive_key_detection_evaluates_flattened_path_like_keys_by_leaf`,
   `test_wlan_secret_material_never_appears_in_plan_json`,
   `test_wlan_security_intent_does_not_classify_legacy_wpa_tkip_psk_as_wpa2`,
   `test_wlan_security_intent_does_not_classify_wpa_tkip_with_passphrase_present_as_wpa2`,
   `test_wlan_security_intent_does_not_classify_unrecognized_psk_opmode_as_wpa2`
   (`tests/unit/test_aos8_migration.py`);
   `test_safe_candidate_redaction_preserves_presence_booleans_and_redacts_secrets`,
   `test_safe_candidate_redaction_only_bypasses_boolean_presence_values`
   (`tests/unit/test_aos8_migration_orchestrator.py`).

8. **RESOLVED — role-only AAA profiles could block WLAN security
   classification; plus corroborating secondary evidence for two prior
   design decisions.** A read-only audit of
   [`secure-ssid/aos8-migration-tool`](https://github.com/secure-ssid/aos8-migration-tool)
   pinned at commit
   [`7bfa884`](https://github.com/secure-ssid/aos8-migration-tool/tree/7bfa884d8e8f1c7e97a7bfa42f15596aa42fcf79)
   — a third-party, same-owner AOS8 migration tool with **no LICENSE file**,
   so it is cited here as secondary provenance/corroboration only; no code
   from it was copied or adapted, and it is never an authoritative API
   contract — surfaced the following:
   - **Classifier bug (fixed here).** `_wlan_security_intent`
     (`src/hpe_networking_mcp/pipeline/aos8_migration.py`) previously treated *any* resolved
     `aaa_profile` reference as blocking further classification, even when
     that profile configured neither a `dot1x_auth_profile` nor a
     `mac_auth_profile` (i.e. it was **role-only** — used only for
     post-auth role assignment, not authentication). This produced a false
     `mode="unknown"` for an otherwise verifiable WPA2-PSK WLAN. The
     companion repo's own parser regression tests fixture pairs a
     `wlan virtual-ap "guest-vap"` (opmode `wpa2-psk-aes` +
     `wpa-passphrase`) with `aaa profile "guest-aaa"` that sets only
     `initial-role "guest-logon"` — a real-world instance of the same
     role-only-AAA-on-a-personal-WLAN shape:
     https://github.com/secure-ssid/aos8-migration-tool/blob/7bfa884d8e8f1c7e97a7bfa42f15596aa42fcf79/tests/test_aos8_parser.py#L15-L34
     (secondary, same-owner prior art — not an authoritative API contract).
     `_wlan_security_intent` now only promotes to `enterprise_dot1x`/
     `mac_auth_only`/`mac_auth_psk` when the resolved `aaa_profile` carries
     an explicit `dot1x_auth_profile` or `mac_auth_profile` reference; a
     role-only profile falls through to the existing verified
     opmode/passphrase classification instead of stopping early. An
     aaa_profile reference that fails to resolve in the export at all (or
     one that ambiguously configures both a dot1x and a MAC-auth profile)
     still correctly stays `unknown` — only the role-only case changed.
     Regression tests:
     `test_wlan_security_intent_role_only_aaa_profile_falls_through_to_opmode_classification`,
     `test_wlan_security_intent_stays_unknown_when_aaa_profile_has_both_dot1x_and_mac_auth`
     (`tests/unit/test_aos8_migration.py`).
     **Re-review follow-up (fixed here).** The role-only fall-through above
     was itself too permissive: a resolved `aaa_profile` that sets no
     `dot1x_auth_profile`/`mac_auth_profile` but *does* configure a
     `dot1x_server_group`, `mac_server_group`, or `accounting_server_group`
     (`src/hpe_networking_mcp/pipeline/aos8_schema.py` `AOS8AAAProfile`) still carries external
     server-group authentication intent that cannot be safely verified from
     opmode/passphrase alone — e.g. an `opensystem` WLAN with only a
     `dot1x_server_group` configured on its `aaa_profile` was previously
     falling through and being classified as unambiguous `open`.
     `_wlan_security_intent` now only falls through to opmode/passphrase
     classification when the resolved `aaa_profile` configures **none** of
     `dot1x_auth_profile`, `mac_auth_profile`, `dot1x_server_group`,
     `mac_server_group`, or `accounting_server_group`; otherwise it stays
     `mode="unknown"`, `ambiguous=True`, with an explicit fail-closed
     warning naming the configured server-group field(s). The originally
     verified role-only (`initial-role`-only) + WPA2-PSK fallback is
     unchanged. Regression tests:
     `test_wlan_security_intent_stays_unknown_for_dot1x_server_group_without_auth_profile`,
     `test_wlan_security_intent_stays_unknown_for_mac_server_group_without_auth_profile`,
     `test_wlan_security_intent_stays_unknown_for_accounting_server_group_without_auth_profile`
     (`tests/unit/test_aos8_migration.py`).
   - **Defensive boolean/flag normalization (hardened here).** The same
     repo's client documents AOS8 fields arriving in loosely-typed shapes —
     flag dicts, and values "double-wrapped as `{key: {key: val}}`"
     (secondary, same-owner prior art, not an authoritative API contract):
     https://github.com/secure-ssid/aos8-migration-tool/blob/7bfa884d8e8f1c7e97a7bfa42f15596aa42fcf79/docs/API-NOTES.md#L57-L64.
     `_wlan_security_signals`'s `wpa3_transition` extraction
     (`src/hpe_networking_mcp/pipeline/aos8_parsers.py`) previously did a naive `bool(raw_value)`,
     which would silently misclassify an ambiguous shape (notably: an
     *empty* dict, which is falsy in Python, was reported as a confident
     `False` rather than "unverifiable"). A new `_normalize_optional_bool`
     helper accepts only actual booleans, integer `0`/`1`, a narrow set of
     explicit true/false-ish strings, and recursively unwraps single-key
     wrapper dicts (bounded to 4 levels); any other shape (multi-key dict,
     empty dict, list, or unrecognized string) now returns `None` rather
     than guessing. Regression tests: `test_normalize_optional_bool_*`,
     `test_parse_wlans_wpa3_transition_accepts_documented_flag_variants`
     (`tests/unit/test_aos8_parsers.py`).
   - **Source alias coverage (audited and extended here).** The same
     client tries multiple build-dependent object/field names for the same
     AOS8 concept — `ssid_prof`/`ssid-profile`/`ssid-prof` for the
     SSID-profile reference, and the legacy `wlan_virtual_ap` object name
     alongside the canonical `virtual_ap` (secondary, same-owner prior art,
     not an authoritative API contract):
     https://github.com/secure-ssid/aos8-migration-tool/blob/7bfa884d8e8f1c7e97a7bfa42f15596aa42fcf79/lib/aos8_client.py#L315-L399.
     `parse_wlans` (`src/hpe_networking_mcp/pipeline/aos8_parsers.py`) already recognized
     `ssid-profile`/`ssid_prof`; the missing all-hyphen `ssid-prof` variant
     was added (and to `_wlan_payload`'s consumed-field set in
     `src/hpe_networking_mcp/pipeline/aos8_migration.py`, so it is not double-reported as an
     unmapped field). `aos8_list_virtual_aps`
     (`src/hpe_networking_mcp/mcp_servers/aos8.py`) now falls back to the legacy
     `wlan_virtual_ap` object name when the canonical `virtual_ap` object
     read fails, mirroring the same tolerant-of-either-name behavior — a
     failure on both names is still reported exactly as a single failed
     lookup always has been (no new silent-success path). The `ap_group`
     virtual-AP binding field itself already accepted both `virtual-ap` and
     `virtual_ap`; no changes were needed there. Regression tests:
     `test_parse_wlans_recognizes_ssid_prof_hyphenated_alias`
     (`tests/unit/test_aos8_parsers.py`);
     `test_aos8_list_virtual_aps_falls_back_to_legacy_wlan_virtual_ap_object`,
     `test_aos8_export_wlans_still_warns_when_both_virtual_ap_object_names_fail`
     (`tests/unit/test_aos8_export_and_migration_tool.py`).
     **Re-review follow-up (fixed here).** The parser-level alias fix above
     was incomplete: `_VIRTUAL_AP_FIELDS` (`src/hpe_networking_mcp/mcp_servers/aos8.py`), the
     bounded field allow-list used by `_compact_primary_list` to compact
     live `aos8_list_virtual_aps`/`aos8_export_wlans` reads, still only
     listed `ssid-profile`/`ssid_prof` and omitted the all-hyphen
     `ssid-prof` alias. A live virtual-AP record shaped like
     `{profile-name, ssid-prof, vlan}` had its `ssid-prof` field stripped by
     bounded compaction before `parse_wlans` ever saw it, so the WLAN could
     never be joined to its SSID profile even though the parser itself
     supported the alias. `ssid-prof` was added to `_VIRTUAL_AP_FIELDS`.
     Regression tests:
     `test_aos8_list_virtual_aps_retains_ssid_prof_hyphenated_alias`,
     `test_aos8_export_wlans_links_ssid_prof_alias_vap_to_one_wlan`
     (`tests/unit/test_aos8_export_and_migration_tool.py`).
   - **Secondary corroboration for item 7's Classic read-back stance
     (no code change — informational only).** The same repo's Classic
     Central client documents that its own group-create call "is a known
     flaw [that] lets the v3 create return success **without applying**",
     requiring an explicit `GET /configuration/v1/groups/properties`
     read-back to confirm the setting actually took (secondary, same-owner
     prior art, not an authoritative API contract):
     https://github.com/secure-ssid/aos8-migration-tool/blob/7bfa884d8e8f1c7e97a7bfa42f15596aa42fcf79/docs/API-NOTES.md#L112-L118.
     This corroborates (but does not itself establish) the general
     Classic-Central caution already reflected in this matrix's §2/§5
     read-back-before-trust posture; hpe-networking-mcp's own Classic adapter has
     no group-create call to change, so no code changed for this item.
   - **Secondary corroboration for item 6/7's stricter no-PSK-reuse rule
     (no code change — informational only).** The same repo's own New
     Central client includes a `secret_looks_unusable()` guard and a
     Classic-side hashed-PSK-to-placeholder substitution specifically
     because AOS8-exported PSK/secret values are sometimes unusable as-is
     (empty, longer than a real WPA passphrase can be, or a hex-encoded
     hash) (secondary, same-owner prior art, not an authoritative API
     contract):
     https://github.com/secure-ssid/aos8-migration-tool/blob/7bfa884d8e8f1c7e97a7bfa42f15596aa42fcf79/lib/central_client.py#L40-L51,
     https://github.com/secure-ssid/aos8-migration-tool/blob/7bfa884d8e8f1c7e97a7bfa42f15596aa42fcf79/tests/test_clients_http.py#L336-L354.
     This corroborates hpe-networking-mcp's own, stricter rule (item 6/7 above):
     rather than substituting a placeholder, hpe-networking-mcp never persists or
     reuses a source PSK/hexkey value at all — only a presence boolean
     ever leaves the parser/migration layer, and the operator must always
     re-enter the real credential on the target.

## 4. New Central audited conclusions (encoded contract)

- **Authoritative base**: `/network-config/v1alpha1` (see server blocks in every spec cited in §5 — `wlan.json`, `auth-server.json`, `auth-server-group.json`, `aaa-profile.json`, `aaa-dot1xauth.json`, `aaa-macauth.json`, `role.json`, `role-acl.json`, `static-route.json`, `l3-route.json`, `vrrp.json`, `vrrp-interface.json`, `policy.json`, `config-assignment.json`, `cda-auth-profile.json`, `cda-authz-policy.json` all declare `"servers": [{"url": "/network-config/v1alpha1"}]`).
- **SHARED/LOCAL + config-assignment verification is mandatory** — see §1.2/§1.3 and the divergence in §2.1. LOCAL objects require `scope-id`+`device-function` on the write call itself; SHARED objects require a follow-up config-assignment (`src/hpe_networking_mcp/mcp_servers/config.py:2100-2134`).
- **Secured WLAN schema** (`ingestion/sources/openapi_specs/wlan.json`, `ArubaWlanSecurity_WlanSecurityConfig.opmode`) supports the closed enum `OPEN, WPA2_PERSONAL, WPA2_ENTERPRISE, ENHANCED_OPEN, WPA3_SAE, WPA3_ENTERPRISE_CCM_128, WPA3_ENTERPRISE_GCM_256, WPA3_ENTERPRISE_CNSA, WPA_ENTERPRISE, WPA_PERSONAL, WPA2_MPSK_AES, WPA2_MPSK_LOCAL, DPP, WPA2_PSK_AES_DPP, WPA2_AES_DPP, WPA3_SAE_DPP, WPA3_AES_CCM_128_DPP, WPA3_AES_GCM_256_DPP, BOTH_WPA_WPA2_PSK, BOTH_WPA_WPA2_DOT1X, STATIC_WEP, DYNAMIC_WEP, WPA3_MPSK_SAE`. There is **no** `WPA2_PSK` value — any code sending `WPA2_PSK` directly in the API payload (rather than `WPA2_PERSONAL`) is stale/incorrect. `WPA2_PSK` is kept as a **caller-facing deprecated alias only** (0.4.0's published `--opmode WPA2_PSK` CLI value): `hpe_networking_mcp.pipeline.create_ssid._normalize_opmode` and `run_ssid.py`'s `--opmode` choices still accept it, log/print a deprecation warning, and normalize it to `WPA2_PERSONAL` before the value ever reaches `_build_ssid_body`'s payload/security branching — the payload `opmode` field itself is always the authoritative `WPA2_PERSONAL`, never the alias. WPA3 personal transition is represented as the boolean `wpa3-transition-mode-enable` inside `ArubaWlanSecurity_WirelessSecurityAdvancedConfig` (`ingestion/sources/openapi_specs/wlan.json:5578-5584` and `:5714-5720`), **not** as a distinct `opmode` value — this needs live validation against a real WPA3-Personal-transition SSID before being classified `exact`.
- **Auth server** (`ingestion/sources/openapi_specs/auth-server.json`, `ArubaAuthServer_AuthServersauthServerSchema.type`) supports the enum `RADIUS, LDAP, TACACS, WINDOWS, RFC3576, XMLAPI, RADSEC, LOCAL`, with `x-supportedDeviceType` restricted per platform (AP: RADIUS/LDAP/TACACS/XMLAPI; CX: RADIUS/TACACS; PVOS: RADIUS/TACACS; GW: RADIUS/TACACS/WINDOWS/LDAP). RadSec (`RADSEC`) representation needs care — it was not exercised anywhere in `src/hpe_networking_mcp/pipeline/aos8_target_adapters.py` (only the RADIUS path is implemented, `:728-780`) and needs its own schema-field audit before use. Server groups (`auth-server-group.json`) have an **ordered** `servers` array (`server-name` + `position`, `ingestion/sources/openapi_specs/auth-server-group.json`), which the current `AOS8ServerGroup.auth_servers`/`auth_server_entries` fields (`src/hpe_networking_mcp/pipeline/aos8_schema.py`) do not yet guarantee order-preserving mapping for.
- **Device AAA/dot1x/macauth profiles are Gateway/Switch concepts** — `ArubaAaaProfile_AaaProfileSchemaGet`, `ArubaAaaDot1xauth_Dot1xauthSchemaGet`, `ArubaAaaMacauth_MacauthSchemaGet` are all `x-supportedDeviceType: [Gateway, Switch CX, Switch PVOS]` only (no `Access Point`). AP WLAN authentication is configured directly on the WLAN/SSID resource (`ArubaWlanSecurity_AuthServerConfig` embedded in `wlan.json`), not through a device AAA profile. **Central NAC auth profiles** (`ingestion/sources/openapi_specs/cda-auth-profile.json`, `x-tag-group: "Central NAC"`) are a distinct, explicit alternative surface for MAC/MPSK/wired/EAP authentication policy and must never be produced as an automatic LDAP/TACACS conversion of an AOS8 `mac_auth_profile`/`dot1x_auth_profile`.
- **Role ACL is CX-only** — `ArubaRoleAcl_RoleAclsSchemaGet.x-supportedDeviceType == ["Switch CX"]` (`ingestion/sources/openapi_specs/role-acl.json`). Gateway security policies (`policy.json`, used by the existing `delete_gw_policy` tool at `/network-config/v1alpha1/policies/{name}`, `src/hpe_networking_mcp/mcp_servers/config.py:2110-2121`) are a **distinct** object family from CX role-ACLs. `NewCentralAdapter._map_role` (`src/hpe_networking_mcp/pipeline/aos8_target_adapters.py:647-726`) currently only accepts a normalized ACL value of `allowall`/`sys_allow_all` and raises `AdapterError` for anything else (`:661-665`) — custom AOS8 ACLs/policies must never be silently reduced to allow-all; they remain `manual` until a verified custom-policy write path exists.
- **AOS8 AP groups require operator-selected Device Group/scope and profile assignments** — there is no automatic one-to-one creation of a New Central Device Group from an AOS8 `ap_groups` profile. `NewCentralAdapter` has no `_map_ap_group` method at all today, so any `ap_group` candidate falls through `_map_candidate`'s `getattr(..., None)` branch (`:574-580`) to `unsupported`.
- **Gateway IPv4 static-route destination contract is not verified.** `ArubaStaticRoute_Ipv4RouteCfg` (`ingestion/sources/openapi_specs/static-route.json`) keys routes by a composite `prefix-vrf-nexthop-id` string and exposes a `forwarding-type` enum (`NEXTHOP, INTERFACE, NULLROUTE, REJECT, VLAN, TUNNEL, IPSECMAP, CLUSTER`) that is `x-supportedDeviceType: ["Switch CX"]`-only for that specific field, while `next-hop` itself spans AP/Gateway/Switch. A live 0.6 bounded `MOBILITY_GW` read reached `/network-config/v1alpha1/static-route`, but the returned profile exposed only `name` plus `default-gateway[]` (`dg-name`, `forwarding-type`, `ipv4-address`, `metric`) and no general destination/prefix route shape. This confirms endpoint availability and a default-route representation, not the write contract for arbitrary AOS8 routes. IPv6 (`ArubaStaticRoute_Ipv6RouteCfg`) remains conditional. **`l3-route.json` (`/l3-route`) is Switch-CX-only** (`ArubaL3Route_L3RouteSchemaGet.x-supportedDeviceType == ["Switch CX"]`) and is a separate, non-Gateway payload family — never substitute it for `static-route.json` on a Gateway/AP persona.
- **VRRP/VRRPv6/tracking remain conditional/unsupported** until VLAN-interface attachment and tracking normalization are proven. `vrrp.json` (`/vrrp-global`, used by the existing `build_*` LOCAL network-profile helper, `src/hpe_networking_mcp/mcp_servers/config.py` `_NETWORK_PROFILE_TYPES["vrrp"] = "vrrp-global"`) and `vrrp-interface.json` (`/vrrp`, Gateway-only per `ArubaVrrpInterface_VrrpprofileSchema`) are two **different** resources; the interface-level profile keys `virtual-router` entries by a composite `router-id-address-family` string, not directly by VLAN ID, and tracking is a nested `ArubaVrrpInterface_VrrpTrackingConfiguration` block that has not been mapped from `AOS8VRRP.tracking` (`src/hpe_networking_mcp/pipeline/aos8_schema.py`). A live 0.6 `MOBILITY_GW` GET confirmed `/vrrp` is reachable but returned an empty collection, so no live attachment or tracking shape is available yet.

## 5. Classic Central audited conclusions (encoded contract)

- **Verified object REST**: `POST/GET/PUT/DELETE /configuration/full_wlan/{target}/{wlan}` with body `{"wlan": {...}, "access_rule": {...}}` (`src/hpe_networking_mcp/pipeline/aos8_target_adapters.py:1039-1085`), citing `developer.arubanetworks.com/central/reference/apifull_wlancreate_wlan` / `apifull_wlanget_wlan_list`. There is a **separate, documented v2 WLAN contract** for the documented WPA2-Personal case that has not been reconciled with the `full_wlan` shape used here — treat v1 `full_wlan` and any v2 WLAN endpoint as non-interchangeable until both are read live against the same target group.
- **Official samples support WPA3 Personal and Enterprise**, but transition mode and MAC-auth are ambiguous in the audited sources and must remain `manual`/`unsupported` without live goldens confirming the exact request/response shape and dependency order. **The enterprise dependency lifecycle (auth server → server group → AAA profile/role → WLAN) has one narrower, evidenced exception**: `ClassicCentralAdapter._wpa3_enterprise_wlan_action` maps WPA3-Enterprise to `full_wlan` citing an official sample, but only when the caller supplies an already-existing Classic auth-server name via `external_object_references` (§1.2/§1.3, §6.2); every action it returns is `dry_run_only=True` and can never reach `applied`/`skipped`. WPA2-Enterprise and the rest of the dependency chain (standalone server-group/AAA-profile/role object REST) remain `manual`/`unsupported` — see the next bullet.
- **No standalone object REST exists in the audited Classic API surface** for AAA profiles, server groups, roles, gateway/role policies, static/VRRP routes, LDAP, or TACACS. `ClassicCentralAdapter._map_candidate` (`src/hpe_networking_mcp/pipeline/aos8_target_adapters.py:938-956`) explicitly rejects every `object_type != "wlan"` with `compatibility_errors=["Classic Central '<type>' target operation is not verified in this repository; candidate remains unapplied"]`. AP CLI (`aos8_write`) and MobilityController templates are **whole-config/manual fallbacks only** — never treat a CLI/template blob as a safe, idempotent, per-object automatic write.
- **Central groups/device moves are not AOS8 AP groups.** The Classic `full_wlan` `{target}` path segment must be an explicit Classic group name resolved by the operator/context, never derived automatically from an AOS8 `ap_groups` profile name.

## 6. Contract matrix by family

Apply order (`src/hpe_networking_mcp/pipeline/aos8_migration.py:33-46`, `APPLY_ORDER`) is shared between Classic and New Central candidate lists and drives every dependency-aware topological sort (`_topological_candidates`, `src/hpe_networking_mcp/pipeline/aos8_target_adapters.py:205-264`): `vlan`/`auth_server`=10, `dot1x_auth_profile`/`mac_auth_profile`/`server_group`/`policy`=20, `role`=30, `aaa_profile`=40, `wlan`=50, `ap_group`=60, `route`=70, `vrrp`=80, `controller`=90.

### 6.1 VLANs (foundation dependency for every other family)

| | Classic Central | New Central |
|---|---|---|
| **Source fields** | `AOS8Vlan.vlan_id`, `.description`, remaining raw fields (`src/hpe_networking_mcp/pipeline/aos8_schema.py`) | same |
| **Candidate payload** | `{"vlan_id", "description"}` (`src/hpe_networking_mcp/pipeline/aos8_migration.py:544-565`) | same |
| **Target method/path** | Not implemented in `ClassicCentralAdapter` (falls to generic "not verified" rejection, `:938-948`) | `create_vlan` tool → `POST/PUT /network-config/v1/layer2-vlan/{vlan_id}` plus scope-map (`src/hpe_networking_mcp/pipeline/aos8_target_adapters.py:609-645`) |
| **Context** | Classic group (unresolved) | `scope_id`, `persona` |
| **Preflight/read-back** | none | `GET /network-config/v1/layer2-vlan/{vlan_id}` (`:637-645`) |
| **Update** | n/a | same `create_vlan` operation reused as update (`:635`) |
| **Secrets** | none | none |
| **Classification** | `unsupported` | `exact` (implemented, tested per `tests/unit/test_aos8_target_adapters.py:150-190`) |

### 6.2 Secured WLANs

`parse_wlans`/`build_migration_plan` now derive a bounded, fail-closed source
`payload["security"]` intent summary for every WLAN candidate (§3 item 2 —
partially resolved): the original raw `opmode` string is always preserved,
plus a normalized `mode` in
`{open, wpa2_personal, wpa3_sae, wpa3_transition_personal, enhanced_open,
enterprise_dot1x, mac_auth_only, mac_auth_psk, unknown}` derived only from
evidenced AOS8 `ssid_prof` fields (`opmode`, `wpa3_transition`, and
presence-only `wpa_passphrase`/`wpa_hexkey` booleans) plus a cross-reference
against the attached `aaa_profile`'s dot1x/MAC-auth chain. Any unverifiable
combination reports `mode="unknown"` with an explicit warning rather than a
guess. **A New Central adapter mapping now exists** for `OPEN`/`WPA2_PERSONAL`/
`WPA3_SAE`/`ENHANCED_OPEN` (`NewCentralAdapter._map_wlan`,
`src/hpe_networking_mcp/pipeline/aos8_target_adapters.py`, added by the `aos8-verification` todo after
this section was first written, and confirmed live at the `preview()`/
preflight-read level by the `aos8-live-dryrun-eval` todo, 2026-07-25 — see
`docs/aos8-live-dryrun-evaluation.md`). **Classic Central now also has verified
mappings for `open` and `wpa3_sae`** (`ClassicCentralAdapter._open_wlan_action`/
`._wpa3_personal_wlan_action`, citing the official `apifull_wlancreate_wlan`
sample), plus a narrower **conditional, dry-run-only** WPA3-Enterprise mapping
(`._wpa3_enterprise_wlan_action`) that requires an already-existing Classic
auth-server name supplied via `aos8_preview_migration_run`'s
`external_object_references` and can never reach `applied`/`skipped`. Classic
WPA2-Personal and Enhanced Open still have no verified payload and remain
`manual`. **No target has a live-confirmed apply + secret read-back for any
secured mode yet** — every implemented secured mapping (New Central
`wpa2_personal`/`wpa3_sae`/`enhanced_open`; Classic `wpa3_sae`/WPA3-Enterprise)
remains `conditional` (not `exact`) pending that evidence. Every other
classification below is unchanged: MAC-auth/enterprise-802.1X (except Classic
WPA3-Enterprise) and the WPA2/WPA3 transition mode remain blocked/unsupported.

| Mode | AOS8 source signal (raw, unverified enum) | Classic target | New Central target | Classification |
|---|---|---|---|---|
| **OPEN** | `opmode` ∈ `{open, opensystem}` (checked case-insensitively, `src/hpe_networking_mcp/pipeline/aos8_target_adapters.py:850`, `:983`); source `payload["security"]["mode"] == "open"` | `full_wlan` body with `"opmode": "opensystem"`, `wpa_passphrase: ""` (`:1006-1032`) | `build_underlay_ssid`/`build_overlay_ssid` with `opmode: "OPEN"` (`:866-903`) | **exact** (both targets — implemented and tested end to end) |
| **WPA2 Personal** | source `mode == "wpa2_personal"` **only** when `opmode` explicitly contains `wpa2` *and* also contains a `psk`-style token or `wpa_passphrase`/`wpa_hexkey` is present (presence only, never the value); legacy/unrecognized personal modes that carry a passphrase/PSK signal without an explicit `wpa2` opmode token (e.g. WPA1/TKIP `wpa-psk-tkip`, `wpa-tkip`) are reported as `unknown` with a warning rather than guessed | no verified payload — Classic's documented v2 WPA2-Personal contract is unreconciled with the `full_wlan` (v1) shape used here (§5); remains `manual` (`_CLASSIC_WLAN_MODE_GUIDANCE["wpa2_personal"]`) | **implemented**: `NewCentralAdapter._map_wlan` maps to `build_underlay_ssid`/`build_overlay_ssid` with `opmode: "WPA2_PERSONAL"` and a transient, caller-supplied `wpa_passphrase` (`_secret_value` — never recovered from source state); confirmed live at the `preview()`/preflight-read level by `aos8-live-dryrun-eval` (2026-07-25, `docs/aos8-live-dryrun-evaluation.md`); no live `apply()` + secret read-back yet | `manual` (Classic), `conditional` (New Central — implemented and live-preview-confirmed, pending a live apply + secret read-back) |
| **WPA3-SAE** | source `mode == "wpa3_sae"` when `opmode` contains `wpa3` and a PSK-style signal (text/passphrase/hex-key presence) | **implemented**: `ClassicCentralAdapter._wpa3_personal_wlan_action` maps to `full_wlan` with `opmode: "wpa3-sae-aes"`, `opmode_transition_disable: true`, and a transient, caller-supplied `wpa_passphrase`, citing the official `apifull_wlancreate_wlan` sample plus `central-python-workflows` `psk_network.yaml` as secondary corroboration; unit-tested end to end including a full apply + read-back round trip (`tests/unit/test_aos8_migration_orchestrator.py`), but never confirmed against a live Classic tenant | **implemented**: same `_map_wlan` path as WPA2 Personal above with `opmode: "WPA3_SAE"`; confirmed live at the `preview()`/preflight-read level by `aos8-live-dryrun-eval` (2026-07-25); no live `apply()` + secret read-back yet | `conditional` (Classic — implemented, official-sample-evidenced, unit-tested; pending a live apply + read-back), `conditional` (New Central — implemented and live-preview-confirmed, pending a live apply + secret read-back) |
| **WPA2/WPA3 transition (mixed personal)** | source `mode == "wpa3_transition_personal"` when the evidenced `wpa3_transition` ssid_prof flag is true (`src/hpe_networking_mcp/pipeline/aos8_parsers.py` `_wlan_security_signals`); AOS8 concept still maps loosely to `BOTH_WPA_WPA2_PSK` or the target-side `wpa3-transition-mode-enable` flag, not a single 1:1 field | ambiguous per §5 (transition mode unreconciled in official samples); `_CLASSIC_WLAN_MODE_GUIDANCE["wpa3_transition_personal"]` rejects it | `wpa3-transition-mode-enable` boolean exists (`wlan.json:5578-5584`, `:5714-5720`) but `NewCentralAdapter._map_wlan` explicitly returns a dry-run-only blocker for this mode pending live validation against a real WPA3-transition SSID | `manual`/`unsupported` until live validation |
| **Enhanced Open (OWE)** | source `mode == "enhanced_open"` when `opmode` contains both `enhanced` and `open` (keyword match, no in-repo enum evidence for the exact AOS8 CLI token) | no verified payload — no official sample or live read confirms the exact Classic opmode token (`_CLASSIC_WLAN_MODE_GUIDANCE["enhanced_open"]`); remains `manual` | **implemented**: same `_map_wlan` path as WPA2 Personal/WPA3-SAE above with `opmode: "ENHANCED_OPEN"` (no passphrase — Enhanced Open never carries one); confirmed live at the `preview()`/preflight-read level by `aos8-live-dryrun-eval` (2026-07-25) | `manual` (Classic), `conditional` (New Central — implemented and live-preview-confirmed, pending a live apply + read-back) |
| **MAC-auth only** | `AOS8Wlan.aaa_profile` reference exists but is explicitly rejected — `NewCentralAdapter._map_wlan` raises `AdapterError` whenever `aaa_profile` is non-empty (`:844-847`); Classic raises the identical error (`:979-982`). Source `mode == "mac_auth_only"` when the resolved `aaa_profile` has a `mac_auth_profile` and no PSK/passphrase signal | `unsupported` (both adapters explicitly reject any AAA-profile-attached WLAN) | `unsupported` | **unsupported** (deliberate fail-closed, both targets) |
| **MAC-auth + PSK** | same AAA-profile-attach gap as above; source `mode == "mac_auth_psk"` when the resolved `aaa_profile` has a `mac_auth_profile` and a PSK/passphrase signal is also present | `unsupported` | `unsupported` | **unsupported** |
| **Enterprise (802.1X)** | same AAA-profile-attach gap for WPA2-Enterprise, plus dot1x server-group chain (§6.3–§6.7) is unmapped end to end; source `mode == "enterprise_dot1x"` when the resolved `aaa_profile` has a `dot1x_auth_profile` | WPA2-Enterprise: `unsupported` (Classic dependency lifecycle is explicitly called ambiguous, §5). **WPA3-Enterprise is a distinct, narrower exception**: `ClassicCentralAdapter._wpa3_enterprise_wlan_action` maps a `wpa3` opmode to `full_wlan` citing an official sample, but only when the caller supplies an already-existing Classic auth-server name via the stateless `aos8_preview_migration_run`'s `external_object_references` (§1.2/§1.3); every `CandidateAction` it returns is `dry_run_only=True` and can never reach `applied`/`skipped` regardless of `dry_run`/`confirm`, pending live confirmation of the enterprise dependency lifecycle | `unsupported`; target schema supports `WPA2_ENTERPRISE`/`WPA3_ENTERPRISE_*` but the AAA-profile-attach rejection in `NewCentralAdapter._map_wlan` blocks every enterprise mode | **unsupported** (WPA2-Enterprise, both targets; New Central WPA3-Enterprise); **conditional, dry-run-only** (Classic WPA3-Enterprise only — schema/sample-evidenced, never reaches an applied/skipped state) |

**Dependencies (all modes)**: `vlan:{vlan_id}` and, once unblocked, `aaa_profile:{name}` (`_dependencies` call in `build_migration_plan`, `src/hpe_networking_mcp/pipeline/aos8_migration.py:723-726`). **Apply order**: 50. **Secrets**: WPA2/WPA3 Personal passphrases and any MPSK/WEP key material are never persisted in the candidate payload — `payload["security"]` carries only presence booleans (`passphrase_present`/`psk_hexkey_present`), and the raw `wpa_passphrase`/`wpa_hexkey`/`wepkey1`-`wepkey4` values are redacted wherever the full ssid_prof is otherwise retained in `unsupported_fields`. **A transient, non-persisted secret-input flow now exists** for every implemented secured mode (New Central `wpa2_personal`/`wpa3_sae`; Classic `wpa3_sae`/WPA3-Enterprise): `aos8_preview_migration_run`/`aos8_create_migration_run` only ever inject a fixed, non-secret `__runtime_secret_placeholder__` literal (`hpe_networking_mcp.pipeline.aos8_migration_orchestrator._placeholder_secret_inputs`) — never a real value — while `aos8_apply_migration_run`'s `target_secrets` argument is the only channel for a real passphrase, is validated for length/non-redaction (`_secret_value`/`MAX_SECRET_LENGTH`), and is never written to the persisted run state; every apply call must resupply it.

### 6.3 AAA profiles

| | Classic Central | New Central |
|---|---|---|
| **Source fields/aliases** | `AOS8AAAProfile`: `profile-name`/`name`; `default_user_role`/`default-user-role`; `dot1x_auth_profile`/`dot1x-auth-profile`; `dot1x_default_role`/`dot1x-default-role`; `dot1x_server_group`/`dot1x-server-group`; `mac_auth_profile`/`mac-auth-profile`; `mac_default_role`/`mac-default-role`; `mac_server_group` aliases are `mac_server_group`/`mba_server_group`/`mac-server-group` (§3 item 1 — RESOLVED); `accounting_server_group` from `rad_acct_sg`/`radius-accounting-server-group` (`src/hpe_networking_mcp/pipeline/aos8_parsers.py`) | same source |
| **Candidate payload** | `{"name","default_user_role","dot1x_auth_profile","dot1x_default_role","dot1x_server_group","mac_auth_profile","mac_default_role","mac_server_group","accounting_server_group"}` (`_aaa_payload`, `src/hpe_networking_mcp/pipeline/aos8_migration.py:433-445`) | same |
| **Target method/path** | not implemented (falls to generic rejection) | `create_aaa_profile` → `POST /network-config/v1alpha1/aaa-profile/{name}` (`src/hpe_networking_mcp/pipeline/aos8_target_adapters.py:819`) |
| **Schema/context** | n/a | `x-supportedDeviceType: [Gateway, Switch CX, Switch PVOS]` (`aaa-profile.json`) — **not** an AP concept; `device-function`/`persona` must resolve to a gateway or switch persona |
| **Payload fields actually mapped** | n/a | `auth_role` (← `default_user_role`), `acct_server_group` (← `accounting_server_group`), `dot1x_auth_profile` (→ `authentication.dot1x-auth`), and `mac_auth_profile` (→ `authentication.mac-auth`). `dot1x_default_role`, `dot1x_server_group`, `mac_default_role`, and `mac_server_group` remain rejected until their distinct target fields and lifecycle are verified. |
| **Dependencies/apply order** | `role:{default_user_role, dot1x_default_role, mac_default_role}`, `dot1x_auth_profile:{...}`, `mac_auth_profile:{...}`, `server_group:{dot1x_server_group, mac_server_group, accounting_server_group}` (`src/hpe_networking_mcp/pipeline/aos8_migration.py:711-720`); apply order 40 | same |
| **Preflight/read-back** | none | `get_aaa_profile` (`:825-831`) |
| **Secrets** | none directly | none directly (server-group/auth-server secrets flow through dependencies) |
| **Classification** | `unsupported` | **conditional** — the object contract (create/update/delete/read) is verified and tested for the simple subset and for schema-defined `authentication.dot1x-auth` / `authentication.mac-auth` bindings (`test_simple_aaa_profile_maps_only_verified_fields`, `test_aaa_profile_maps_device_auth_profile_bindings`, `test_aaa_profile_rejects_ap_persona_and_exposes_update_delete_operations`). A live 0.6 config-assignment GET confirmed the `"aaa-profile"` literal and the adapter exposes exact bounded assignment reads, but candidates remain `blocked` pending a disposable create/assign/read-back/unassign/delete lifecycle (`_assignment_write_blocker`). Default-role and direct server-group authentication attributes remain unsupported. |

### 6.4 Device 802.1X profiles

| | Classic Central | New Central |
|---|---|---|
| **Source fields** | `AOS8AuthProfile(auth_type="dot1x")`: `profile-name`/`name`, all else in `.settings` (`parse_auth_profiles`, `src/hpe_networking_mcp/pipeline/aos8_parsers.py:399-419`) | same |
| **Candidate payload** | `{"name","auth_type":"dot1x"}` plus `unsupported_fields=profile.settings` (`src/hpe_networking_mcp/pipeline/aos8_migration.py:562-577`) | same |
| **Target method/path** | none verified | `NewCentralAdapter._map_dot1x_auth_profile` → `POST/PATCH/DELETE/GET /network-config/v1alpha1/dot1xauth/{name}` (`src/hpe_networking_mcp/pipeline/aos8_target_adapters.py:1695-1699`, shared `_map_device_auth_profile` helper) — a bare, name-only profile is a verified mapping; any additional field raises `AdapterError` |
| **Schema context** | n/a | `x-supportedDeviceType: [Gateway, Switch CX, Switch PVOS]` — Gateway/switch concept, **not** used for AP WLAN 802.1X (that lives on the WLAN resource itself, `ArubaWlanSecurity_AuthServerConfig`) |
| **Dependencies/apply order** | referenced by `aaa_profile.dot1x_auth_profile`; apply order 20 | same |
| **Classification** | `unsupported` | **conditional** — the bare-name object contract is verified and tested (`test_bare_dot1x_and_macauth_profiles_map_on_gateway_switch_only`). A live 0.6 assignment GET confirmed `"dot1xauth"` and exact assignment reads are wired, but the candidate stays `blocked` pending the controlled write lifecycle; profiles with additional settings remain `unsupported` (`test_rich_dot1x_and_macauth_profiles_are_rejected_not_guessed`). |

### 6.5 Device MAC-auth profiles

| | Classic Central | New Central |
|---|---|---|
| **Source fields** | `AOS8AuthProfile(auth_type="mac")`, same shape as 802.1X (`src/hpe_networking_mcp/pipeline/aos8_parsers.py:399-419`) | same |
| **Target method/path** | none verified | `NewCentralAdapter._map_mac_auth_profile` → `POST/PATCH/DELETE/GET /network-config/v1alpha1/macauth/{name}` (`src/hpe_networking_mcp/pipeline/aos8_target_adapters.py:1702-1707`, shared `_map_device_auth_profile` helper) — same bare-name-only contract as §6.4 |
| **Schema context** | n/a | `x-supportedDeviceType: [Gateway, Switch CX, Switch PVOS]` — same Gateway/switch-only caveat as §6.4 |
| **Classification** | `unsupported` | **conditional** — same bare-name-verified status as §6.4; a live 0.6 assignment GET confirmed `"macauth"` and exact assignment reads are wired, while writes remain blocked pending the controlled lifecycle. |

### 6.6 Central NAC auth-profile alternative

| | Classic Central | New Central |
|---|---|---|
| **Purpose** | n/a | Distinct auth-policy surface (`ingestion/sources/openapi_specs/cda-auth-profile.json`, `x-tag-group: "Central NAC"`; companion `cda-authz-policy.json`) covering MAB, MPSK, wired-profile, and EAP/custom-certificate authentication policy — `/network-config/v1alpha1/auth-profiles/{auth-profile-id}` and `/network-config/v1alpha1/authz-policies/{policy-id}` |
| **Relationship to AOS8 source** | n/a | **Never** an automatic conversion target for an AOS8 `dot1x_auth_profile`/`mac_auth_profile`/LDAP/TACACS server. Central NAC is an explicit, operator-selected alternative architecture, only to be offered as an option in tooling/UX copy — never auto-selected by the migration hpe_networking_mcp.pipeline. |
| **Classification** | n/a | **manual** (by design — requires an explicit operator decision, not a mapping) |

### 6.7 Auth servers (RADIUS / RadSec / LDAP / TACACS)

| | Classic Central | New Central |
|---|---|---|
| **Source fields** | `AOS8AuthServer`: RADIUS from `radius_servers` (`rad_server_name`/`name`, `rad_host`/`host`); LDAP from `ldap_servers` (`ldap_server_name`/`name`, `ldap_host`/`host`); TACACS from `tacacs_servers` (`tacacs_server_name`/`name`, `tacacs_host`/`host`); all else in `.settings` (`parse_auth_servers`, `src/hpe_networking_mcp/pipeline/aos8_parsers.py:497-527`) | same |
| **Candidate payload** | `{"name","server_type","host"}` plus `unsupported_fields=server.settings` (`src/hpe_networking_mcp/pipeline/aos8_migration.py:517-536`) | same |
| **Target method/path** | none verified | `create_auth_server` → `/network-config/v1alpha1/auth-servers/{name}` (`src/hpe_networking_mcp/pipeline/aos8_target_adapters.py:728-780`) |
| **Schema/enum** | n/a | `ArubaAuthServer_AuthServersauthServerSchema.type` ∈ `{RADIUS, LDAP, TACACS, WINDOWS, RFC3576, XMLAPI, RADSEC, LOCAL}` (`auth-server.json`); per-platform support varies (AP: RADIUS/LDAP/TACACS/XMLAPI; CX/PVOS: RADIUS/TACACS; GW: RADIUS/TACACS/WINDOWS/LDAP) |
| **Payload fields mapped** | n/a | Adapter **only** accepts `server_type == "radius"`; anything else raises `AdapterError` (`:730-733`). Allowed unsupported-field passthrough: `rad_authport`, `rad_acctport`, `rad_key`/`radius_key`/`radius_secret`/`shared_secret` (`:735-746`) |
| **Secrets** | n/a | `shared_secret` is resolved via `_secret_value(context, key, "shared_secret")` (`:754`) — an ephemeral, apply-time-only caller-supplied secret, never persisted; marked `sensitive_argument_fields=("shared_secret",)` for preview masking (`:775`) |
| **Preflight/read-back** | none | `get_auth_server` (`:768-776`) |
| **Dependencies/apply order** | referenced by `server_group.auth_servers`; apply order 10 | same |
| **Classification (RADIUS)** | `unsupported` | **conditional** — the object contract (create/update/delete/read, secret handling) is verified and tested (`test_radius_requires_caller_secret_and_masks_preview`, `test_radius_auth_server_has_verified_update_and_delete_operations`). A live 0.6 assignment GET confirmed `"auth-servers"` and exact assignment reads are wired, but candidates remain `blocked` pending the controlled create/assign/read-back/unassign/delete lifecycle. |
| **Classification (RadSec)** | `unsupported` | **conditional** — schema value `RADSEC` exists but no field-level audit or adapter code exists; must be scoped separately (§4) |
| **Classification (LDAP)** | `unsupported` | **unsupported** — explicitly rejected by the adapter (`:730-733`); the `ldap_admindn` over-redaction defect (§3 item 6) is now resolved, so this row is blocked solely by the adapter's explicit RADIUS-only rejection, not by secret handling |
| **Classification (TACACS)** | `unsupported` | **unsupported** — explicitly rejected by the adapter |

### 6.8 Server groups

| | Classic Central | New Central |
|---|---|---|
| **Source fields** | `AOS8ServerGroup`: `sg_name`/`profile-name`/`name`; `auth_server`/`auth-server` (list, each resolved via `_server_reference` to a bare name); `fail_thru`/`fail-through`; `load_balance`/`load-balance`; `derivation_rules_vlan_role`; all else in `.settings` (`parse_server_groups`, `src/hpe_networking_mcp/pipeline/aos8_parsers.py`). Type-collision risk (§3 item 4) is now resolved at the migration-planning layer — see next row. | same |
| **Candidate payload** | `{"name","auth_servers","auth_server_entries","fail_through","load_balance","derivation_rules"}` (`src/hpe_networking_mcp/pipeline/aos8_migration.py:591-599`) | same |
| **Target method/path** | none verified | `NewCentralAdapter._map_server_group` → `POST/PATCH/DELETE/GET /network-config/v1alpha1/server-groups/{name}` (`src/hpe_networking_mcp/pipeline/aos8_target_adapters.py:1709-1851`) — builds the ordered `servers` array from resolved same-type `auth_server` dependencies |
| **Schema** | n/a | `ArubaAuthServerGroup_ServerGroupsserverGroupSchema.servers` is an **ordered** array (`server-name` + `position` + `match-rules`), `type` restricted to CX (`RADIUS`/`TACACS` mandatory there); `load-balance`/`load-balance-algo` are AP/Gateway concepts |
| **Dependencies/apply order** | resolved by `server_ids_by_name` keyed by name **and** server type (§3 item 4 — RESOLVED); a same-name collision across RADIUS/LDAP/TACACS is left unresolved with an explicit `auth_server_type_collisions` warning rather than guessed; apply order 20 | same |
| **Classification** | `unsupported` | **conditional** — the homogeneous-type (RADIUS/LDAP/TACACS-only, non-RadSec) ordered-`servers` mapping is verified and tested (`test_server_group_builds_ordered_servers_array_from_dependencies`). A live 0.6 assignment GET confirmed `"server-groups"` and exact assignment reads are wired, but candidates remain `blocked` pending the controlled write lifecycle; mixed-type, unresolved-entry, RadSec-member, and `derivation_rules`-bearing groups remain `unsupported` (`test_server_group_rejects_mixed_auth_server_types`, `test_server_group_rejects_type_collision_flag_and_unresolved_entries`, `test_server_group_radsec_dependency_is_unsupported_not_a_crash`). |

### 6.9 Roles

| | Classic Central | New Central |
|---|---|---|
| **Source fields** | `AOS8Role`: `rolename`/`role`/`name`/`profile-name`; `vlan`; `acl`/`access-list`; `captive-portal-profile` (known-lossy, `UNSUPPORTED_FIELDS["role"]["captive_portal_profile"]`, `src/hpe_networking_mcp/pipeline/aos8_schema.py:255-262`) | same |
| **Candidate payload** | `{"name","vlan","acl"}` (Classic key is `"acl"`) | `{"name","vlan","policies"}` (New Central key is `"policies"`) — `_role_payload(new_central=bool)` branches the key name (`src/hpe_networking_mcp/pipeline/aos8_migration.py:360-389`) |
| **Target method/path** | none verified (falls to generic rejection) | `create_role`/`update_role` → `POST/PUT /network-config/v1/roles/{name}` (`src/hpe_networking_mcp/pipeline/aos8_target_adapters.py:706-715`), plus a required config-assignment (`:689-703`, see §2.1 divergence) |
| **Payload fields mapped** | n/a | only a normalized ACL value of `allowall`/`sys_allow_all` is accepted; anything else raises `AdapterError` — **custom AOS8 ACLs are never reduced to allow-all silently; they are blocked** (`:661-665`) |
| **Dependencies/apply order** | `vlan:{vlan}`, `policy:{acl}` (via `_policy_dependencies`, `:460-469`); apply order 30 | same |
| **Preflight/read-back** | none | `list_roles(full_list=True)` matched by identifier (`:718-725`) |
| **Classification** | `unsupported` | **conditional** — allow-all roles only are `exact`/tested (`tests/unit/test_aos8_target_adapters.py:150-192` pattern); any role with a real ACL is `manual`/`unsupported` pending §6.10 |

### 6.10 Gateway security policies vs. role ACLs (explicit distinction)

| Concept | Source | Classic target | New Central target | Notes |
|---|---|---|---|---|
| **AOS8 session ACL / "policy"** | `AOS8Policy`/`AOS8PolicyRule` — IPv4 rules from `acl_sess__v4policy` (or legacy `rule`/`rules`), IPv6 from `acl_sess__v6policy`; per-rule aliases `source/src/source-address/...`, `destination/dst/...`, `service/svc/protocol/application/app`, `action/permit/deny`, `log/logging` (`parse_policies`/`_parse_policy_rules`, `src/hpe_networking_mcp/pipeline/aos8_parsers.py:266-338`) | none verified | `_map_policy` is persona-gated to Gateway and preserves ordered IPv4 `any`→`any`, service `any`, permit/allow or deny, and absent/disabled logging as `RULE_ANY` + `ADDRESS_ANY` + `ACTION_ALLOW`/`ACTION_DENY`; every other semantic is rejected | Classic remains `unsupported`; the exact Gateway subset is `conditional` and dry-run-only |
| **CX role ACL** | n/a (target-only concept) | n/a | `ArubaRoleAcl_RoleAclsSchemaGet.x-supportedDeviceType == ["Switch CX"]` — only valid when the target persona is a CX switch | Never apply a role-ACL write against a Gateway/AP persona |
| **Gateway security policy** | n/a (target-only concept) | n/a | `/network-config/v1alpha1/policies/{name}` (`policy.json`); `create_gw_policy`, `list_gw_policies`, and `delete_gw_policy` are in the production migration invoker allowlists. Live bounded GET evidence confirmed `policy/security-policy/policy-rule[]`, rule position, `CONDITION_DEFAULT`, `RULE_ANY`, `ADDRESS_ANY`, and allow/deny action shapes. | Distinct from role ACLs. The adapter preview is dry-run-only until a disposable create/read-back/delete lifecycle proves the write contract. |

**Classification**: `unsupported` on Classic, AP, and CX targets. `conditional`
and dry-run-only on a Gateway for the exact ordered IPv4 any-to-any
permit/deny subset. Named services, source/destination aliases, IPv6, logging,
applications, captive portal behavior, and other lossy rules remain
`manual`/`unsupported` and are never reduced to allow-all (per §4).

### 6.11 AP-group / device-group / profile assignment

| | Classic Central | New Central |
|---|---|---|
| **Source fields** | `AOS8ApGroup.profile_name`, `.virtual_ap_profiles` (list of VAP names, `parse_ap_groups`, `src/hpe_networking_mcp/pipeline/aos8_parsers.py:12-33`) | same |
| **Candidate payload** | `{"name","wlan_profiles"}` (sorted VAP names mapped through `vap_to_wlan`, `src/hpe_networking_mcp/pipeline/aos8_migration.py:770-792`) | same |
| **Target method/path** | none verified (falls to generic rejection) | no `_map_ap_group` method on `NewCentralAdapter`; falls to `unsupported` via `getattr(self, "_map_ap_group", None)` returning `None` (`:574-580`) |
| **Dependencies/apply order** | `wlan:{...}` per VAP (`:774-778`); apply order 60 | same |
| **Required operator input** | n/a | **Explicit** Device Group/scope selection and profile (WLAN/role/VLAN) assignment — there is no automatic 1:1 creation of a Device Group from an AOS8 AP group. VAP-to-WLAN dependency resolution must be validated against the actual export; an unresolved VAP now produces an explicit per-reference warning (§3 item 5 — RESOLVED) in addition to the generic dependency-not-present check, and never invents a target WLAN |
| **Classification** | `unsupported` | **unsupported** |

### 6.12 IPv4/IPv6 static routes

| | Classic Central | New Central |
|---|---|---|
| **Source fields** | `AOS8Route`: `address_family`; `destip`/`destination`; `destmask`/`netmask`; `nexthop`/`next-hop`; `nexthop1`/`secondary-next-hop`; `vlanid`/`vlan`; `cost`; `cost1`; `zero`; all else in `.settings` (`parse_routes`, `src/hpe_networking_mcp/pipeline/aos8_parsers.py:531-568`) | same |
| **Candidate payload** | `{"address_family","destination","netmask","next_hop","secondary_next_hop","vlan_id","cost","secondary_cost","zero"}` (`src/hpe_networking_mcp/pipeline/aos8_migration.py:824-838`) | same |
| **Target method/path (IPv4)** | none verified | `/network-config/v1alpha1/static-route/{name}` (`static-route.json`) — no adapter mapper; not yet implemented. The existing curated `gateway_config_static_route` uses a divergent `/network-config/v1` PUT/payload and must not be reused until both versions are reconciled live. `get_network_profile(profile_type="static-route")` is read-only evidence only. |
| **Schema notes** | n/a | `ArubaStaticRoute_Ipv4RouteCfg` keys by composite `prefix-vrf-nexthop-id`; `forwarding-type` enum (`NEXTHOP, INTERFACE, NULLROUTE, REJECT, VLAN, TUNNEL, IPSECMAP, CLUSTER`) is CX-only for that field. A live Gateway read returned only a `default-gateway[]` representation, so the exact arbitrary-destination write contract remains unverified. |
| **IPv6** | n/a | `ArubaStaticRoute_Ipv6RouteCfg` — schema-expressible, same unverified-destination caveat, classified conditional pending live evidence |
| **Switch routes** | n/a | `l3-route.json` (`/l3-route`) is a **separate**, Switch-CX-only payload family (`ArubaL3Route_L3RouteSchemaGet.x-supportedDeviceType == ["Switch CX"]`) — never substitute for `static-route.json` on a Gateway/AP persona |
| **Dependencies/apply order** | `vlan:{vlan_id}` (`:846`); apply order 70 | same |
| **Classification** | `unsupported` | **conditional** for IPv4 (schema exists, destination contract unverified); **conditional** for IPv6 (same, plus narrower device support); `unsupported` for automatic Switch-CX `l3-route` selection without explicit persona confirmation |

### 6.13 VRRP / VRRPv6 / tracking

| | Classic Central | New Central |
|---|---|---|
| **Source fields** | `AOS8VRRP`: `address_family`; `id`(vrid); `{prefix}_ip`/`_vlan`/`_priority`/`_preempt`/`_shut`/`_adv_interval`/`_holdtime`/`_desc`/`_auth`; `tracking` dict assembled from `{prefix}_track_*` keys; all else in `.settings` (`parse_vrrp`, `src/hpe_networking_mcp/pipeline/aos8_parsers.py:614-660`) where `prefix` is `vrrp` (IPv4) or `vrrp6` (IPv6) | same |
| **Candidate payload** | `{"address_family","vrid","virtual_ip","vlan_id","priority","preempt","shutdown","advertisement_interval","hold_time","description","authentication","tracking"}` (`src/hpe_networking_mcp/pipeline/aos8_migration.py:856-869`) | same |
| **Target method/path** | none verified | Two **distinct** resources: `vrrp.json` → `/network-config/v1alpha1/vrrp-global` (LOCAL network profile, already wired as `_NETWORK_PROFILE_TYPES["vrrp"]` in `src/hpe_networking_mcp/mcp_servers/config.py`); `vrrp-interface.json` → `/network-config/v1alpha1/vrrp` (Gateway-only, `ArubaVrrpInterface_VrrpprofileSchema`). `get_network_profile(profile_type="vrrp-interface")` exposes the latter for bounded reads only; generic set/delete remains blocked. |
| **Schema notes** | n/a | The interface-level profile keys `virtual-router` entries by composite `router-id-address-family`, not directly by VLAN — **VLAN-interface attachment is a separate, unproven step**. Tracking is a nested `ArubaVrrpInterface_VrrpTrackingConfiguration` block that has no mapping from `AOS8VRRP.tracking` today |
| **Dependencies/apply order** | `vlan:{vlan_id}` (`:876`); apply order 80 | same |
| **Classification** | `unsupported` | **unsupported** until VLAN-interface attachment and tracking normalization are proven live; do not treat `vrrp-global` and `vrrp-interface` as interchangeable |

### 6.14 Controllers / Mobility Conductors

| | Classic Central | New Central |
|---|---|---|
| **Source fields** | `AOS8Controller.name/ip_address/model/version`, remaining raw fields (`parse_controllers`, `src/hpe_networking_mcp/pipeline/aos8_parsers.py:171-191`) | same |
| **Candidate payload** | `{"name","ip_address","model","version"}`, Classic-only candidate (no New Central candidate is emitted for controllers, `src/hpe_networking_mcp/pipeline/aos8_migration.py:876-899`) | not emitted |
| **Target method/path** | none — explicit warning appended: "AOS8 controllers/Mobility Conductors are not migrated as New Central objects; onboard replacement gateways/APs individually" (`:894-897`) | n/a |
| **Classification** | **unsupported** (explicit, by design) | **unsupported** (no candidate emitted) |

### 6.15 Network destination aliases, Ethernet ACLs, IP-classification whitelist rules (new, reference-only source families)

These three AOS8 object families were added to the parser/schema/migration
layers by the `aos8-source-coverage` 0.6 todo. All three are **normalized and
emitted as migration candidates on both targets** (unlike controllers, which
are Classic-only) purely for **dependency tracking and operator review** —
every candidate for these families carries an explicit `"no deterministic
Classic/New Central adapter mapping exists in this repository..."` warning
(`src/hpe_networking_mcp/pipeline/aos8_migration.py` `_REFERENCE_ONLY_WARNING`), and the single
source-of-truth set of affected `object_type` values is
`hpe_networking_mcp.pipeline.aos8_schema.REFERENCE_ONLY_OBJECT_TYPES`. No adapter code in
`src/hpe_networking_mcp/pipeline/aos8_target_adapters.py` was changed to support this: since
`NewCentralAdapter._map_candidate` already falls back to `_unsupported(...)`
for any `object_type` without a matching `_map_<object_type>` method, and
`ClassicCentralAdapter._map_candidate` already falls back to
`_manual_guidance(...)` for any `object_type` other than `wlan`/`ap_group`,
these new candidates are automatically treated as unsupported/manual by both
adapters with zero adapter-layer changes.

| | Network destination aliases | Ethernet ACLs | IP-classification whitelist rules |
|---|---|---|---|
| **AOS8 object(s)** | `netdst` (IPv4), `netdst6` (IPv6) | `acl_eth` (200-299 range) | `whitelist_rule` (the separate, global `whitelist` Activate-sync object is intentionally **not** parsed — see below) |
| **Source dataclass** | `AOS8NetworkDestination` (`src/hpe_networking_mcp/pipeline/aos8_schema.py`) | `AOS8EthernetACL`/`AOS8EthernetACLRule` | `AOS8WhitelistRule` |
| **Parser** | `parse_network_destinations` — combines `netdst`/`netdst6` into one `address_family`-tagged list, reading the exact `netdst__*`/`netdst6__*` request-body property names from `aos8_post_object_netdst`/`aos8_post_object_netdst6` (`src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/aos8.json`) | `parse_ethernet_acls` — same defensive alias + `unsupported_fields` catch-all pattern as `parse_policies`/`_parse_policy_rules`, since no nested `acl_eth__policy` rule schema is available locally beyond the property name (`aos8_post_object_acl_eth`) | `parse_whitelist_rules` — reads `sipaddr`/`eipaddr` per `aos8_post_object_whitelist_rule`'s required properties |
| **Absent-section behavior** | No warning when the `netdst`/`netdst6`/`acl_eth`/`whitelist_rule` keys are entirely absent from the export (via `_optional_dict_items`) — deliberately different from every other family's `_dict_items`, because `hpe_networking_mcp.mcp_servers.aos8.aos8_export_all()` does not fetch these object types yet, so their absence from every export today is expected, not a defect. A key that *is* present but malformed still warns, same as every other family. | same | same |
| **Candidate object_type** | `network_destination` (identifier `{address_family}:{name}`) | `ethernet_acl` (identifier = `accname`) | `whitelist_rule` (identifier `{start_ip}-{end_ip}`, an index-based `unknown-{i}` fallback when an address is missing, same pattern as `_route_identifier`/`_vrrp_identifier`) |
| **Apply order** | 15 (`APPLY_ORDER["network_destination"]`) — before `policy`/`ethernet_acl` (20), so a `policy` candidate that depends on a `network_destination` always sorts after it | 20 (alongside `policy`) | 15 |
| **Dependency wiring** | n/a (destinations have no dependencies of their own) | none | none |
| **Known-lossy fields** | `invert` (match-polarity negation) has no Central/New Central destination-alias equivalent; every candidate with `invert` truthy carries an explicit warning (`UNSUPPORTED_FIELDS["network_destination"]["invert"]`) | any per-rule field not matched by the bounded L2 alias set (source/destination MAC, ethertype, VLAN, action, log) is retained in `unsupported_fields` with an explicit warning (`UNSUPPORTED_FIELDS["ethernet_acl"]["unsupported_rule_field"]`), mirroring `policy`'s `unsupported_rule_field` | none — the two fields (`start_ip`/`end_ip`) fully map |
| **Target method/path** | none — reference-only by design | none — reference-only by design | none — reference-only by design |
| **Classification** | **unsupported** on both targets (candidates emitted for dependency tracking only) | **unsupported** on both targets | **unsupported** on both targets |

**Cross-family dependency enhancement (policy → network destination):** a
`policy` candidate's IPv4/IPv6 rule `destination` value that exactly matches
a parsed `netdst`/`netdst6` alias `name`, in the *same* address family as the
rule, now resolves to an explicit `network_destination:{address_family}:{name}`
dependency (`_policy_network_destination_dependencies`,
`src/hpe_networking_mcp/pipeline/aos8_migration.py`) — reusing the existing `_dependency`/
`_dependencies` helpers rather than a new mechanism, the same pattern as
`server_group`'s type-aware `auth_server` dependency resolution (§3 item 4).
A `destination` value that is not a string, or does not match any parsed
alias (e.g. `any`, a role name, or a literal address), is left alone rather
than guessed at.

**`whitelist` (the global object, not `whitelist_rule`) is intentionally not
parsed.** `aos8_get_object_whitelist`/`aos8_post_object_whitelist`
(`src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/aos8.json`) describe Mobility
Master-to-Activate synchronization settings (a provisioning URL and
credentials: `url`, `username`, `password`, `ca_cert`, `server_cert`,
`provisionurl`), not a per-item list with a stable identifier. There is
nothing here to normalize into a migration candidate, and — unlike
`whitelist_rule` — no unambiguous non-secret identifier exists to key a
candidate on. A future revision may model it as a single informational,
fully-redacted candidate if a concrete migration need is identified; today it
is out of scope, and no parser or migration code references it.

**`aos8_migration_dependency_plan`** (`src/hpe_networking_mcp/mcp_servers/aos8.py`) is the
recommended bounded, read-only entry point for surfacing these reference-only
families in context: it groups an existing plan's candidates by
`apply_order` and classifies each candidate's `status` as `reference_only`
(any `object_type` in `REFERENCE_ONLY_OBJECT_TYPES`), `blocked` (an
unresolved-dependency warning), or `ready`, without re-deriving or
duplicating `build_migration_plan`'s existing ordering/dependency logic.

**Not yet wired:** `hpe_networking_mcp.mcp_servers.aos8.aos8_export_all()` does not fetch
`netdst`/`netdst6`/`acl_eth`/`whitelist_rule` from a live AOS8 node today —
these object types are parsed and planned only when a caller supplies an
export dict that already includes them (e.g. a test fixture, or a future
increment that adds them to `aos8_export_all()`'s `object_names` fetch map
via the same generic `_aos8_collect_object` helper already used for
`aaa_profiles`/`server_groups`/routes/VRRP). This is a deliberate, minimal-
blast-radius scoping choice for this increment — see the "Remaining
blockers" note in the implementing changeset.

### 6.16 Wired / captive-portal / WISPr / Kerberos / NTLM / stateful-802.1X authentication profiles (`v07-aos8-promotion`, new reference-only source families)

Six additional AOS8 authentication-profile object families were added to the
parser/schema/migration layers by the `v07-aos8-promotion` todo, using local
manifest evidence (`src/hpe_networking_mcp/mcp_servers/openapi_gen/manifests/aos8.json`
`aos8_post_object_wired_auth_profile`/`_stateful_dot1x_auth_profile`/
`_wispr_auth_profile`/`_cp_auth_profile`/`_krb_auth_profile`/
`_ntlm_auth_profile` request-body properties). All six are **normalized and
emitted as migration candidates on both targets, reference-only** — the same
pattern established for network destinations/Ethernet ACLs/whitelist rules
in §6.15: every candidate carries the explicit
`"no deterministic Classic/New Central adapter mapping exists..."` warning
(`src/hpe_networking_mcp/pipeline/aos8_migration.py` `_REFERENCE_ONLY_WARNING`), and all six
`object_type` values were added to the single source-of-truth
`hpe_networking_mcp.pipeline.aos8_schema.REFERENCE_ONLY_OBJECT_TYPES` set. No adapter code
changed: both adapters' existing unmapped-`object_type` fallback already
covers any new reference-only family with zero adapter-layer changes (same
mechanism §6.15 describes).

| | `wired_auth_profile` | `stateful_dot1x_auth_profile` | `wispr_auth_profile` | `cp_auth_profile` (captive portal) | `krb_auth_profile` (Kerberos) | `ntlm_auth_profile` |
|---|---|---|---|---|---|---|
| **Shape** | singleton, unnamed (no `profile-name` in its request-body schema) | singleton, unnamed | named list (`profile-name`) | named list (`profile-name`) | named list (`profile-name`) | named list (`profile-name`) |
| **Source dataclass** | `AOS8WiredAuthProfile` | `AOS8StatefulDot1xAuthProfile` | `AOS8WisprAuthProfile` | `AOS8CaptivePortalAuthProfile` | `AOS8KerberosAuthProfile` | `AOS8NTLMAuthProfile` (`src/hpe_networking_mcp/pipeline/aos8_schema.py`) |
| **Parser** | `parse_wired_auth_profiles` | `parse_stateful_dot1x_auth_profiles` | `parse_wispr_auth_profiles` | `parse_cp_auth_profiles` | `parse_krb_auth_profiles` | `parse_ntlm_auth_profiles` (`src/hpe_networking_mcp/pipeline/aos8_parsers.py`) |
| **Export location** | `aaa.wired_auth_profiles` | `aaa.stateful_dot1x_auth_profiles` | `aaa.wispr_auth_profiles` | `aaa.cp_auth_profiles` | `aaa.krb_auth_profiles` | `aaa.ntlm_auth_profiles` — all six nested under the export's existing `aaa` section (`hpe_networking_mcp.mcp_servers.aos8.aos8_export_all()`'s `object_names` map), alongside `dot1x_auth_profiles`/`mac_auth_profiles` |
| **Identifier** | `"global"` (`"global-{n}"` for an unexpected additional instance — always warned about, never silently dropped/overwritten) | `"global"` (same convention) | `profile-name` | `profile-name` | `profile-name` | `profile-name` |
| **Candidate payload fields mapped** | `aaa_profile`, `blacklist_time` | `mode`, `server_group`, `default_role`, `timeout` | `name`, `default_role`, `server_group` | `name`, `default_role`, `default_guest_role`, `server_group` | `name`, `default_role`, `server_group`, `timeout` | `name`, `default_role`, `server_group`, `enabled`, `timeout` |
| **Dependencies** | `aaa_profile:{aaa_profile}` | `server_group:{server_group}`, `role:{default_role}` | `role:{default_role}`, `server_group:{server_group}` | `role:{default_role}`, `role:{default_guest_role}`, `server_group:{server_group}` | `role:{default_role}`, `server_group:{server_group}` | `role:{default_role}`, `server_group:{server_group}` |
| **Apply order** | 45 (after `aaa_profile`=40, which it references) | 35 (after `role`=30, before `aaa_profile`=40) | 35 | 35 | 35 | 35 |
| **Unmapped fields** | every other `wired_auth_profile` property retained in `.settings`/`unsupported_fields` with the standard "not mapped" warning | same | `agent_string`, the `wispr_id_*`/`wispr_name_*` location fields, `wispr_load_thresh`, `wispr_max_delay`, `wispr_maxf`, `wispr_min_delay`, `wispr_auth_profile_clone` | redirect/branding/proxy/AUP/session/black-white-list settings, `cp_auth_profile_clone` | `krb_auth_profile_clone` | `ntlm_auth_profile_clone` |
| **Target method/path** | none — reference-only by design | none | none | none | none | none |
| **Classification** | **unsupported** on both targets (candidate emitted for dependency tracking/operator review only) | **unsupported** | **unsupported** | **unsupported** | **unsupported** | **unsupported** |

**Promotion status.** None of these six families is promoted beyond
reference-only by this revision: there is no committed New Central schema
evidence for a *direct, 1:1* wired/WISPr/captive-portal/Kerberos/NTLM
authentication-profile conversion target in
`ingestion/sources/openapi_specs/*.json`. Central NAC's
`cda-auth-profile.json`/`cda-authz-policy.json` (§6.6) remain the closest
documented adjacent New Central authentication-policy surface for the
device-profile families, and `cda-portal-profile.json` (`x-tag-group:
"Central NAC"`, `/portals` — "customizations to be applied to Central NAC
portals (e.g. MPSK provisioning, or captive portals)") is a newly-noted,
directly-relevant adjacent schema specifically for `cp_auth_profile`. None
of these is treated as an automatic conversion target for any AOS8
authentication-profile family, including these six — per §6.6, Central NAC
is an explicit, operator-selected alternative architecture, never
auto-selected by the migration pipeline, and no adapter mapping to any of
these three schemas has been written or tested. Promoting any of these six
rows to `conditional`/`exact` requires, at minimum: (1) a committed target
schema for the equivalent New Central concept (now closer for
`cp_auth_profile` given `cda-portal-profile.json`, but still unmapped), or
(2) a completed, evidenced live create/assign/read-back/delete round trip
against a real New Central tenant — neither exists today. Per requirement 4
of this todo, these rows stay explicitly `unsupported`/reference-only with
precise blockers recorded above rather than guessed at.

**Regression coverage**: `tests/unit/test_aos8_parsers.py` (parser fixtures,
alias/settings coverage, singleton-instance-count warning, absent-section
tolerance) and `tests/unit/test_aos8_migration.py`
(`test_new_auth_profile_families_are_reference_only`,
per-family payload/dependency/candidate tests, JSON-serialization check).

## 7. Implementation order (post-matrix)

1. ~~Fix the §3 prerequisites (`mac_server_group` alias, `ldap_admindn` redaction split, server-group name+type dependency keying) in `src/hpe_networking_mcp/pipeline/aos8_parsers.py`/`src/hpe_networking_mcp/pipeline/aos8_migration.py`~~ — **done** by the `aos8-source-enrichment` todo (§3 items 1, 4, 5, 6); item 2 (WLAN security normalization) source-layer parsing is done, and adapter/target mappings for `open`/`wpa2_personal`/`wpa3_sae`/`enhanced_open` now exist on New Central (and `open`/`wpa3_sae`/WPA3-Enterprise on Classic) per §6.2. **Not all of these are `conditional`**: `open` is `exact` on both targets (implemented and tested end to end); Classic `wpa3_sae` and WPA3-Enterprise, and every New Central secured mode (`wpa2_personal`/`wpa3_sae`/`enhanced_open`), remain `conditional` pending live apply + secret read-back, not yet `exact` (see §6.2 rows/checklist).
2. ~~Enrich WLAN security parsing only far enough to represent the AOS8-side signal needed for `OPEN`, `WPA2_PERSONAL`-equivalent, `WPA3_SAE`-equivalent, `ENHANCED_OPEN`-equivalent, and enterprise dot1x/mac-chain references — without inventing an AOS8 enum this repository has not observed in a real export~~ — **done** (§3 item 2, §6.2).
3. ~~Implement New Central adapter mappers for `dot1x_auth_profile`, `mac_auth_profile`, `server_group` (ordered `servers`, type-aware)~~ — **done** (§6.4/§6.5/§6.8). Live 0.6 GET evidence confirmed all five assignment literals and exact assignment reads are wired; writes remain blocked until the disposable create/assign/read-back/unassign/delete lifecycle passes. A Gateway-only policy mapper now covers the exact dry-run-only any-to-any IPv4 permit/deny subset (§6.10); broader Gateway rules and all CX role-ACL translation remain blocked. Still to implement: `route` (static-route, Gateway/AP), `vrrp`/`vrrp-interface` (with explicit VLAN-interface attachment), and `ap_group`.
4. Implement Classic adapter mappers only from official Classic docs/collections or validated live read shapes — never by reusing a New Central payload shape.
5. Resolve the §2.1 curated-tool-vs-spec divergence for config-assignments before any role/SHARED-object write path is trusted. **This is now the single blocking item for every implemented-but-`blocked` SHARED profile type** (`auth-servers`, `aaa-profile`, `dot1xauth`, `macauth`, `server-groups` — only `roles` has an independently evidenced `profile-type` literal today).
6. Re-run this matrix's classification for every row that moved from `unsupported`/`conditional`/`blocked` to `exact`, with the exact live evidence cited.
7. Run `scripts/evaluate_aos8_070_disposable_lifecycle.py --mode write` (§10.2) for each of `auth_server`/`server_group`/`aaa_profile`/`dot1x_auth_profile`/`mac_auth_profile`/`assignment` against a disposable lab scope, and record the read-back-confirmed result back into §6.3/§6.4/§6.5/§6.7/§6.8/§6.9 before reclassifying any of them past `conditional`/`blocked`.
8. Once at least one real (non-lab) migration run has reached `applied` candidates, exercise `hpe_networking_mcp.pipeline.aos8_rollback`/`aos8_execute_migration_rollback` (§10.1) against it in a disposable/lab context and record whether the verified inverse operations actually restore prior state, before ever recommending rollback execution as part of a standard operating procedure.

## 8. Verification and live-lab gate checklist

All of the following remain **read-only discovery and dry-run only**. No real write against any target is authorized by this document; a real write requires a separate, explicit confirmation naming the exact target and payload, per the 0.5 plan's write-gate contract (`src/hpe_networking_mcp/pipeline/aos8_target_adapters.py` `WriteGateError`, `execute(..., dry_run: bool, confirmation: bool)`).

- [ ] Enable/configure the optional AOS8 backend and run `aos8_login`/session diagnostics.
- [ ] Export a bounded source configuration (`aos8_export_all`) and retain only sanitized evidence (never raw secrets).
- [ ] Resolve Classic group and New Central scope/persona/gateway/cluster context with **read-only** calls before constructing any `TargetContext`.
- [ ] Run `aos8_migration_plan` and confirm every candidate's `classification` in this matrix matches the adapter's actual `CandidateAction.status` (`ready`/`blocked`/`unsupported`) for a real export.
- [ ] For each `exact` row (currently: New Central `vlan`, `role` allow-all subset, `wlan` open bridged/tunneled; Classic `wlan` open bridged only), run `dry_run=True` previews against a live lab scope/group and confirm the preflight read, payload, and read-back match.
- [ ] For each `conditional`-and-`blocked`-pending-config-assignment row (New Central `auth_server` RADIUS-only, `aaa_profile` simple subset, `dot1x_auth_profile`/`mac_auth_profile` bare-name, `server_group` homogeneous-type), confirm the real `"auth-servers"`/`"aaa-profile"`/`"dot1xauth"`/`"macauth"`/`"server-groups"` config-assignment `profile-type` literal against a live read (§2.1/§5) before reclassifying any of them as `exact`.
- [ ] For each `conditional` secured-WLAN row (New Central `wpa2_personal`/`wpa3_sae`/`enhanced_open`; Classic `wpa3_sae`; Classic WPA3-Enterprise dry-run-only), run a live `apply()` with a real transient passphrase/auth-server reference and confirm the read-back before reclassifying any of them as `exact` (§6.2).
- [ ] Confirm the §2.1 config-assignment divergence against a live read before trusting any role/SHARED-object assignment path.
- [ ] Confirm WPA3 transition (`wpa3-transition-mode-enable`) and RadSec (`auth-server type=RADSEC`) field-level behavior against a live WLAN/auth-server read before reclassifying either as `exact`.
- [ ] Run the Gateway policy subset through a disposable create/read-back/delete lifecycle and compare the returned rule order, condition, source/destination, and action before removing `dry_run_only`.
- [ ] Confirm the Gateway IPv4/IPv6 static-route destination write contract (`prefix-vrf-nexthop-id`, `forwarding-type`) against a live Gateway read before reclassifying routes as `exact`.
- [ ] Confirm VRRP VLAN-interface attachment and tracking field mapping against a live Gateway VRRP read before reclassifying VRRP as anything but `unsupported`.
- [ ] Confirm `aos8_verify_migration_run`'s role config-assignment check (`scope-id`/`device-function`/`profile-type`/`profile-instance` via `list_config_assignments`) against a live read before trusting its `assignment_verification` result -- it is independent of, and can legitimately disagree with, the role object's own field verification.
- [ ] Record every supported, lossy, blocked, and unverifiable finding back into this matrix (update classifications, never silently widen scope in adapter code without a matching matrix update).
- [ ] Run `scripts/evaluate_aos8_070_disposable_lifecycle.py --mode read` then `--mode write` (§10.2) against a disposable lab scope for each adapter-mapping-verified kind before reclassifying its row past `conditional`/`blocked`; `route`/`vrrp`/`ap_group` remain refused until a real adapter mapping exists to test in the first place.
- [ ] Exercise `aos8_plan_migration_rollback`/`aos8_execute_migration_rollback` (§10.1) against a disposable/lab migration run's applied candidates and confirm the verified inverse operations actually restore prior state before ever relying on rollback execution operationally.

## 9. Related documentation

- [`docs/product-workflows.md`](product-workflows.md) — AOS8 migration tool roadmap (`aos8_migration_plan`, `aos8_preview_migration_run`/`aos8_create_migration_run`/`aos8_apply_migration_run`, `aos8_verify_migration_run`) that this matrix gates.
- [`docs/aos8-live-dryrun-evaluation.md`](aos8-live-dryrun-evaluation.md) — the `aos8-live-dryrun-eval` todo's sanitized, read-only live/fixture-backed evaluation record (2026-07-25) that corrected the two WLAN-security prose staleness findings above.
- [`docs/capability-gap-matrix.md`](capability-gap-matrix.md) — ranked practical gap #1 ("Broader verified migration mappings and live evaluation") tracks the same scope at a summary level; this file is the detailed contract behind that ranked gap.
- `src/hpe_networking_mcp/pipeline/aos8_schema.py`, `src/hpe_networking_mcp/pipeline/aos8_parsers.py`, `src/hpe_networking_mcp/pipeline/aos8_migration.py`, `src/hpe_networking_mcp/pipeline/aos8_target_adapters.py` — the implementation this matrix constrains.
- `src/hpe_networking_mcp/pipeline/aos8_rollback.py` — the reverse-dependency-order rollback/compensation planning and separately-gated execution module described in §10.
- `scripts/evaluate_aos8_070_disposable_lifecycle.py` — the credential-gated disposable create/read-back/delete lifecycle harness described in §10.
- `tests/unit/test_aos8_parsers.py`, `tests/unit/test_aos8_migration.py`, `tests/unit/test_aos8_target_adapters.py`, `tests/unit/test_aos8_migration_orchestrator.py`, `tests/unit/test_aos8_export_and_migration_tool.py`, `tests/unit/test_aos8_rollback.py`, `tests/unit/test_evaluate_aos8_070_disposable_lifecycle.py` — current regression coverage; every row moved to `exact` in a future revision must gain a corresponding test here.

## 10. Rollback/compensation planning and disposable-lifecycle harness (`v07-aos8-promotion`)

### 10.1 Rollback/compensation (`src/hpe_networking_mcp/pipeline/aos8_rollback.py`)

Every mapping's `CandidateAction.delete_operations` (New Central) /
`.rollback_operations` (Classic) has existed as **non-executable reference
metadata only** since the 0.5 release (§2.1/§5; `_action_preview`'s
`"rollback_supported": False`, unchanged by this revision — see that
field's own updated docstring comment). `src/hpe_networking_mcp/pipeline/aos8_rollback.py` adds a
real, **separately gated** execution path on top of that same metadata,
without changing `BaseCentralTargetAdapter.execute`/`dry_run`'s own
behavior (they still never invoke rollback themselves) and without adding a
`"rolled_back"` candidate status to `AOS8MigrationOrchestrator`'s own
`apply()` state machine (rollback progress is tracked separately, per run,
in `run["rollback"]["resume_state"]`).

- **Planning** (`plan_rollback`): given a set of previously-*applied*
  candidates and the exact same `adapter.candidate_action` mapping function
  used at apply time, orders them in reverse dependency order (a candidate
  is rolled back before anything it depends on) and classifies each as
  `supported=True` (a verified `delete_operations`/`rollback_operations`
  exists) or `supported=False` with an explicit, specific reason. **`vlan`
  candidates are always refused** — `NewCentralAdapter._map_vlan` sets
  neither `delete_operations` nor `rollback_operations` — this is the
  canonical example of requirement 4's "preserve precise blockers" instead
  of guessing an inverse operation that has never been verified.
- **Execution** (`execute_rollback_plan`): mirrors
  `BaseCentralTargetAdapter.execute`'s gates — real (non-dry-run) execution
  requires `confirmation=True` **and** the dedicated
  `HPE_MCP_AOS8_ROLLBACK_WRITES=1` gate (`rollback_writes_enabled()`),
  a gate distinct from, and required in addition to, whichever ordinary
  per-target write gate (e.g. `HPE_MCP_CENTRAL_WRITES`) the caller's
  `write_invoker` itself already enforces.
  `RollbackConflictPolicy.ABORT` (default) stops at the first
  failed/refused step and marks every later step `"not_attempted"`;
  `CONTINUE` attempts every remaining step regardless. `resume_from`
  supports resumable, idempotent retries: any step already recorded
  `"applied"` is skipped (`"already_applied"`) rather than re-issuing a
  delete.
- **Orchestrator/MCP integration**: `AOS8MigrationOrchestrator.rollback_plan`/
  `.execute_rollback` (`src/hpe_networking_mcp/pipeline/aos8_migration_orchestrator.py`) load a
  persisted run's `"applied"` candidates, build the target adapter the same
  way `apply()` does, and additionally require the ordinary per-target
  write gate (`adapter.writes_enabled(...)`) *and* the rollback-specific
  gate before any real execution — three independent authorizations, none
  sufficient alone. `aos8_plan_migration_rollback` (read-only) and
  `aos8_execute_migration_rollback` (destructive, `dry_run=True` by
  default) are the corresponding MCP tools
  (`src/hpe_networking_mcp/mcp_servers/aos8.py`); `execute_rollback` persists only
  `{candidate_key: "applied"}` resume-state, never a target secret.
- **Test coverage**: `tests/unit/test_aos8_rollback.py` (pure planning/
  execution unit tests: ordering, refusal, gates, conflict policy,
  resumability) and the rollback-specific tests appended to
  `tests/unit/test_aos8_migration_orchestrator.py` (full create→apply→
  rollback-plan→execute-rollback round trip against the existing
  `FakeBackend` harness, including the dual-gate and resume-state checks).

### 10.2 Disposable-write lifecycle harness (`scripts/evaluate_aos8_070_disposable_lifecycle.py`)

A credential-gated, bounded create/read-back/delete round-trip harness for
the New Central target object families named in requirement 3 of the
`v07-aos8-promotion` todo: auth server, server group, AAA profile, dot1x
and MAC-auth device profiles, and role scope+device-function
config-assignments (`assignment`, reusing the already-verified `role`
mapping's assignment lifecycle rather than inventing a separate assignment-
only object family) all have a real, adapter-mapping-backed lifecycle;
`route`, `vrrp`, and `ap_group` are explicitly refused for both read and (for
`ap_group`) write, citing their specific evidence gap from §6.11-§6.13,
rather than being silently skipped.

Gating uses `src/hpe_networking_mcp/pipeline/live_test_config.py` (platform `"central"`, since
every object family here lives on the New Central *target* account that
`_platform_writes_allowed("central")`/`get_client()` already write to for
ordinary migration applies — never the AOS8 *source* platform key): `--mode
status` never makes a network call; `--mode read` requires
`HPE_MCP_LIVE_TEST_CENTRAL_READ=1`; `--mode write` additionally requires
`HPE_MCP_LIVE_TEST_CENTRAL_WRITE=1`, `--confirm`, and a
`--lab-prefix`-prefixed `--lab-name`.

A disposable-write round trip deliberately drives the target adapter's
`CandidateAction.operations`/`.read_back_operation`/`.delete_operations`
directly instead of going through `adapter.execute(...)`: every one of
these five object types is *permanently* `status="blocked"` through the
normal `execute()` path today
(`BaseCentralTargetAdapter._assignment_write_blocker` — §6.3/§6.4/§6.5/§6.7/
§6.8 all cite it) specifically pending the disposable
create/assign/read-back/unassign/delete lab round trip this harness exists
to perform; the harness *is* that controlled round trip, so it must reach
past the standing blocker, not be stopped by it.

Per requirement 3, this harness is **implemented for later confirmed
execution and was not run live by this todo** — every test in
`tests/unit/test_evaluate_aos8_070_disposable_lifecycle.py` uses a fake
read/write invoker; no network call, credential, or live tenant was
touched. `live_test_status("central")` at the time of writing reports
`read_enabled: false`, `write_enabled: false`. Once a live pass is recorded
(read-back matched, cleanup confirmed), the corresponding row(s) in §6.3/
§6.4/§6.5/§6.7/§6.8/§6.9 may be reclassified per §8's checklist — this
harness does not itself change any classification in this matrix.

### 10.3 Staged/batch apply planning (`aos8_migration_batch_plan`)

`aos8_migration_batch_plan` (`src/hpe_networking_mcp/mcp_servers/aos8.py`) is a new, purely
additive, read-only report tool: it reuses
`aos8_migration_dependency_plan`'s existing per-`apply_order` stage
grouping and further chunks each stage into ordered batches of at most
`batch_size` candidates (default 10) — a staged/incremental review-and-apply
unit. It never calls `aos8_apply_migration_run` or any write tool itself and
never changes that tool's own per-candidate, dependency-ordered execution —
candidates within a stage are still applied individually, in the same
deterministic `(apply_order, object_type, identifier)` order
`build_migration_plan` already produces. This satisfies requirement 6
("staged/batch apply planning and report metadata integration without
changing default write behavior") as additive planning metadata layered on
the existing plan, not a change to `apply()`'s default behavior.
