# Provenance and replay

Public base: `yung13yubabie/line-summary`, branch `fix/core-identity-readonly-wal-20261013`, head `637fbcc4400dd374b718a07749306dfe58817c2f`, tree `0fa628d5e69713ff0dc4c3ba5847f08327272fe0`. The isolated verification directory reconstructed all 59 public blobs and matched that tree byte-for-byte. Its synthetic local base commit is not the original Git history; the remote save must use the actual public head as parent and a non-forced expected-head update.

The preserved product22 hashes match both the accepted source11 and disabled-preview source. The two preview files and three validation helpers also match their recorded originals. New wrappers and tests are recorded separately in MANIFEST.json. No original checkout or review package was modified.

Original patch bytes:

- `patches/MCP_SCHEMA.patch`: SHA256 `f109ed769af2ed82cf532a2c77b32a25784c9b619b78f1611aedbb6adb620fa5`.
- `patches/PROVIDER_MCP_DELTA.patch`: SHA256 `ae39470fec91d35a1ca1f63b0d5aece98c283aa0174244314d5082b2bbceafb8`.

These are alternative replay routes, not sequential patches. Independent byte-only validation reconstructed the 21-file EOF baseline for the first route and 16-file provider baseline for the second, then forward-applied each original patch to obtain the exact same final source22 hashes. Reverse-check, reverse, forward-check and forward-apply each exited zero. No product code or old tests ran during that verification.

To repeat only the byte check, copy `source/` into a fresh scratch directory, preserve line endings, and run `git -c core.autocrlf=false apply --reverse --check <absolute-original-patch>`, then reverse-apply, forward-check and forward-apply the same patch in that scratch directory. Compare every final file with `source22_sha256` in MANIFEST.json. Never apply either replay to the repository root or an active checkout.

The historical disabled-preview metadata patch does not replay raw against its original LF baseline because the patch has CRLF differences. That remains a recorded FAIL; the exact final preview files are saved instead. It is not included among the two successful product replay routes. Original full packages and raw receipts remain local and are not copied into this public repository.
