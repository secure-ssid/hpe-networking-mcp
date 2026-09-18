# What Makes a Good MCP Server?

A good Model Context Protocol (MCP) server is a narrow, trustworthy bridge between an AI client and real systems. It turns a user or agent's intent into bounded, useful action without making them repeat context or guess how the system works.

## Core promise

```text
AI client / agent → Good MCP server → Real systems
  intent + context      bounded action       data, services, workflows
```

## Quality pillars

### Clear tools

- Keep tools small, predictable, and composable.
- Use unsurprising names, inputs, and outputs.
- Expose only capabilities that solve a real task.

### Relevant context

- Provide focused resources and schemas.
- Include the context needed to make a good decision.
- Avoid exposing unrelated data or asking the user to repeat known context.

### Safety boundaries

- Use least privilege by default.
- Validate inputs and make risky actions explicit.
- Require consent when an action can materially change a system.

### Reliable behavior

- Handle timeouts and partial failures clearly.
- Return useful errors and recovery guidance.
- Make results trustworthy enough for an agent to act on.

### Interoperable contracts

- Follow the standard protocol.
- Keep contracts stable and evolve them predictably.
- Make version and capability changes understandable.

### Developer experience

- Provide concise documentation, examples, and easy setup.
- Make the first successful connection quick.
- Give clear feedback when setup, access, or a request fails.

## Desired outcomes

- **Useful:** Solves a real, frequent user or agent need and exposes only the context and operations required.
- **Trustworthy:** Risky actions are explicit and constrained; failures are clear enough to recover from.
- **Easy to adopt:** Names, inputs, outputs, examples, and contracts are stable and easy to understand.

## Minimal-prompt networking example

### 1. The operator asks naturally

> How does the network look?

### 2. The MCP discovers scope first

The MCP checks which organizations, sites, and permissions are available.

- If there is one clear scope, it continues automatically.
- If multiple scopes match, it asks one concrete question:

> I found 2 sites and 3 organizations. Which organization or site should I review?

### 3. The MCP returns a useful health overview

The review should summarize:

- Current issues and their severity
- Impacted users or locations
- Recommended next actions
- Access point health, coverage, and failures
- Client experience, weak signal, and disconnects
- Switch ports, power, uplinks, and errors
- SSID usage, authentication, and quality

### 4. The operator drills into a target

Instead of asking the original question again, the MCP offers focused choices:

- Switch
- Access point
- Client
- SSID

When the operator selects one, the MCP carries the chosen organization or site forward automatically and returns an updated review with:

- Target-specific issues
- Likely causes
- Affected users or devices
- Recommended next actions

### Principle

Ask only when the answer changes the next action. Never make the operator repeat the original request or previously selected scope.

## Easy configuration and privilege management

### API key setup

1. Provide one clear API-key field.
2. Store the key as a secret; never show it again or write it to logs.
3. Test the connection immediately.
4. Report a clear success or failure result with a next step.

### Safe starting access

- Default to the smallest useful role.
- Scope access to the selected organization or site.
- Clearly show what the MCP can do and what remains unavailable.

### Optional privilege elevation

Provide an explicit **Enable more access** option rather than silently requesting broad permissions.

Before elevation, show:

- Which extra capabilities will be added
- Which organizations, sites, or device types will be affected
- Whether the new access includes read-only operations, configuration changes, or other higher-risk actions

Then require explicit confirmation. Elevated access should be optional, reversible, and auditable.

## Configuration flow

```text
Add API key
  → Store as a secret
  → Test connection
  → Choose starter access
  → Apply least privilege
  → Ready to use
  → Optional: request more access
  → Review added capabilities
  → Confirm and apply approved elevation
```

## Design rule

Make the common safe path effortless. Make broader access easy to request, but impossible to grant accidentally.
