from __future__ import annotations

import math
import struct

PRECISION = 12
REGISTER_COUNT = 1 << PRECISION
SPARSE_LIMIT = 256
_MAGIC = b"CPH1"
_SPARSE = 0
_DENSE = 1


class HyperLogLog:
    """A mergeable HyperLogLog sketch with a stable application-owned format."""

    __slots__ = ("precision", "_sparse", "_dense")

    def __init__(self, precision: int = PRECISION) -> None:
        if precision < 4 or precision > 16:
            raise ValueError("HyperLogLog precision must be between 4 and 16")
        self.precision = precision
        self._sparse: dict[int, int] | None = {}
        self._dense: bytearray | None = None

    @property
    def register_count(self) -> int:
        return 1 << self.precision

    def add(self, digest: bytes) -> None:
        if len(digest) < 8:
            raise ValueError("HyperLogLog input must contain at least 64 hash bits")
        bits = len(digest) * 8
        value = int.from_bytes(digest, "big")
        index = value >> (bits - self.precision)
        remainder_bits = bits - self.precision
        remainder = value & ((1 << remainder_bits) - 1)
        rank = remainder_bits + 1 if remainder == 0 else remainder_bits - remainder.bit_length() + 1
        self._set(index, rank)

    def _set(self, index: int, rank: int) -> None:
        if self._sparse is not None:
            if rank > self._sparse.get(index, 0):
                self._sparse[index] = rank
                if len(self._sparse) > SPARSE_LIMIT:
                    self._promote()
            return
        assert self._dense is not None
        if rank > self._dense[index]:
            self._dense[index] = rank

    def _promote(self) -> None:
        dense = bytearray(self.register_count)
        assert self._sparse is not None
        for index, rank in self._sparse.items():
            dense[index] = rank
        self._sparse = None
        self._dense = dense

    def merge(self, other: HyperLogLog) -> HyperLogLog:
        if self.precision != other.precision:
            raise ValueError("cannot merge HyperLogLog sketches with different precision")
        if other._sparse is not None:
            for index, rank in other._sparse.items():
                self._set(index, rank)
        else:
            assert other._dense is not None
            if self._sparse is not None:
                self._promote()
            assert self._dense is not None
            for index, rank in enumerate(other._dense):
                if rank > self._dense[index]:
                    self._dense[index] = rank
        return self

    def estimate(self) -> float:
        if self._sparse is not None:
            zero_count = self.register_count - len(self._sparse)
            inverse_sum = zero_count + sum(2.0**-rank for rank in self._sparse.values())
        else:
            assert self._dense is not None
            zero_count = self._dense.count(0)
            inverse_sum = sum(2.0**-rank for rank in self._dense)
        count = self.register_count
        if count == 16:
            alpha = 0.673
        elif count == 32:
            alpha = 0.697
        elif count == 64:
            alpha = 0.709
        else:
            alpha = 0.7213 / (1 + 1.079 / count)
        estimate = alpha * count * count / inverse_sum
        if estimate <= 2.5 * count and zero_count:
            return count * math.log(count / zero_count)
        return estimate

    def count(self) -> int:
        return max(0, round(self.estimate()))

    def to_bytes(self) -> bytes:
        header = _MAGIC + bytes((self.precision, _SPARSE if self._sparse is not None else _DENSE))
        if self._sparse is not None:
            entries = sorted(self._sparse.items())
            body = struct.pack(">H", len(entries)) + b"".join(
                struct.pack(">HB", index, rank) for index, rank in entries
            )
            return header + body
        assert self._dense is not None
        return header + bytes(self._dense)

    @classmethod
    def from_bytes(cls, value: bytes) -> HyperLogLog:
        if len(value) < 6 or value[:4] != _MAGIC:
            raise ValueError("invalid HyperLogLog serialization")
        precision, mode = value[4], value[5]
        sketch = cls(precision)
        if mode == _SPARSE:
            if len(value) < 8:
                raise ValueError("truncated sparse HyperLogLog")
            count = struct.unpack(">H", value[6:8])[0]
            if len(value) != 8 + count * 3 or count > sketch.register_count:
                raise ValueError("invalid sparse HyperLogLog length")
            previous = -1
            for offset in range(8, len(value), 3):
                index, rank = struct.unpack(">HB", value[offset : offset + 3])
                if index <= previous or index >= sketch.register_count or rank == 0:
                    raise ValueError("invalid sparse HyperLogLog register")
                sketch._sparse[index] = rank  # type: ignore[index]
                previous = index
            if count > SPARSE_LIMIT:
                sketch._promote()
            return sketch
        if mode == _DENSE:
            if len(value) != 6 + sketch.register_count:
                raise ValueError("invalid dense HyperLogLog length")
            sketch._sparse = None
            sketch._dense = bytearray(value[6:])
            return sketch
        raise ValueError("unsupported HyperLogLog serialization mode")


def union_bytes(existing: bytes | None, incoming: bytes) -> bytes:
    sketch = HyperLogLog.from_bytes(existing) if existing is not None else HyperLogLog()
    sketch.merge(HyperLogLog.from_bytes(incoming))
    return sketch.to_bytes()
