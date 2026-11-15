# Public API resource boundaries

The HTTP API accepts inline `profile_data` only. Non-null `profile` values are
rejected before starting a process, including local names and absolute paths.
The CLI's local profile workflow remains unchanged. API request files contain
an inline profile object, use private OS temporary files, and are removed after
success, validation failure or timeout. They never use the personal `profiles/`
directory. Abrupt process/host termination can leave OS temp files behind.

`RequestLimits` caps `/simulate` and `/dildoro/boss` together at two active
requests and 120 accepted starts per rolling minute per API process. It rejects
excess requests with 429 and Retry-After, without an unbounded waiting queue.
It counts actual streamed request bytes (512 KiB maximum), and allows 15 seconds
to receive a body. Health checks and CORS preflight bypass this limiter.
Simulation duration is limited to 600 simulated seconds; the existing 60-second
subprocess wall-clock timeout remains in force. Option lists and strings are
bounded, non-finite numbers and CLI option-like strings are rejected.

Public responses no longer include executable paths, temporary request paths,
or internal failure tracebacks. The legacy `cmd` and `stderr` response fields
remain present but empty. `profile_used` is `inline` or null.

These are resource bounds, **not member authentication**. CORS does not stop
non-browser callers. All callers share the limits; increasing workers/replicas
multiplies capacity, and a distributed attack can still exhaust it. Use one
Uvicorn worker with the current Render setup. Stronger public-service protection
requires an authenticated gateway and/or an upstream distributed limiter.

Validation before deployment: `python -m runner.doclint`,
`python -m runner.snapshot`, `python -m unittest discover -s tests` with API
dependencies installed (including the test-only HTTP client). API security tests
cover path rejection, size/duration limits, chunked bodies, concurrency, rate
window expiration, inline-profile cleanup and response redaction. No production
load testing is needed to run these tests.
