# Agent surfaces

The canonical source skill lives at [`skills/axio-stitching-pipeline/`](../skills/axio-stitching-pipeline/),
not here. That copy is the **dev-repo recipe** (it drives the `axio` CLI from a checkout);
the copy installed into an agent is *rendered* from it by
`axio_stitching/agent_integration.py :: render_installed_skill` and drives the MCP tools
instead, because an installed agent has no checkout to `cd` into.

To install the skill and register the MCP server into the agent platforms on this machine:

```bash
axio agent status            # what is detected, installed, or drifted
axio agent install --dry-run # every file and config key that would change
axio agent install           # do it
axio agent uninstall         # remove only what AXIO wrote, hash-verified
```

See [`docs/AGENT_INTEGRATION.md`](../docs/AGENT_INTEGRATION.md) for the per-platform paths
and the safety contract.
