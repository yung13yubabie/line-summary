# Synthetic schema MCP development candidate

This experimental Windows entry accepts ordinary MCP JSON-RPC as its first input. One empty-argument `line_schema_counts` tool connects an owned, generated encrypted DB/WAL/SHM fixture to the existing native provider and returns aggregate table/view counts. It supplies synthetic discovery and key candidates; it never opens a real LINE database or process. The four legacy data tools are removed from this entry.

`source/` is an exact copy of the previously reviewed 22-file candidate, not the repository root's product. The four `synthetic_*.py` files, `lifecycle_trace.py` and `tests/` are the experimental development layer. Root product files, dependency locks and workflow are unchanged. See [provenance](PROVENANCE.md), [manifest](MANIFEST.json), and [initial bounded results](RESULTS.json) and [reliability follow-up](RELIABILITY_RESULTS.json).

## Reproduce with an already approved runtime

Use Windows Python 3.11.9 and the existing approved environment containing MCP 1.29.0, AnyIO 4.15.1, Pydantic 2.13.5, and the existing APSW/Win32 dependencies. The commands do not install packages. Set `$PythonBase` to that base interpreter and `$ApprovedSite` to its approved environment's absolute `Lib\site-packages` directory. Start in this directory.

```powershell
& $PythonBase -I -S -B -m unittest discover -s tests -p 'test_*.py'
& $PythonBase -I -S -B tests/guard_probe.py
& $PythonBase -I -S -B tests/stdio_client.py $FreshOutputDirectory basic $ApprovedSite
```

Set `$FreshOutputDirectory` to a new child of the ignored `evidence/` directory for every run. The client starts exactly one suspended server, assigns its exact Job before resume, and sends initialize without a private preamble. Other finite client modes are `wrong_key`, `missing_wal`, `missing_shm`, `invalid_db`, `exception`, `cross_request`, `cancel`, `cancel_then_success`, `timeout`, `eof_idle`, and `eof_active`. Run native cases serially. Work/stage/inner cleanup budgets remain 30/25/3 seconds; initialize is bounded at 10 seconds. Test-client EOF allows 3.05 seconds cooperatively, then at most one second to prove exact forced cleanup. Forced cleanup never makes the case pass.

To run the synthetic server from a normal stdio client:

```powershell
& $PythonBase -I -S -B synthetic_stdio.py --site-packages $ApprovedSite --artifacts $OwnedOutputDirectory --case success
```

These trusted local CLI arguments are not MCP tool arguments. No key, settings, DB path, module or arbitrary command can be supplied through the tool. Fixtures and private settings exist only in newly owned session directories. Audit guards deny arbitrary processes, external sockets, private sources outside that session and actual LINE memory entry. The audit hook is a process-scoped test boundary, not an OS sandbox.

## Reliability follow-up

The initial `RESULTS.json` belongs to saved commit `0daf5afe03c1f67657960458882f3f3f0e3e5561`. Current reliability work and preserved failures are separate in `RELIABILITY_RESULTS.json`.

Fixed-phase observations now record startup/import CPU and wall time, EOF delivery, service close, MCP return, main/atexit, and exact child exit/Job-empty state. The client queries CPU totals only from its already retained child and Job handles; it never looks up a PID or opens another process. Diagnostic output contains fixed labels and typed numeric/boolean values. Detailed local clocks and receipts remain under owned output directories; the public report contains finite relative timings.

The observed runtime's deadline clock reports 15.625 ms resolution; diagnostic wall timestamps use its separate performance counter (reported nominal resolution 100 ns). Work and cleanup still use their original clocks and limits. Measurement consumes CPU/wall time, so it is not a zero-impact experiment and does not establish a slowdown cause.

Four bounded author probes produced one initialize timeout while the first MCP SDK import was unfinished, before provider startup; the same idle case also passed. Two cross-request probes returned correct counts and exited about 132-134 ms after EOF. No additional identical root probes were run. The SDK slowdown cause remains unknown, and the historical post-success EOF force was not reproduced.

Independent fake counterexamples found two defects in this new diagnostic layer. An output-sink failure could replace the original import exception, and a CPU-sampling failure could precede a wire failure in cleanup. The added regression cases preserve the primary import/wire errors and record diagnostic failures separately. Diagnostic-only failure before main returns makes an otherwise clean service exit 3. The client also requires a final owned snapshot ending at `server_atexit`, with zero emission failures or dropped events; a missing or unhealthy final snapshot rejects acceptance even if the process exited 0. These are harness fixes, not explanations of the historical startup or EOF failure.

## Result semantics and limits

Success requires `completed`, `successful=true`, `application_ack=true`, confirmed OS release and counts exactly `{table_count:2, view_count:1}`. A failure receipt may have `application_ack=true`; this acknowledges its terminal failure and does not mean the operation succeeded. Failure responses never contain counts. Wrong-key exhaustion, injected exception and candidate deadline currently report conservative `cleanup_unconfirmed` with no application ack even when the outer Job proves OS release. The extraction layer does not carry positive application cleanup evidence for those ownerless errors; this candidate does not infer it.

Cold-start import variability remains unresolved: earlier attempts exceeded the unchanged 10-second handshake limit. Successful warm attempts do not close those failures. Two sequential successful counts also had one recorded forced EOF cleanup. Preserve the failed receipts and assess later outcomes separately. Output checks cover stdout/stderr, split sentinels and arbitrary Windows drive paths. Local raw transport is not persisted; fixture values and local evidence are excluded from Git.

The prior 518 unit / 8 stdio results and disabled-preview acceptance are historical evidence for their own fixed versions. They do not validate this new bootstrap. `preview.py` and `preview_guard.py` preserve the earlier disabled entry; it requires a locally filled `runtime_contract.json` based on the example and remains separate from this synthetic chain. No ready-to-register host configuration is included.

Actual desktop-host registration, host MCP inventory, approvals, ChatGPT web/mobile, real data and deployment are NOT RUN. The visible host MCP-status failure, CLI endpoint errors and missing Computer Use tools are distinct observations, not proof of a shared cause. No trust/global setting, persistent credential, tunnel or shared-app restart was changed.
