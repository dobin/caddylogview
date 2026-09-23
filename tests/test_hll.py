import hashlib

import pytest

from caddylogview.hll import HyperLogLog, SPARSE_LIMIT, union_bytes


def digest(value: int) -> bytes:
    return hashlib.blake2b(str(value).encode(), digest_size=16).digest()


def test_duplicates_union_and_serialization():
    left = HyperLogLog()
    right = HyperLogLog()
    for value in range(1_000):
        left.add(digest(value))
        left.add(digest(value))
    for value in range(500, 1_500):
        right.add(digest(value))

    restored = HyperLogLog.from_bytes(left.to_bytes())
    assert restored.to_bytes() == left.to_bytes()
    restored.merge(right)
    assert restored.count() == pytest.approx(1_500, rel=0.06)
    assert HyperLogLog.from_bytes(union_bytes(left.to_bytes(), right.to_bytes())).count() == restored.count()


def test_sparse_promotes_to_dense():
    sketch = HyperLogLog()
    for value in range(SPARSE_LIMIT * 4):
        sketch.add(digest(value))
        if len(sketch.to_bytes()) > 1_000:
            break
    encoded = sketch.to_bytes()
    assert encoded[5] == 1
    assert HyperLogLog.from_bytes(encoded).to_bytes() == encoded


def test_estimates_representative_cardinality():
    for cardinality in (1, 10, 100, 10_000):
        sketch = HyperLogLog()
        for value in range(cardinality):
            sketch.add(digest(value))
        assert sketch.count() == pytest.approx(cardinality, rel=0.08, abs=1)


def test_rejects_invalid_or_incompatible_sketches():
    with pytest.raises(ValueError):
        HyperLogLog.from_bytes(b"bad")
    with pytest.raises(ValueError):
        HyperLogLog.from_bytes(b"CPH1\x0c\x00\x00\x01")
    with pytest.raises(ValueError):
        HyperLogLog(12).merge(HyperLogLog(11))
    with pytest.raises(ValueError):
        HyperLogLog().add(b"short")
