# Typed product workflow roadmap

The optional product backends remain opt-in and lab-friendly, but now cover the
verified operational families needed for migration, assurance, NAC, posture,
and guarded configuration workflows. The low-token router keeps this broader
catalog out of client context until `find_tool` selects a workflow.

Use this page as the implementation roadmap for typed tools that should graduate
from generic GET exploration into named MCP workflows.

The ArubaOS 8-to-Classic/New Central migration rows below are gated by the authoritative source-to-target contract in [`aos8-migration-contract-matrix.md`](aos8-migration-contract-matrix.md). Any family that matrix classifies `manual`/`unsupported` remains blocked for automatic writes regardless of what appears here.

## Catalog snapshot

| Surface | Tool count |
|---|---:|
| Central generated + curated configuration, monitoring, NAC, and operations | 1,924 |
| GreenLake Platform | 1,011 |
| RAG/OpenAPI | 12 |
| Optional products, read-only annotated | 1,773 |
| Optional products, guarded writes included | 3,757 |
| **Platform API backend catalog** | **6,704** |
| `design-core` + `interop-core` (credential-free, no vendor API) | 12 |
| **Complete backend catalog** | **6,716** |

## Promotion rule

Promote a generic GET pattern to a typed tool when it is:

| Signal | Why it matters |
|---|---|
| Repeated in real troubleshooting | Saves prompt tokens and user time |
| Easy to type safely | Clear parameters and bounded output |
| Useful across tenants | More than a one-off lab endpoint |
| Write/destructive | Needs explicit MCP annotations and confirmation |

## GreenLake Platform implemented starters

| Workflow | Tool | Notes |
|---|---|---|
| Device, user, audit, and workspace detail | `get_glp_device_by_id` / `get_glp_user` / `get_glp_audit_log_detail` / `get_glp_workspace` / `get_glp_workspace_contact` | Official GLP read-only detail wrappers layered on the existing guarded GLP client |
| Reporting status | `list_glp_reporting_statuses` / `get_glp_reporting_status` | Bounded reporting status lookup from `/reporting/v1/statuses` |
| Service catalog | `list_glp_service_offers` / `get_glp_service_offer` / `list_glp_service_provisions` / `get_glp_service_provision` | Cursor-bounded service catalog read workflows, including optional workspace header for service provisions |
| Service managers | `list_glp_service_managers` / `get_glp_service_manager` / `list_glp_service_manager_provisions` / `get_glp_service_manager_provision` / `list_glp_per_region_service_managers` / `get_glp_service_managers_for_region` | Official service-manager and per-region service-manager views |
| v2beta1 devices and grouping | `list_glp_devices_v2` / `get_glp_device_v2` / `group_glp_devices` | Current device inventory plus documented grouping by make, model, source, category, device type, and other supported attributes; GLP does not expose a device-group resource by ID |
| Audit Logs v2beta1 | `list_glp_audit_logs_v2` / `get_glp_audit_log_v2` / `get_glp_audit_log_v2_detail` | Bounded audit event search and detail |
| Workspace contact and subscriptions | `update_glp_workspace_contact` / `glp_add_subscriptions` | Feature-gated writes with dry-run and confirmation |
| RBAC role assignments and scope groups | `list_glp_role_assignments` / `get_glp_role_assignment` / `list_glp_scope_groups` / `get_glp_scope_group` / `list_glp_scope_group_scopes` | 22 high-frequency typed GLP reads promoted from generic exploration; RBAC/IAM detail and scope-group membership |
| Events, webhooks, and deliveries | `list_glp_event_webhooks` / `get_glp_event_webhook` / `list_glp_event_subscriptions` / `list_glp_webhook_deliveries` | Bounded event subscription, webhook, and delivery-history reads |
| Locations and tags | `list_glp_locations` / `get_glp_location` / `reverse_geocode_glp_location` / `list_glp_location_tags` / `get_glp_location_tags` / `list_glp_tags` / `list_glp_tag_resources` | Location inventory, reverse geocoding, and tag/resource association reads |
| SCIM users, groups, and membership | `list_glp_scim_users` / `get_glp_scim_user` / `list_glp_scim_groups` / `get_glp_scim_group` / `list_glp_scim_group_users` / `list_glp_scim_user_groups` | Bounded SCIM identity reads for users, groups, and group/user membership |
| Compute Ops Management inventory and jobs | `list_glp_compute_servers` / `get_glp_compute_server` / `list_glp_compute_server_alerts` / `list_glp_compute_groups` / `list_glp_compute_jobs` | Region-aware (`GLP_GENERATED_REGION`); iLO-managed server inventory, per-server alerts, groups, and firmware/config job status |
| Storage Fleet and Block Storage inventory | `list_glp_storage_systems` / `get_glp_storage_system` / `list_glp_storage_system_types` / `list_glp_block_storage_volumes` / `get_glp_block_storage_volume` / `list_glp_block_storage_hosts` | Region-aware (data.cloud.hpe.com hosts); cross-device-type storage system, volume, and host-initiator inventory |
| Virtualization inventory and guarded VM power | `list_glp_virtual_machines` / `get_glp_virtual_machine` / `list_glp_hypervisor_managers` / `list_glp_hypervisor_clusters` / `list_glp_datastores` / `set_glp_virtual_machine_power` / `set_glp_virtual_machines_power_bulk` | VM/hypervisor/datastore inventory; guarded power-on/off with dry-run/confirm, plus a bounded (max 20) bulk composite with per-VM partial-failure reporting |
| Backup & Recovery status and guarded run-now | `list_glp_backup_protection_jobs` / `get_glp_backup_protection_job` / `list_glp_backup_protection_stores` / `list_glp_backup_storeonces` / `list_glp_backup_vm_protection_groups` / `run_glp_backup_protection_job` | Protection job/store/StoreOnce/VM-protection-group status; guarded run-protection-job-now with dry-run/confirm |
| Data Services issues and async operations | `list_glp_data_services_issues` / `get_glp_data_services_issue` / `list_glp_data_services_async_operations` / `list_glp_data_services_storage_locations` | Cross-resource health/status feed, async-operation job tracking, and storage-location inventory |
| Read-only cross-resource reconciliation | `plan_glp_reconciliation` | Bounded, read-only planning composite over devices/subscriptions/users/RBAC role assignments/scope groups/audit logs/reporting statuses; flags likely drift, never writes |

