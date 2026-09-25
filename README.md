# caddylogview

Compact, privacy-oriented traffic analytics for newline-delimited Caddy JSON access logs. The importer consumes completed gzip rotations and stores only UTC hourly aggregates by domain and first path section. It never persists individual requests or full URLs.

This is 100% vibe coded. Its only for my usecase for `r00ted.ch` stats: Simple, space efficient, fast, static html (secure). 

It converts `/var/log/caddy/*` into `.html`.


## Setup

```shell
uv sync --dev
```


## Caddy configuration

Rotate on every UTC clock hour and retain enough files for the importer to catch up after an outage:

```caddyfile
example.com {
    log {
        output file /var/log/caddy/access.log {
            roll_minutes 0
            roll_size 1000MiB
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
uv run caddylogview update /var/log/caddy \
  --database stats.sqlite3 \
    --output report \
    --site-domain r00ted.ch
```

Directory discovery reads only `*.log.gz`. The active `access.log`, uncompressed `.log` files, and unrelated files are intentionally ignored. Dashboard freshness therefore follows the rotation schedule.

Import and report generation can also run separately:

```shell
uv run caddylogview import /var/log/caddy --database stats.sqlite3
uv run caddylogview build --database stats.sqlite3 --output report
```


### cron

Run `update` shortly after each hourly rotation, for example from a systemd timer or cron at minute 5:

```cron
5 * * * * cd /opt/caddylogview && uv run caddylogview update /var/log/caddy --database stats.sqlite3 --output /srv/www/traffic
```

### systemd timer

For a system-wide timer that runs as Caddy's service user, create `/etc/systemd/system/caddylogview-update.service`. Replace `/var/lib/caddy/.local/bin/uv` with the absolute path returned by `sudo -u caddy -H command -v uv`.

```ini
[Unit]
Description=Update Caddy traffic report

[Service]
Type=oneshot
User=caddy
Group=caddy
WorkingDirectory=/var/lib/caddy
Environment=HOME=/var/lib/caddy
Environment=PATH=/var/lib/caddy/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/var/lib/caddy/.local/bin/uv run caddylogview update /var/log/caddy --database stats.sqlite3 --output /srv/www/traffic
```

Then create `/etc/systemd/system/caddylogview-update.timer`:

```ini
[Unit]
Description=Run the Caddy traffic report updater hourly

[Timer]
OnCalendar=*-*-* *:05:00
Persistent=true

[Install]
WantedBy=timers.target
```

`OnCalendar=*-*-* *:05:00` runs at five minutes past every hour, allowing the hourly rotation to complete first. `Persistent=true` runs a missed invocation after boot.

Ensure the `caddy` user can read `/var/log/caddy`, write the database and report directories, and traverse `/opt/caddylogview`. Load and enable the timer, then test it immediately:

```shell
sudo systemctl daemon-reload
sudo systemctl enable --now caddylogview-update.timer
sudo systemctl start caddylogview-update.service
sudo systemctl status caddylogview-update.service
sudo journalctl -u caddylogview-update.service --since today
```

Check the next scheduled run with `systemctl list-timers caddylogview-update.timer`. Do not enable both this timer and the cron entry.

Aggregate updates and the consumed-file marker commit in one SQLite transaction. A crash, corrupt gzip stream, or strict parse error leaves the rotation unconsumed and safe to retry. In default non-strict mode malformed records are reported and omitted, while the otherwise valid rotation is consumed.

Serve the generated directory over HTTP; browsers commonly block report JSON requests from `file://` pages:

```shell
uv run python -m http.server 8000 --directory report
```

Open <http://localhost:8000>.


## Storage model

For every `(UTC hour, domain, first path section)` group, SQLite stores:

- exact hit count;
- exact transmitted-byte total;
- a mergeable HyperLogLog sketch for estimated unique visitors.

Visitor sketches use keyed hashes of client IP addresses and precision `p=12`, with an expected standard error of approximately 1.6%. Visitor values are estimates; hits and bytes are exact. Raw IP addresses, headers, cookies, query strings, full paths, original JSON records, and per-request hashes are not stored. Referrer URLs are reduced to their hostname and aggregated by hour; full referrer URLs are not retained.

A small consumed-file table prevents a completed rotation from being counted twice. Its identity is the SHA-256 digest of decompressed content, so renamed or recompressed copies are still skipped. Distinct rotation files are assumed not to overlap, as expected with normal Caddy rotation. Duplicate lines inside one new rotation represent separate logged requests and are counted separately.



## Metrics and ranges

- **Hit:** one structurally valid access-log record.
- **Visitor:** an approximate distinct keyed client-IP hash after merging HyperLogLog sketches across the selected hours and sections. Hourly estimates are never simply added.
- **Bytes:** Caddy's non-negative response `size` value.
- **Section:** normalized domain plus the first decoded path element, such as `example.com/docs`. Query strings and fragments are discarded.
- **Ranges:** `1d`, `1m`, `6m`, `12m`, and `all`, aligned to UTC hour boundaries because request-level timestamps are not retained.

The report provides domain summaries, top domains, top sections, top 20 external referrers, bandwidth, and aggregate timelines. `--site-domain` controls the first-party domain excluded from referrer rankings and defaults to `r00ted.ch`. Full-URL rankings are intentionally unsupported.


## Operational limitations

- Only completed gzip rotations are ingested; the active file is not read.
- The updater must run before Caddy removes unconsumed rotations.
- Whole duplicate files are detected, but partial overlap between two different rotations cannot be detected without retaining request-level identities.
- Historical aggregate data cannot be reconstructed into request-level data.
- Keep the SQLite database private. Publish only the generated report directory.
