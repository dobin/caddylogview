# caddyparser

Compact, privacy-oriented traffic analytics for newline-delimited Caddy JSON access logs. The importer consumes completed gzip rotations and stores only UTC hourly aggregates by domain and first path section. It never persists individual requests or full URLs.

## Storage model

For every `(UTC hour, domain, first path section)` group, SQLite stores:

- exact hit count;
- exact transmitted-byte total;
- a mergeable HyperLogLog sketch for estimated unique visitors.

Visitor sketches use keyed hashes of client IP addresses and precision `p=12`, with an expected standard error of approximately 1.6%. Visitor values are estimates; hits and bytes are exact. Raw IP addresses, headers, cookies, query strings, full paths, original JSON records, and per-request hashes are not stored.

A small consumed-file table prevents a completed rotation from being counted twice. Its identity is the SHA-256 digest of decompressed content, so renamed or recompressed copies are still skipped. Distinct rotation files are assumed not to overlap, as expected with normal Caddy rotation. Duplicate lines inside one new rotation represent separate logged requests and are counted separately.

## Setup

```shell
uv sync --dev
```

This schema is intentionally incompatible with earlier request-level databases. Move or delete an existing `stats.sqlite3`, then replay the retained `.log.gz` rotations into a fresh database.

## Recommended Caddy configuration

Rotate on every UTC clock hour and retain enough files for the importer to catch up after an outage:

```caddyfile
example.com {
    log {
        output file /var/log/caddy/access.log {
            roll_minutes 0
            roll_size 100MiB
            roll_keep 168
            roll_keep_for 168h
        }
        format json
    }
}
```

Caddy enables gzip compression by default. `roll_minutes 0` rolls on the next write at or after the start of each hour; `roll_size` may rotate sooner under heavy traffic. The example retains up to 168 rotations and seven days, with both limits applying. Increase retention beyond the maximum expected updater outage.

Caddy requires a server restart to apply changed options for an existing file output; a configuration reload alone does not apply those output changes.

## Use

Import completed rotations and build the report:

```shell
uv run caddyparser update /var/log/caddy \
  --database stats.sqlite3 \
  --output report
```

Directory discovery reads only `*.log.gz`. The active `access.log`, uncompressed `.log` files, and unrelated files are intentionally ignored. Dashboard freshness therefore follows the rotation schedule.

Import and report generation can also run separately:

```shell
uv run caddyparser import /var/log/caddy --database stats.sqlite3
uv run caddyparser build --database stats.sqlite3 --output report
```

Run `update` shortly after each hourly rotation, for example from a systemd timer or cron at minute 5:

```cron
5 * * * * cd /opt/caddyparser && uv run caddyparser update /var/log/caddy --database stats.sqlite3 --output /srv/www/traffic
```

Aggregate updates and the consumed-file marker commit in one SQLite transaction. A crash, corrupt gzip stream, or strict parse error leaves the rotation unconsumed and safe to retry. In default non-strict mode malformed records are reported and omitted, while the otherwise valid rotation is consumed.

Serve the generated directory over HTTP; browsers commonly block report JSON requests from `file://` pages:

```shell
uv run python -m http.server 8000 --directory report
```

Open <http://localhost:8000>.

## Metrics and ranges

- **Hit:** one structurally valid access-log record.
- **Visitor:** an approximate distinct keyed client-IP hash after merging HyperLogLog sketches across the selected hours and sections. Hourly estimates are never simply added.
- **Bytes:** Caddy's non-negative response `size` value.
- **Section:** normalized domain plus the first decoded path element, such as `example.com/docs`. Query strings and fragments are discarded.
- **Ranges:** `1d`, `1m`, `6m`, `12m`, and `all`, aligned to UTC hour boundaries because request-level timestamps are not retained.

The report provides domain summaries, top domains, top sections, bandwidth, and aggregate timelines. Full-URL rankings are intentionally unsupported.

## Operational limitations

- Only completed gzip rotations are ingested; the active file is not read.
- The updater must run before Caddy removes unconsumed rotations.
- Whole duplicate files are detected, but partial overlap between two different rotations cannot be detected without retaining request-level identities.
- Historical aggregate data cannot be reconstructed into request-level data.
- Keep the SQLite database private. Publish only the generated report directory.