## ClearPass implemented starters

| Workflow | Tool | Notes |
|---|---|---|
| Check endpoint by MAC | `clearpass_get_endpoint_by_mac` | Normalize MAC input and return compact endpoint/profile/status fields |
| List recent auth failures | `clearpass_list_auth_failures` | Bound by `limit` / `offset`; include username, MAC, NAD, reason |
| Show NAD status | `clearpass_get_network_device` | Useful for RADIUS/TACACS troubleshooting |
| Find guest by email/name | `clearpass_find_guest` | Read-only lookup only |
| Insight endpoint data | `clearpass_get_insight_endpoint` | Documented `/api/insight/endpoint/mac/{mac}` lookup |
| OnGuard activity | `clearpass_list_onguard_activity` / `clearpass_get_onguard_activity_by_mac` | Documented activity inventory and per-MAC lookup |
| Access Tracker session search | `clearpass_list_access_tracker_sessions` / `clearpass_get_access_tracker_session` | General bounded `/api/session` search by any `auth_status` (or none) plus by-ID lookup; complements the FAILED-only `clearpass_list_auth_failures` |
| Endpoint inventory | `clearpass_list_endpoints` | Bounded `/api/endpoint` list |
| Guest inventory | `clearpass_list_guests` | Bounded `/api/guest` list |
| Policy elements | `clearpass_list_roles` / `clearpass_list_enforcement_policies` / `clearpass_get_enforcement_policy` | Read-only `/api/role` and `/api/enforcement-policy` views |
| Service management | `clearpass_list_services` / `clearpass_get_service` | Read-only `/api/config/service` views |
| Syslog export | `clearpass_list_syslog_targets` / `clearpass_list_syslog_export_filters` | Read-only `/api/syslog-target` and `/api/syslog-export-filter` views |
| Diagnostics | `clearpass_get_server_version` / `clearpass_list_cluster_servers` | Read-only `/api/server/version` and `/api/cluster/server` views |

## Mist implemented starters

