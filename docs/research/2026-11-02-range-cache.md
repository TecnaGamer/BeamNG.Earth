# Range-cache reliability and duplicate-transfer audit

Baseline: `8b0e9b04`, preserved as `baseline/modernization-20261102`.
Scope: `server/rangeread.py`, offline range regression tests, and the HTTPS test double.

## Findings and implementation

| Finding | Severity / reproduction | Change |
|---|---|---|
| A second process finishes a block between the cache lookup and process-lock acquisition; the new owner downloads it again. | Medium; deterministic interleaving, occurrence frequency during gameplay unknown. Increases provider traffic and stalls a reader unnecessarily. | Recheck each block while holding the process lock; fetch only the remaining consecutive runs. Existing priority bypass and host policies are preserved. |
| A missing intermediate block is omitted when assembling a read, shifting subsequent bytes to incorrect offsets. A missing final block also returns a partial success. | High data-integrity consequence; injected missing-block regression, no confirmed in-game incident. | Raise `RangeError` before returning incomplete bytes. Recompute the valid endpoint when a first response discovers the file size so legitimate EOF remains supported. |
| The response's range endpoint was not checked, and an invalid response could persist an invalid learned file size. | Medium; malformed-response fixture. | Validate the complete Content-Range and body length before writing either blocks or learned size. |
| HTTPS connection mock rejects the `context` keyword on Windows with the required truststore dependency installed. | Medium CI reliability; four baseline test/subtest errors in the complete dependency environment. | Match the HTTPS constructor's context parameter and explicitly test context forwarding without depending on the host OS. No TLS verification is weakened. |

Content-Range checks follow [RFC 9110 section 14.4](https://www.rfc-editor.org/rfc/rfc9110.html#section-14.4).
The TLS mock signature was checked with Context7 against the
[CPython HTTP client implementation](https://github.com/python/cpython/blob/main/Lib/http/client.py).

No new dependencies, provider allowance increases, cache layout changes, or protocol changes.
Malformed range responses that previously slipped through now fail explicitly; existing caller error handling applies.

## Reproduction and benchmark

```sh
python -B -m unittest discover -s server/tests -p test_rangeread.py -v
python -B -m unittest discover -s server/tests -p test_mapdata.py -q
python -B -m unittest discover -s server/tests -p test_netfetch.py -q
python tools/benchmark_range_cache.py --baseline-ref baseline/modernization-20261102
```

The first regression suite produced **six failures out of eight tests** before the production change and passed all eight afterward. The tests force the race using an injected lock-acquisition boundary, including both a partial cache fill and a complete cache fill, and verify lock cleanup. They also cover incomplete reads, malformed headers, first-response EOF discovery, and warm reads.

Controlled benchmark measured on Windows 11 build 26200, Python 3.14.7, AMD Ryzen 7 7700X (8 cores / 16 threads). Fifteen iterations per implementation, sixteen 256 KiB blocks (4 MiB), 30 ms simulated latency per request, temporary local disk cache. The producer completes immediately before the second reader acquires the process lock. The script loads the baseline implementation directly from the specified local Git ref and verifies identical output bytes for both versions.

| Forced-race metric | Baseline | Updated |
|---|---:|---:|
| Requests per iteration | 2 | 1 |
| Transferred bytes per iteration | 8,388,608 | 4,194,304 |
| Median elapsed time (ms) | 134.637 | 71.301 |

These results show removal of the duplicate transfer **when this race occurs**. They are not cold/warm world-load measurements, network throughput estimates, or evidence of improved BeamNG frame times. Timing is sensitive to filesystem and concurrent machine load; request and byte counts are the deterministic performance assertions. No external provider was contacted by this benchmark.

## Remaining validation

Run a cold-cache drive and a warm-cache repeat with real BeamNG/BeamMP, recording world-ready latency, range-request count, transferred bytes, and frame-time percentiles. Compare with the same route, settings, provider state, and hardware. Linux runtime validation and real engine validation remain pending.
