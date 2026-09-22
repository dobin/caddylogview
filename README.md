# caddyparser

Compact traffic analytics for newline-delimited Caddy JSON access logs. The importer streams active logs into a local SQLite cache and builds a static HTML dashboard containing only aggregates.

## Setup

```shell
uv sync --dev
```

## Use

Import one or more logs and build the report:

```shell
uv run caddyparser update access.log --database stats.sqlite3 --output report
```

A directory can be supplied to import all `*.log` and `*.log.gz` files in it. Rotated
files are processed by name and `access.log` is processed last:

```shell
uv run caddyparser update /var/log/caddy --database stats.sqlite3 --output report
```

Compressed gzip logs are read directly. Byte-identical records encountered under a
different rotated filename are imported only once.

Repeated runs import only complete lines appended since the previous run. Rename/create and copy-truncate rotations are detected. Import and build can also be run separately:

```shell
uv run caddyparser import access.log --database stats.sqlite3
uv run caddyparser build --database stats.sqlite3 --output report
```

Serve the generated folder locally (browsers usually block JSON requests from `file://` pages):

```shell
uv run python -m http.server 8000 --directory report
```

Open <http://localhost:8000>.

## Metrics

- **Hit:** one structurally valid access-log record, regardless of response status or client type.
- **Visitor:** one distinct keyed hash of `request.remote_ip` in a selected range or time bucket. NAT and changing addresses make this an estimate.
- **Bytes:** Caddy's non-negative response `size` value.
- **Content:** query strings and fragments are discarded; paths are decoded and normalized. Rankings cover domain, domain plus first path element, and domain plus full normalized path.
- Ranges and chart labels use UTC and end at the newest imported event. This makes reports from historical logs reproducible.

## Privacy

Raw IP addresses, request/response headers, cookies, query strings, and original JSON records are never persisted. Visitor hashes use a random key stored only in the SQLite database. Do not publish the database; publish only the generated report folder.

All requests count, including bots and internal clients. Bot detection and response-status filtering are outside the first version.