| Workflow | Tool | Notes |
|---|---|---|
| List org sites | `mist_list_sites` | Return site IDs/names/timezone only by default |
| Client lookup by MAC | `mist_get_client` | Compact client health, AP, WLAN, RSSI/SNR |
| Site WLAN summary | `mist_list_wlans` | Bound output for model context |
| Recent site alarms | `mist_list_alarms` | Severity/time bounded |
| NAC and user MACs | `mist_list_nac_tags` / `mist_list_nac_portals` / `mist_list_nac_idps` / `mist_list_user_macs` | Access Assurance and Cloud RADIUS context |
| Marvis client troubleshooting | `mist_search_marvis_clients` / `mist_get_client_insights` / `mist_search_events` | Official Mist OpenAPI 2606.1.1 paths |
| Wired and WAN Assurance | `mist_list_switches` / `mist_list_switch_ports` / `mist_list_gateways` / `mist_get_gateway` | Unified device-stat workflows |
| Org inventory | `mist_list_org_inventory` | Omits claim secrets from output |
| Diagnostic result collection | `mist_collect_diagnostic_results` | Bounded authenticated regional WebSocket collection from `/api-ws/v1/stream`, correlated by `session_id` and capped by event count, byte size, and elapsed time; requires the `websockets>=14.0` dependency |
| Org/site SLE assurance summary | `mist_get_org_sle_overview` / `mist_get_site_sle_metric_summary` | Bounded typed reads of the confirmed `/insights/{metric}` and `/sle/{scope}/{scope_id}/metric/{metric}/summary` endpoints |

## ClearPass implemented lab writes

| Workflow | Tool | Notes |
|---|---|---|
| Generic lab write | `clearpass_write` | Guarded POST/PUT/PATCH/DELETE to `/api/*`; dry-run default |
| Access Tracker disconnect | `clearpass_disconnect_session` | Destructive `/api/session/{id}/disconnect`; dry-run default |
| Guest create | `clearpass_create_guest` | Guarded `/api/guest` create; password redacted in previews |
| Service enable/disable | `clearpass_set_service_enabled` | Guarded PATCH to the confirmed enable/disable endpoints |
| Endpoint attributes | `clearpass_update_endpoint_attributes` | Patch endpoint attributes by MAC; optional CoA query flag |
| Delete endpoint | `clearpass_delete_endpoint` | Destructive endpoint delete by MAC |
| Enable/disable guest | `clearpass_set_guest_enabled` | Patch guest enabled state by username or ID |
| Delete guest | `clearpass_delete_guest` | Destructive guest delete by username or ID |
| Generated Agentless OnGuard writes | Discover `clearpass_agentless_on_guard_*` tools | Current 6.12.7 specification-derived settings and subnet-mapping operations |

## Mist implemented lab writes

| Workflow | Tool | Notes |
|---|---|---|
| Generic lab write | `mist_write` | Guarded POST/PUT/PATCH/DELETE to `/api/v1/*`; dry-run default |
| Ack site alarm | `mist_ack_alarm` | POST site alarm acknowledgement |
| Unack site alarm | `mist_unack_alarm` | POST site alarm unacknowledgement |
| Delete WLAN | `mist_delete_wlan` | Destructive site WLAN delete |
| Claim devices | `mist_claim_devices` | Guarded claim-code submission with masked previews |
| User MAC and Marvis settings | `mist_upsert_user_mac` / `mist_set_marvis_settings` | Guarded NAC and Marvis configuration |

## Apstra implemented starters

