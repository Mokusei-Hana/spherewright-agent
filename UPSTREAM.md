# Inspected upstream identities

Inspected on 2026-11-10. These are source review identities, not installed-game validation.

| Repository | Inspected revision | Purpose |
|---|---|---|
| [Arcueid77/Spherewright](https://github.com/Arcueid77/Spherewright/tree/19cd5eb505ffd30f4832f3bfbf0ced9ddad4a889) | `19cd5eb505ffd30f4832f3bfbf0ced9ddad4a889` | Current docs, stdio host, tool schemas, contracts, native handcraft coordinator |
| [Spherewright v0.4.0](https://github.com/Arcueid77/Spherewright/tree/v0.4.0) | `b7c0002dbe2e8f5608c5ca140b0c6326a1818021` | Checked handcraft tool names and action/session contracts against formal baseline |
| [etalam/dsp-agent](https://github.com/etalam/dsp-agent/tree/ae9751dc47074ebf3497b2dcb4f4ea286d702202) | `ae9751dc47074ebf3497b2dcb4f4ea286d702202` | README and current Mock Agent scope; no source copied |

Spherewright sources reviewed: `src/Spherewright.Mcp/Program.cs`, `Tools/SpherewrightTools.cs`,
`Contracts/Actions/NormalActionContracts.cs`, `ActionResultContracts.cs`, session/player/catalog/journal
contracts, `Plugin/Game/NormalGameActionCoordinator.cs`, release installation and Agent playbook.

The native coordinator's `UpdateHandcraft` waits for the exact forge task to leave the queue and
requires all runtime recipe products in inventory. This client separately binds the original action,
session, planet and idempotency key, retains native item deltas, checks product deltas, and fresh-reads.
It does not infer completion from commit acceptance, wall time, a Journal first-event entry or later
backpack motion. No game files, tokens, Bridge descriptors or upstream code are redistributed.
