# Performance benchmark harness

Run from `backend/` with the development environment active:

```bash
NBM_SECRET_KEY=<valid-fernet-key> AUTHENTICATION_DISABLED=True \
  python -m bench.bench --label baseline
```

The default fake latency is 30 ms and the synthetic repository contains 2,000 device types with
48 interfaces each. Results are written to `bench/results/<label>.json`. Use `--latency`,
`--repo-size`, `--migration-scale`, and `--migration-rps` only when intentionally defining a separate
profile; the JSON records these inputs. The migration scenarios run the production executor against
an on-disk SQLite job at the production four requests/second default, comparing bulk, per-item, and
SQLite `synchronous=OFF` modes. The fakes never contain or log real credentials.

The harness invokes production repository, pynetbox, search, fleet, push, streaming archive, and
rate-limiter code. It is intentionally not copied by the Dockerfile.

Regenerate the measured documentation after producing `after-remaining.json`:

```bash
python -m bench.render_docs
```