| Workflow | Tool | Notes |
|---|---|---|
| List blueprints | `apstra_list_blueprints` | IDs/names/state only |
| List design templates | `apstra_list_templates` | Compact template inventory from `/api/design/templates` |
| Blueprint anomalies | `apstra_list_anomalies` | Read-only fabric health |
| Blueprint racks | `apstra_list_racks` | Compact rack topology from `/api/blueprints/{id}/racks` |
| Blueprint routing zones | `apstra_list_routing_zones` | Compact security-zone/VRF view from `/api/blueprints/{id}/security-zones` |
| Blueprint virtual networks | `apstra_list_virtual_networks` | Compact VN/subnet/binding view from `/api/blueprints/{id}/virtual-networks` |
| Blueprint remote gateways | `apstra_list_remote_gateways` | Compact remote EVPN gateway view from `/api/blueprints/{id}/remote_gateways` |
| Blueprint connectivity templates | `apstra_list_connectivity_templates` | Compact policy summary from `/api/blueprints/{id}/endpoint-policies` |
| Blueprint application endpoints | `apstra_list_application_endpoints` | Compact CT attachment-point view from `/api/blueprints/{id}/obj-policy-application-points` |
| Blueprint diff status | `apstra_get_diff_status` | Compact staging-vs-active status from `/api/blueprints/{id}/diff-status` |
| Blueprint protocol sessions | `apstra_list_protocol_sessions` | Compact protocol/BGP session status from `/api/blueprints/{id}/protocol-sessions` |
| Blueprint system info | `apstra_get_system_info` | Compact systems/devices from `/api/blueprints/{id}/experience/web/system-info` |
| Generic lab write | `apstra_write` | Guarded POST/PUT/PATCH/DELETE to `/api/*`; dry-run default |
| Session authentication | `apstra_login` | Uses current `/api/aaa/login`, falling back to older `/api/user/login` only on 404/405 |
| Connectivity-template lifecycle | `apstra_get_connectivity_template` / `apstra_create_connectivity_template` / `apstra_delete_connectivity_template` | Current `endpoint-policies` and `obj-policy-import` paths from official SDK 6.1.2 |
| Application-point assignment | `apstra_set_application_point_assignment` | Guarded `/obj-policy-batch-apply` workflow |
| Async task monitoring | `apstra_get_task` / `apstra_wait_for_task` | Poll blueprint tasks through `/tasks/{task_id}` to succeeded, failed, or timeout |

(v0.7) Top-level resource pools, device/rack profiles, system agents,
telemetry, and blueprint-scoped IBA are exposed as generated tools
(`apstra_list_ip_pools`/`apstra_create_ip_pool`/..., `apstra_list_device_profiles`/...,
`apstra_list_system_agents`/..., `apstra_list_telemetry_service_registry_entries`/...,
`apstra_list_blueprint_iba_dashboards`/...) from the same pinned `aos-sdk-api`
6.1.2.post1 SDK; see `scripts/_apstra_operations.py` and
`src/hpe_networking_mcp/mcp_servers/openapi_gen/provenance/apstra.json`'s `coverage_gaps` for the
handful of verbs (rack-type create, telemetry-collector delete, device-profile
digest writes, IBA widgets/import/export) the pinned SDK does not model.

## ArubaOS 8 implemented starters

