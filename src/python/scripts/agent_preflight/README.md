---
description: Check AgentSync's complete target set before creating any native links.
---

# Agent preflight

`just agents::apply` runs this zero-dependency Python script before native
AgentSync. It reads `.agents/agentsync.toml`, preserves authored destinations,
rejects unexpected links and symlinked parents, and accepts correct managed
links on repeated runs. Canonical source paths must stay inside the repository
without symlink aliases. No configuration is written by this check.

PEP 723 metadata and `script.py.lock` keep this utility independent of the
optional Python application workspace. Its interpreter comes from Devenv.
