"""G.711 (PCMU / PCMA) codec and tiny resampler, numpy only (audioop is gone in Python 3.13)."""
from __future__ import annotations

import numpy as np

_BIAS = 0x84
_CLIP = 32635


def pcm_from_bytes(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype="<i2").copy()


def pcm_to_bytes(pcm: np.ndarray) -> bytes:
    return np.asarray(pcm, dtype="<i2").tobytes()


def ulaw_encode(pcm: np.ndarray) -> bytes:
    x = np.asarray(pcm, dtype=np.int32)
    sign = np.where(x < 0, 0x80, 0)
    x = np.minimum(np.abs(x), _CLIP) + _BIAS
    exp = np.floor(np.log2(x)).astype(np.int32) - 7
    exp = np.clip(exp, 0, 7)
    mant = (x >> (exp + 3)) & 0x0F
    return ((~(sign | (exp << 4) | mant)) & 0xFF).astype(np.uint8).tobytes()


def ulaw_decode(data: bytes) -> np.ndarray:
    u = (~np.frombuffer(data, dtype=np.uint8).astype(np.int32)) & 0xFF
    sign = u & 0x80
    exp = (u >> 4) & 0x07
    mant = u & 0x0F
    mag = (((mant << 3) + _BIAS) << exp) - _BIAS
    return np.where(sign != 0, -mag, mag).astype(np.int16)


def alaw_encode(pcm: np.ndarray) -> bytes:
    x = np.asarray(pcm, dtype=np.int32)
    sign = np.where(x >= 0, 0x80, 0)
    mag = np.minimum(np.abs(x), 32767)
    big = mag >= 256
    exp = np.where(big, np.floor(np.log2(np.maximum(mag, 1))).astype(np.int32) - 7, 0)
    mant = np.where(big, (mag >> (exp + 3)) & 0x0F, mag >> 4)
    return ((sign | (exp << 4) | mant) ^ 0x55).astype(np.uint8).tobytes()


def alaw_decode(data: bytes) -> np.ndarray:
    v = np.frombuffer(data, dtype=np.uint8).astype(np.int32) ^ 0x55
    sign = v & 0x80
    exp = (v >> 4) & 0x07
    mant = v & 0x0F
    mag = np.where(exp == 0, (mant << 4) + 8, ((mant << 4) + 0x108) << np.maximum(exp - 1, 0))
    return np.where(sign != 0, mag, -mag).astype(np.int16)


def resample(pcm: np.ndarray, src: int, dst: int) -> np.ndarray:
    """Cheap resampler for telephony (box-filter down, linear interpolation up)."""
    pcm = np.asarray(pcm)
    if src == dst or pcm.size == 0:
        return pcm.astype(np.int16)
    x = pcm.astype(np.float32)
    if dst < src and src % dst == 0:
        k = src // dst
        n = (x.size // k) * k
        out = x[:n].reshape(-1, k).mean(axis=1)
    else:
        n_out = int(round(x.size * dst / src))
        out = np.interp(np.linspace(0, x.size - 1, n_out), np.arange(x.size), x)
    return np.clip(out, -32768, 32767).astype(np.int16)
