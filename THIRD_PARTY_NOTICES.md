# Third-party notices

hpe-networking-mcp is MIT licensed. Generated API manifests and implementation patterns
also depend on upstream projects and vendor documentation with their own terms.

## Mist OpenAPI

The generated Mist operation manifest is derived from
[`mistsys/mist_openapi`](https://github.com/mistsys/mist_openapi), pinned to
version 2606.1.1 at commit
`f374cffdd5a275c7954645a306fcab7f1227e7a3`.

That repository is distributed under the MIT License. Preserve its copyright
and license notices when redistributing derived generated metadata.

## hpe-networking-mcp reference implementation

Generated-tool architecture and platform operation coverage were compared
against
[`nowireless4u/hpe-networking-mcp`](https://github.com/nowireless4u/hpe-networking-mcp),
an MIT-licensed community project. hpe-networking-mcp uses its public implementation as
reference material while retaining independent clients, registration, routing,
and response handling. The local `mcp_servers/skills` runbook engine is an
independent adaptation of that project's skills discovery pattern (list/load
markdown procedures with YAML frontmatter), with hpe-networking-mcp-specific tool names
and workflows.

## Vendor API specifications

Aruba, HPE GreenLake, ClearPass, ArubaOS, EdgeConnect, UXI, Apstra, and Axis
API specifications and documentation may be subject to vendor portal or product
terms and are not automatically covered by hpe-networking-mcp's MIT license.

hpe-networking-mcp does not commit vendor raw specifications unless redistribution is
explicitly permitted. Generated manifests contain operation metadata needed by
the runtime and record source provenance/digests. Users remain responsible for
accessing vendor APIs under the applicable product and documentation terms.