| Workflow | Tool | Notes |
|---|---|---|
| Show command | `aos8_show_command` | Only permits `show ...` commands via `/v1/configuration/showcommand` |
| Controller inventory | `aos8_list_controllers` | Bounded root-scope `show switches` read |
| Software version | `aos8_get_version` | Bounded root-scope `show version` read |
| License inventory | `aos8_list_licenses` | Bounded root-scope `show license` read |
| AP inventory | `aos8_list_aps` | Bounded `show ap database` read scoped by `config_path` |
| Active APs | `aos8_list_active_aps` | Bounded `show ap active` read scoped by `config_path` |
| Client visibility | `aos8_list_clients` | Bounded `show user-table` read scoped by `config_path` |
| Client lookup | `aos8_find_client` | Bounded `show user-table` lookup by exactly one MAC, IP, or username |
| Client detail | `aos8_get_client_detail` | Bounded verbose `show user-table verbose mac` read scoped by `config_path` |
| Client association history | `aos8_get_client_history` | Bounded root-scope `show ap association history client-mac` read |
| Active alarms | `aos8_get_alarms` | Bounded `show alarms` read scoped by `config_path` |
| Audit trail | `aos8_get_audit_trail` | Bounded root-scope `show audit-trail` read |
| Events | `aos8_get_events` | Bounded `show events` read scoped by `config_path` |
| MD hierarchy | `aos8_get_md_hierarchy` | Bounded root-scope `show configuration node-hierarchy` read |
| RF neighbors | `aos8_get_rf_neighbors` | Bounded `show ap arm-neighbors ap-name` read scoped by AP name and `config_path` |
| Cluster state | `aos8_get_cluster_state` | Bounded root-scope `show lc-cluster group-membership` read |
| AP wired ports | `aos8_get_ap_wired_ports` | Bounded root-scope `show ap port status ap-name` read for one AP |
| IPsec tunnel state | `aos8_get_ipsec_tunnels` | Bounded root-scope `show crypto ipsec sa` read |
| System logs | `aos8_get_system_logs` | Bounded root-scope `show log system` diagnostic read with capped count |
| ARM history | `aos8_get_ap_arm_history` | Bounded `show ap arm history` RF diagnostic read scoped by `config_path` |
| AP monitor stats | `aos8_get_ap_monitor_stats` | Bounded `show ap monitor stats` RF diagnostic read scoped by `config_path` |
| BSS table | `aos8_list_bss` | Bounded `show ap bss-table` read scoped by `config_path` |
| Radio summary | `aos8_get_radio_summary` | Bounded `show ap radio-summary` read scoped by `config_path` |
| AP-group inventory | `aos8_list_ap_groups` | Configuration-object read scoped by `config_path` |
| SSID profile summary | `aos8_list_ssid_profiles` | Configuration-object read scoped by `config_path` |
| Virtual AP profiles | `aos8_list_virtual_aps` | Configuration-object read scoped by `config_path` |
| User roles | `aos8_list_user_roles` | Configuration-object read scoped by `config_path` |
| Generic lab write | `aos8_write` | Guarded GET/POST to `/v1/*`; AOS8 config mutations use POST plus `_action` |
| SSID profile lab write | `aos8_manage_ssid_profile` | Create/update/delete `ssid_prof` objects; dry-run default; returns write-memory hint |
| Virtual AP lab write | `aos8_manage_virtual_ap` | Create/update/delete `virtual_ap` objects; dry-run default; returns write-memory hint |
| AP group lab write | `aos8_manage_ap_group` | Create/update/delete `ap_group` objects; dry-run default; returns write-memory hint |
| User role lab write | `aos8_manage_user_role` | Create/update/delete `role` objects with `rolename`; dry-run default; returns write-memory hint |
| VLAN lab write | `aos8_manage_vlan` | Create/update/delete `vlan_id` objects; dry-run default; returns write-memory hint |
| Persist staged AOS8 config | `aos8_write_memory` | POST write-memory for an affected `config_path`; dry-run default |
| Session lifecycle | `aos8_login` / `aos8_logout` | Preferred UIDARUBA/X-CSRF session flow; legacy token remains a compatibility fallback |
| Migration export | `aos8_get_vlans` / `aos8_get_policies` / `aos8_export_wlans` / `aos8_export_all` | Exhaustive local paging plus WLAN, role, VLAN, AP group, controller, policy, AAA server/profile, route, and VRRP inventory |
| Classic/New Central migration plan | `aos8_migration_plan` | Candidate schemas, warnings, deterministic diffs, and verification steps without target writes |
| Resumable migration run | `aos8_preview_migration_run` / `aos8_create_migration_run` / `aos8_apply_migration_run` | Atomic per-candidate state, dry-run-first confirmed writes, dependency-aware resume/retry, and ephemeral target secrets |
| Migration run status | `aos8_get_migration_run` / `aos8_list_migration_runs` | Bounded persisted status, partial results, retryability, and exact checkpoint/rollback guidance |
| Migration verification | `aos8_verify_migration_run` | Bounded, read-only per-candidate verification: `verified`/`partially_verified`/`failed`/`unsupported`/`not_applied`/`unverifiable`, deterministic indexed array comparison, secret fields always reported unverifiable (never mismatch), a role's config-assignment tuple verified independently of its library object (object and assignment can disagree), and a bounded read-only presence diagnostic (never a verified/applied claim) for blocked candidates (auth-server/AAA/server-group SHARED-assignment mappings) |

## EdgeConnect implemented starters

