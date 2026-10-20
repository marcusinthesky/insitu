# Insitu for Node.js

This wrapper runs the same native executable as the Cargo and Python packages.
It does not implement a separate JavaScript engine or download code at install time.

**This checkout is an uncompiled validation candidate, not a published release.**
Platform packages must be built, tested and published before publishing the wrapper.
The npm scope is proposed; ownership has not been verified.

After an approved release: `npx @marcusinthesky/insitu sync`.
Do not omit optional dependencies: they carry the matching native executable.
Supported package targets: Linux glibc x64/arm64, macOS x64/arm64, Windows x64.
Python extensions additionally require Python and the `insitu` Python SDK.
`INSITU_BINARY_PATH` explicitly selects a locally trusted executable for testing.
