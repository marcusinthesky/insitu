# Security model

This is an uncompiled, unaudited rewrite candidate. Do not use it on irreplaceable
documents without version control and backups.

Extensions are trusted subprocesses with the invoking user's environment and
filesystem privileges. Manifest permissions restrict `host.resource.get` only;
they do not sandbox a process, prevent network access, or prevent direct writes.
Check mode does not commit host patches, but cannot stop a malicious extension
from writing on its own. Do not install untrusted extensions.

The source includes path confinement, rejection of symlink output replacements,
source snapshot checks, staged per-file atomic replacement, overlap/cycle checks,
read-only SQLite authorization and bounded RPC frames. Those mechanisms still
need native tests and security review. Canonical-path checks are not an OS-level
race-free filesystem capability system. Multi-file updates are not crash-atomic;
best-effort rollback can fail. Inputs changing during a pass do not constitute a
transactional project snapshot. Subprocess queues and computation are not protected
by comprehensive memory/CPU quotas. There is no claim of hostile-input hardening.

Package installation checks content hashes against insitu.lock. A content hash
establishes consistency with a lockfile, not that the locked code is trustworthy.
Remote installation uses a user-provided Git source and requires Git on PATH.
`cargo-deny` checks advisories, licenses and sources on every dependency change.
No penetration test, fuzz campaign or signed release has occurred.