| Workflow | Tool | Notes |
|---|---|---|
| Appliance inventory | `edgeconnect_list_appliances` | IDs/names/site/status only |
| Appliance system info | `edgeconnect_get_system_info` | Model/version/status/alarm summary from `/rest/json/systemInfo` |
| Appliance alarms | `edgeconnect_list_alarms` | Outstanding alarms from `/rest/json/alarm`, bounded by `limit` / `offset` |
| Appliance interface state | `edgeconnect_get_interface_state` | Compact interface admin/oper/IP/speed view from `/gms/rest/interfaceState`, scoped by appliance `nePk` |
| Interface labels | `edgeconnect_list_interface_labels` / `edgeconnect_set_interface_labels` / `edgeconnect_apply_interface_labels` | Compact WAN/LAN interface-label read, guarded complete-label-map lab write, and guarded push-to-appliance action |
| Appliance bypass mode | `edgeconnect_get_bypass_mode` / `edgeconnect_set_bypass_mode` | Compact bypass actual/config state plus guarded lab toggle to `/gms/rest/bypass` |
| Link integrity diagnostics | `edgeconnect_get_link_integrity_status` / `edgeconnect_run_link_integrity_test` | Compact status plus guarded iperf/tcpperf diagnostic start at `/gms/rest/linkIntegrityTest/*` |
| Appliance disk report | `edgeconnect_get_disk_report` | Compact disk/storage health view from `/gms/rest/configReportDisk`, scoped by appliance `nePk` |
| Appliance reachability | `edgeconnect_get_appliance_reachability` | Compact reachability from `/gms/rest/reachability/{appliance,gms,gms2}`, scoped by appliance `nePk` |
| Fleet reachability | `edgeconnect_list_appliance_reachability` | Compact all-appliance reachability from `/gms/rest/reachability/gms2/appliancesReachability` |
| Overlay configuration | `edgeconnect_list_overlays` | Compact overlay configs from `/gms/rest/gms/overlays/config`, with optional overlay ID filter |
| Overlay priority | `edgeconnect_get_overlay_priority` | Compact overlay priority order from `/gms/rest/gms/overlays/priority` |
| Topology link status | `edgeconnect_get_topology_link_info` | Sparse topology link status from `/gms/rest/gms/topologyConfig/linkInfo/v2`, scoped by overlay ID |
| Route maps | `edgeconnect_get_route_maps` | Compact route policy settings from `/gms/rest/routeMaps`, scoped by appliance `nePk` |
| Route labels | `edgeconnect_list_route_labels` / `edgeconnect_set_route_labels` | Compact route-label read plus guarded lab write to `/gms/rest/routeLabels` |
| ACL address groups | `edgeconnect_list_address_groups` / `edgeconnect_set_address_group` / `edgeconnect_delete_address_group` | Compact ACL address-group read plus guarded create/update/replace/delete lab writes to `/gms/rest/ipObjects/addressGroup` |
| ACL service groups | `edgeconnect_list_service_groups` / `edgeconnect_set_service_group` / `edgeconnect_delete_service_group` | Compact ACL service-group read plus guarded create/update/replace/delete lab writes to `/gms/rest/ipObjects/serviceGroup` |
| Overlay internet services | `edgeconnect_list_services` / `edgeconnect_set_services` | Compact overlay internet-service read plus guarded complete-service-list lab write to `/gms/rest/gms/services` |
| Third-party services | `edgeconnect_list_third_party_services` | Compact built-in/custom third-party cloud services from `/gms/rest/gms/thirdPartyServices` |
| Firewall zones | `edgeconnect_list_zones` / `edgeconnect_set_zones` | Compact firewall-zone read plus guarded complete-map lab write to `/gms/rest/zones` |
| Zone-based firewall | `edgeconnect_get_zone_firewall_status` / `edgeconnect_set_zone_firewall_status` | Read and guarded lab write for End-to-End Zone-Based Firewall status at `/gms/rest/zones/eeEnable` |
| Zone ID allocation | `edgeconnect_get_next_zone_id` / `edgeconnect_set_next_zone_id` | Read and guarded lab write for next firewall-zone ID at `/gms/rest/zones/nextId` |
| VRF zone maps | `edgeconnect_list_vrf_segment_zones` / `edgeconnect_list_vrf_zone_map` | Compact VRF-to-zone mappings from `/gms/rest/zones/vrfSegmentZonesMap` and `/gms/rest/zones/vrfZonesMap` |
| Tunnel health | `edgeconnect_list_tunnels` | Physical tunnel status from `/gms/rest/tunnels2/physical`, with optional filters |
| Tunnel metadata | `edgeconnect_get_tunnel_metadata` | Compact tunnel count metadata from `/gms/rest/tunnels2?metaData=true` |
| VRF/routing segments | `edgeconnect_list_vrf_segments` | Compact routing-segment inventory from `/gms/rest/vrf/config/segments`, with optional segment ID filter |
| Network role and site | `edgeconnect_get_appliance_network_role_site` / `edgeconnect_set_appliance_network_role_site` | Compact appliance network-role/site read plus guarded lab write to `/gms/rest/appliance/networkRoleAndSite` |
| Maintenance mode | `edgeconnect_get_maintenance_mode` / `edgeconnect_set_maintenance_mode` | Compact maintenance-mode read plus guarded lab write to `/gms/rest/maintenanceMode` |
| Persist appliance changes | `edgeconnect_save_changes` | Guarded lab write to `/gms/rest/appliance/saveChanges`, dry-run default and `confirm=True` required |
| Generic lab write | `edgeconnect_write` | Guarded POST/PUT/PATCH/DELETE to Orchestrator REST paths; dry-run default |
| API compatibility diagnosis | `edgeconnect_doctor` | Probes live Orchestrator API/Swagger metadata and reports legacy-gate status |
| Alarm acknowledge/clear | `edgeconnect_acknowledge_alarm` / `edgeconnect_clear_alarm` | Guarded confirmed `/alarm/acknowledgement/gms` and `/alarm/clearance/gms` workflows |
| Alarm summary | `edgeconnect_alarm_summary` | Read-only confirmed `/alarm/summary` |
| Flow visibility | `edgeconnect_list_flows` / `edgeconnect_get_flow_stats` | Bounded confirmed `/flow` and `/stats/aggregate/flow` reads |

