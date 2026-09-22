from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

_SLASHES = re.compile(r"/{2,}")
_CONTROLS = re.compile(r"[\x00-\x1f\x7f]")


class ParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ParsedEvent:
    timestamp: float
    host: str
    path: str
    first_path: str
    status: int
    bytes_sent: int
    visitor: bytes


def normalize_host(value: str) -> str:
    value = value.strip().lower()
    try:
        parsed = urlsplit(f"//{value}")
        host = parsed.hostname or ""
    except ValueError as exc:
        raise ParseError("invalid request host") from exc
    if not host:
        raise ParseError("missing request host")
    return host


def normalize_path(uri: str) -> tuple[str, str]:
    try:
        raw_path = urlsplit(uri).path
        decoded = unquote(raw_path, errors="replace")
    except (TypeError, ValueError) as exc:
        raise ParseError("invalid request URI") from exc
    decoded = _CONTROLS.sub("�", decoded)
    path = _SLASHES.sub("/", decoded or "/")
    if not path.startswith("/"):
        path = f"/{path}"
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    segment = path.lstrip("/").split("/", 1)[0]
    first_path = f"/{segment}" if segment else "/"
    return path, first_path


def parse_line(line: bytes | str, key: bytes) -> ParsedEvent:
    try:
        record = json.loads(line)
        request = record["request"]
        timestamp = float(record["ts"])
        remote_ip = str(request["remote_ip"])
        host = normalize_host(str(request["host"]))
        path, first_path = normalize_path(str(request.get("uri", "/")))
        status = int(record.get("status", 0))
        size = max(0, int(record.get("size", 0)))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ParseError(str(exc)) from exc
    if not remote_ip:
        raise ParseError("missing remote IP")
    visitor = hashlib.blake2b(
        remote_ip.encode("utf-8", "surrogatepass"), key=key, digest_size=16
    ).digest()
    return ParsedEvent(timestamp, host, path, first_path, status, size, visitor)