## Axis implemented starters

| Workflow | Tool | Notes |
|---|---|---|
| Split CRUD per entity | `axis_get_applications` / `axis_create_application` / `axis_update_application` / `axis_delete_application` (and 9 more top-level entities: application groups, connectors, connector zones, groups, locations, SSL exclusions, tunnels, users, web categories) | Generated from the reviewed 47-operation manifest; each verb is a distinct tool with exact write/destructive annotations, splitting the upstream fused `manage_entity` action |
| Staged commit | `axis_commit_changes` | Applies staged create/update/delete changes |
| Connector actions | `axis_regenerate_connector` | Guarded connector credential regeneration |
| Sub-locations | `axis_get_sub_locations` / `axis_create_sub_location` / `axis_update_sub_location` / `axis_delete_sub_location` | Nested-location split CRUD |
| Status | `axis_get_status` | Read-only backend status |

(v0.7) `scripts/evaluate_axis_lab.py` adds an always-on, offline split-CRUD
contract check (confirms all 10 entities have a complete
query/create/update/delete set), a bounded opt-in read-only live check
(`HPE_MCP_LIVE_TEST_AXIS_READ=1`), and a disposable-write **plan** (opt-in,
requires the read gate too) that is only ever generated and SHA-256-hashed —
never executed.

## UXI implemented starters

| Workflow | Tool | Notes |
|---|---|---|
| Backend status | `uxi_status` | Shows whether UXI OAuth client credentials are configured |
| Guarded UXI GET | `uxi_get` | Read-only GET limited to selected UXI list endpoints and `/sensors/{id}/status`; list payloads are bounded with `limit` / `offset` |
| Sensor inventory and status | `uxi_list_sensors` / `uxi_get_sensor_status` | Compact sensor identity/model/MAC/group/location fields plus online/testing status |
| Agent and group inventory | `uxi_list_agents` / `uxi_list_groups` | Compact agent and group reads with cursor-style pagination |
| Network and service-test inventory | `uxi_list_wired_networks` / `uxi_list_wireless_networks` / `uxi_list_service_tests` | Read-only network and service test views |
| Group assignments | `uxi_list_agent_group_assignments` / `uxi_list_sensor_group_assignments` / `uxi_list_network_group_assignments` / `uxi_list_service_test_group_assignments` | Assignment-list views for agents, sensors, networks, and service tests |

## UXI implemented lab writes

| Workflow | Tool | Notes |
|---|---|---|
| Generic guarded write | `uxi_write` | Only method/path combinations documented by the current UXI v1alpha1 specification |
| Group lifecycle | `uxi_create_group` / `uxi_update_group` / `uxi_delete_group` | Dry-run and confirmation gated |
| Sensor and agent lifecycle | `uxi_update_sensor` / `uxi_update_agent` / `uxi_delete_agent` | Guarded documented resource changes; the current API has no sensor DELETE |
| Assignments | `uxi_assign_sensor_to_group` / `uxi_assign_agent_to_group` / `uxi_assign_network_to_group` / `uxi_assign_service_test_to_group` | Explicit group-assignment workflows |

## Remaining optional typed candidates

EdgeConnect production use remains dependent on the target Orchestrator's live
`gmsApiInfo.json` / `vxoaApiInfo.json`. Apstra's 135-operation generated set
(v0.7) is SDK-derived rather than a full appliance OpenAPI export; its
`coverage_gaps` provenance field records the specific verbs the pinned SDK
does not model (rack-type create, telemetry-collector delete, device-profile
digest writes, IBA widgets/import/export, and the binary
streaming-telemetry-schema endpoint). UXI service tests have no
create/update/delete API at all (only list, and only their group assignment
is writable) — a permanent upstream omission, not a missing curated wrapper.
Continue promoting curated tools only after the vendor source or target
instance confirms the request shape.

## Ranked next capabilities

The next roadmap is based on current HPE Aruba Networking documentation and
public MCP/OpenAPI prior art. External projects are design references only;
vendor specifications and live target behavior remain authoritative.

| Rank | Capability | Recommended scope | Evidence |
|---:|---|---|---|
| 1 | Central Streaming API collector | Add a bounded, authenticated WSS collector for AP monitoring and geofence topics, including CloudEvents/protobuf decoding, event/byte/time caps, and an explicit Advanced-subscription prerequisite. | `developer_docs/developer_arubanetworks_com_new-central_docs_streaming-api-connection-management.md`, `developer_docs/developer_arubanetworks_com_new-central_docs_streaming-api-event-ap-monitoring.md` |
| 2 | Better low-token tool ranking | Add BM25 as a deterministic ranking layer beside the current keyword and semantic passes, while retaining compact results and the read/diagnostic/write/destructive gates. Do not replace the router with an upstream generic dispatcher that exposes full schemas or weakens write controls. | [FastMCP Tool Search](https://github.com/PrefectHQ/fastmcp/blob/main/docs/servers/transforms/tool-search.mdx) |
| 3 | Plain-language OpenAPI summaries | Add an endpoint-family summary tier between `find_tool` and exact `lookup_api` results so an agent can disambiguate a large API without loading full schemas. | [janwilmake/openapi-mcp-server](https://github.com/janwilmake/openapi-mcp-server) |
| 4 | Generated router trajectory evals | Extend `openapi_gen` to emit framework-neutral YAML smoke cases that grade tool selection, argument construction, correctness, and hallucination across `find_tool` -> dispatch trajectories. | [cnoe-io/openapi-mcp-codegen](https://github.com/cnoe-io/openapi-mcp-codegen) |
| 5 | Git-incremental vendor-source RAG | Index official SDKs and example repositories alongside documentation, recording repository URL and commit provenance and refreshing only changed files. | [kvncampos/nautobot_mcp](https://github.com/kvncampos/nautobot_mcp) |
| 6 | Telemetry-to-remediation plans | Correlate alerts, audit trails, and state-of-truth data into read-only drift findings, then produce dry-run remediation plans that require the existing confirmation and platform write gates before execution. | Concept demonstrated by [kiskander/mcp-splunk-meraki](https://github.com/kiskander/mcp-splunk-meraki); no license, so do not reuse its code |
| 7 | Generated-backend provenance parity | Normalize source-pin/provenance checks for Mist, Axis, and EdgeConnect, then promote high-value typed workflows such as EdgeConnect SD-WAN-AI sessions and Apstra blueprint commit/rollback. | `src/hpe_networking_mcp/mcp_servers/openapi_gen/provenance/`, `scripts/check_generated_tool_manifests.py` |

## Design constraints

1. Keep optional products opt-in via `HPE_MCP_PRODUCTS`.
2. Include both read and guarded write options for lab workflows where verified
   write endpoints are in scope.
3. Keep outputs compact and paginated.
4. Require explicit write/destructive annotations and confirmation for writes.
5. Keep product tokens in `.env`; do not duplicate them into MCP client configs.
6. Honor `safe-read-only` globally and the `custom`
   `HPE_MCP_PRODUCT_ACCESS=read-only` default by hiding/blocking optional
   product write tools.
