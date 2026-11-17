#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qrstream.py - 把任意文件变成动画二维码，在终端里播放。

设计目标：
  * 单文件、零第三方依赖（仅 Python 标准库）；
  * 命令行运行：python qrstream.py 文件
  * 生成的二维码流兼容 RaptorQR 官方接收端（手机浏览器打开
    https://qr.linkto.host/ 扫码即可接收并还原文件）。

协议（与 RaptorQR 的 legacy js-rlnc 路径一致）：
  8 字节包头 + N 字节负载 + 4 字节 CRC32C
  包头：magic(0x51) + 32bit packed word + 24bit dataLength
  分代：K=16 个源符号/代，R=8 个 RLNC 校验符号/代，
        外层再按 3% 追加 Reed-Solomon 校验代。

QR 编码器与 Reed-Solomon 均为本文件内的纯 Python 实现。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import struct
import sys
import time
import zlib

__version__ = "1.0.0"

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

MAGIC_BYTE = 0x51
HEADER_SIZE = 8
CRC32C_SIZE = 4
K = 16                      # 每代源符号数
R = 8                       # 每代 RLNC 校验符号数
RAPTORQ_SYMBOL_INDEX = 31   # 官方 RaptorQ 路径的哨兵值，本程序不使用
MAX_DATA_LENGTH = 0xFFFFFF  # 24bit dataLength
MAX_GENERATIONS = 0xFFF     # 12bit generationIndex / totalGenerations
OUTER_EC_OVERHEAD = 0.03
GF256_RS_MAX_POINTS = 255

QR_VERSION_OPTIONS = (10, 15, 20, 25, 30, 35, 40)
ECC_LEVEL_OPTIONS = ("L", "M", "Q", "H")

MIME_TYPES = {
    ".txt": "text/plain", ".html": "text/html", ".htm": "text/html",
    ".css": "text/css", ".js": "application/javascript",
    ".ts": "application/typescript", ".json": "application/json",
    ".xml": "application/xml", ".csv": "text/csv", ".md": "text/markdown",
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".svg": "image/svg+xml", ".ico": "image/x-icon", ".zip": "application/zip",
    ".gz": "application/gzip", ".tar": "application/x-tar",
    ".mp3": "audio/mpeg", ".mp4": "video/mp4", ".wasm": "application/wasm",
    ".bin": "application/octet-stream",
}


# ---------------------------------------------------------------------------
# GF(256)：本原多项式 0x11D（QR 与协议层共用）
# ---------------------------------------------------------------------------

_GF_POLY = 0x11D
_GF_EXP = [0] * 512
_GF_LOG = [0] * 256


def _gf_init() -> None:
    x = 1
    for i in range(255):
        _GF_EXP[i] = x
        _GF_LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= _GF_POLY
    for i in range(255, 512):
        _GF_EXP[i] = _GF_EXP[i - 255]
    _GF_LOG[0] = 0


_gf_init()


def gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def gf_div(a: int, b: int) -> int:
    if b == 0:
        raise ZeroDivisionError("GF(256) division by zero")
    if a == 0:
        return 0
    return _GF_EXP[(_GF_LOG[a] - _GF_LOG[b]) % 255]


def gf_pow(a: int, n: int) -> int:
    if n == 0:
        return 1
    if a == 0:
        return 0
    return _GF_EXP[(_GF_LOG[a] * n) % 255]


def gf_inv(a: int) -> int:
    if a == 0:
        return 0
    return _GF_EXP[255 - _GF_LOG[a]]


# 预计算“乘以常数”的字节翻译表，用 bytes.translate + 大整数异或加速
_MUL_TABLES: list[bytes | None] = [None] * 256


def _mul_table(c: int) -> bytes:
    tab = _MUL_TABLES[c]
    if tab is None:
        tab = bytes(gf_mul(c, v) for v in range(256))
        _MUL_TABLES[c] = tab
    return tab


# ---------------------------------------------------------------------------
# CRC32C (Castagnoli, 反射多项式 0x82F63B78)
# ---------------------------------------------------------------------------

_CRC32C_TABLE = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ (0x82F63B78 if _c & 1 else 0)
    _CRC32C_TABLE.append(_c)


def crc32c(data: bytes) -> int:
    crc = 0xFFFFFFFF
    tab = _CRC32C_TABLE
    for b in data:
        crc = (crc >> 8) ^ tab[(crc ^ b) & 0xFF]
    return crc ^ 0xFFFFFFFF


# ---------------------------------------------------------------------------
# xoshiro128**（与 RaptorQR JS 实现逐位一致）
# ---------------------------------------------------------------------------

_MASK32 = 0xFFFFFFFF
_MASK64 = 0xFFFFFFFFFFFFFFFF


def _rotl32(x: int, k: int) -> int:
    x &= _MASK32
    return ((x << k) | (x >> (32 - k))) & _MASK32


def _splitmix64_next(state: int) -> int:
    # 逐位复刻 RaptorQR 的 JS 实现，包括两个“意外”细节：
    #   1) 这里的常量在 JS 中先被解析成 IEEE-754 double，再 BigInt()，
    #      因此实际值是 0x...8000 而不是标准的 0x...7c15 等；
    #   2) 第一次乘法后没有立即截断到 64 位。
    state = (state + 0x9E3779B97F4A8000) & _MASK64
    z = state
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E800
    z = (z ^ (z >> 27)) & _MASK64
    z = (z * 0x94D049BB13311000) & _MASK64
    z ^= z >> 31
    return z & _MASK64


class Xoshiro128:
    """32 位种子经 splitmix64 扩展为 128 位状态。"""

    __slots__ = ("s",)

    def __init__(self, seed: int):
        state = seed & _MASK32
        self.s = [0, 0, 0, 0]
        for i in range(4):
            state = _splitmix64_next(state)
            self.s[i] = state & _MASK32

    def next(self) -> int:
        s = self.s
        result = (_rotl32((s[1] * 5) & _MASK32, 7) * 9) & _MASK32
        t = (s[1] << 9) & _MASK32
        s[2] ^= s[0]
        s[3] ^= s[1]
        s[1] ^= s[2]
        s[0] ^= s[3]
        s[2] ^= t
        s[3] = _rotl32(s[3], 11)
        return result

    def next_byte(self) -> int:
        return self.next() & 0xFF


def derive_coefficient_seed(generation_index: int, coded_symbol_index: int) -> int:
    gen = generation_index & _MASK32
    idx = (coded_symbol_index + 1) & _MASK32
    return ((gen * 0x9E3779B9) ^ (idx * 0x85EBCA6B)
            ^ (gen >> 16) ^ ((idx << 16) & _MASK32)) & _MASK32


def generate_coefficients(k: int, seed: int) -> bytes:
    rng = Xoshiro128(seed)
    coeffs = bytearray(k)
    for i in range(k):
        v = 0
        while v == 0:
            v = rng.next_byte()
        coeffs[i] = v
    return bytes(coeffs)


# ---------------------------------------------------------------------------
# 协议层
# ---------------------------------------------------------------------------

def pack_header(generation_index: int, total_generations: int, symbol_index: int,
                is_text: bool, is_last: bool, compressed: bool,
                data_length: int) -> bytes:
    word = (generation_index & 0xFFF)
    word |= (total_generations & 0xFFF) << 12
    word |= (symbol_index & 0x1F) << 24
    word |= (1 if is_text else 0) << 29
    word |= (1 if is_last else 0) << 30
    word |= (1 if compressed else 0) << 31
    return (bytes((MAGIC_BYTE,))
            + struct.pack("<I", word)
            + data_length.to_bytes(3, "little"))


def build_packet(gen: int, total: int, sym_idx: int, is_text: bool,
                 is_last: bool, compressed: bool, data_length: int,
                 payload: bytes) -> bytes:
    body = pack_header(gen, total, sym_idx, is_text, is_last, compressed,
                       data_length) + payload
    return body + struct.pack("<I", crc32c(body))


def parity_count(source_generations: int) -> int:
    if source_generations <= 0:
        return 0
    requested = int(math.floor(source_generations * OUTER_EC_OVERHEAD))
    max_safe = max(0, GF256_RS_MAX_POINTS - source_generations)
    return min(requested, max_safe)


def source_generations_from_length(data_length: int, symbol_size: int) -> int:
    total_symbols = max(1, (data_length + symbol_size - 1) // symbol_size)
    return max(1, (total_symbols + K - 1) // K)


def derive_mime(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return MIME_TYPES.get(ext, "application/octet-stream")


def preprocess_payload(data: bytes, is_text: bool, compress: bool,
                       filename: str | None, mime: str | None):
    """返回 (payload, data_length, is_compressed)。"""
    if (not is_text) and filename:
        name = filename.encode("utf-8")[:255]
        mtype = (mime or "application/octet-stream").encode("utf-8")[:255]
        wrapped = bytes((len(name),)) + name + bytes((len(mtype),)) + mtype + data
    else:
        wrapped = data

    if compress and len(wrapped) > 64:
        # RaptorQR 用的是 fflate 的 deflateSync，即“裸 DEFLATE”（无 zlib 头），
        # 接收端用 inflateSync 解压，这里必须保持一致。
        engine = zlib.compressobj(6, zlib.DEFLATED, -15)
        packed = engine.compress(wrapped) + engine.flush()
        if len(packed) < len(wrapped):
            return packed, len(packed), True
    return wrapped, len(wrapped), False


def _outer_rs_coefficients(source_generations: int, parity: int):
    """返回 [P][G] 的 Lagrange 系数（按 α^i 评估点）。"""
    if parity == 0:
        return []
    G = source_generations
    xs = [_GF_EXP[i % 255] for i in range(G)]
    # D_i = Π_{j != i} (x_i - x_j)，只需算一次
    den = [1] * G
    for i in range(G):
        xi = xs[i]
        acc = 1
        for j in range(G):
            if i == j:
                continue
            acc = gf_mul(acc, xi ^ xs[j])
        den[i] = acc

    rows = []
    for p in range(parity):
        z = _GF_EXP[(G + p) % 255]
        # 分子 N(z) = Π_j (z - x_j)；L_i(z) = N(z) / ((z - x_i) * D_i)
        numer = 1
        for xj in xs:
            numer = gf_mul(numer, z ^ xj)
        row = [0] * G
        for i in range(G):
            row[i] = gf_div(numer, gf_mul(z ^ xs[i], den[i]))
        rows.append(row)
    return rows


def _xor_scaled(acc: int, chunk: bytes, coeff: int) -> int:
    if coeff == 0:
        return acc
    return acc ^ int.from_bytes(chunk.translate(_mul_table(coeff)), "big")


def encode_outer_rs(source_chunks: list[bytes], parity: int) -> list[bytes]:
    if parity == 0 or not source_chunks:
        return []
    rows = _outer_rs_coefficients(len(source_chunks), parity)
    out = []
    for row in rows:
        acc = 0
        for coeff, chunk in zip(row, source_chunks):
            acc = _xor_scaled(acc, chunk, coeff)
        out.append(acc.to_bytes(len(source_chunks[0]), "big"))
    return out


def build_packets(payload: bytes, symbol_size: int, is_text: bool,
                  compressed: bool):
    """
    把预处理后的负载切分、RLNC 编码，返回 (packets, source_generations,
    total_generations, data_length)。
    """
    data_length = len(payload)
    total_symbols = max(1, (data_length + symbol_size - 1) // symbol_size)
    source_gens = max(1, (total_symbols + K - 1) // K)
    parity = parity_count(source_gens)
    total_gens = source_gens + parity

    if data_length > MAX_DATA_LENGTH:
        raise ValueError(
            f"预处理后的数据 {data_length} 字节超过 24 位长度上限"
            f"（{MAX_DATA_LENGTH} 字节），请先压缩或切分文件。")
    if total_gens > MAX_GENERATIONS:
        raise ValueError(
            f"需要 {total_gens} 代，超过协议上限 {MAX_GENERATIONS} 代；"
            "请改用更大的 QR 版本/纠错级别。")

    # 1. 切分成固定长度符号（末符号补零）
    source_chunks = []
    for gen in range(source_gens):
        chunk = bytearray(K * symbol_size)
        base = gen * K
        for i in range(K):
            idx = base + i
            if idx >= total_symbols:
                break
            piece = payload[idx * symbol_size:(idx + 1) * symbol_size]
            start = i * symbol_size
            chunk[start:start + len(piece)] = piece
        source_chunks.append(bytes(chunk))

    # 2. 外层 RS 校验代
    parity_chunks = encode_outer_rs(source_chunks, parity)

    packets: list[bytes] = []
    all_chunks = source_chunks + parity_chunks
    last_index = len(all_chunks) - 1

    for gen, chunk in enumerate(all_chunks):
        is_last = gen == last_index
        symbols = [chunk[i * symbol_size:(i + 1) * symbol_size] for i in range(K)]

        # 3. 系统符号
        for i in range(K):
            packets.append(build_packet(
                gen, total_gens, i, is_text, is_last, compressed,
                data_length, symbols[i]))

        # 4. RLNC 校验符号
        for j in range(R):
            coeffs = generate_coefficients(K, derive_coefficient_seed(gen, j))
            acc = 0
            for coeff, src in zip(coeffs, symbols):
                if coeff:
                    acc = _xor_scaled(acc, src, coeff)
            data = acc.to_bytes(symbol_size, "big")
            packets.append(build_packet(
                gen, total_gens, K + j, is_text, is_last, compressed,
                data_length, data))

    return packets, source_gens, total_gens, data_length


# ---------------------------------------------------------------------------
# QR Code 编码器（ISO/IEC 18004，字节模式，版本 1-40）
# ---------------------------------------------------------------------------

_ECC_INDEX = {"L": 0, "M": 1, "Q": 2, "H": 3}
_ECC_FORMAT_BITS = {"L": 1, "M": 0, "Q": 3, "H": 2}

# 每个版本 × 纠错级别：若干组 (块数, 块总码字, 块数据码字)
_RS_BLOCK_TABLE = [
    [1, 26, 19], [1, 26, 16], [1, 26, 13], [1, 26, 9],
    [1, 44, 34], [1, 44, 28], [1, 44, 22], [1, 44, 16],
    [1, 70, 55], [1, 70, 44], [2, 35, 17], [2, 35, 13],
    [1, 100, 80], [2, 50, 32], [2, 50, 24], [4, 25, 9],
    [1, 134, 108], [2, 67, 43], [2, 33, 15, 2, 34, 16], [2, 33, 11, 2, 34, 12],
    [2, 86, 68], [4, 43, 27], [4, 43, 19], [4, 43, 15],
    [2, 98, 78], [4, 49, 31], [2, 32, 14, 4, 33, 15], [4, 39, 13, 1, 40, 14],
    [2, 121, 97], [2, 60, 38, 2, 61, 39], [4, 40, 18, 2, 41, 19], [4, 40, 14, 2, 41, 15],
    [2, 146, 116], [3, 58, 36, 2, 59, 37], [4, 36, 16, 4, 37, 17], [4, 36, 12, 4, 37, 13],
    [2, 86, 68, 2, 87, 69], [4, 69, 43, 1, 70, 44], [6, 43, 19, 2, 44, 20], [6, 43, 15, 2, 44, 16],
    [4, 101, 81], [1, 80, 50, 4, 81, 51], [4, 50, 22, 4, 51, 23], [3, 36, 12, 8, 37, 13],
    [2, 116, 92, 2, 117, 93], [6, 58, 36, 2, 59, 37], [4, 46, 20, 6, 47, 21], [7, 42, 14, 4, 43, 15],
    [4, 133, 107], [8, 59, 37, 1, 60, 38], [8, 44, 20, 4, 45, 21], [12, 33, 11, 4, 34, 12],
    [3, 145, 115, 1, 146, 116], [4, 64, 40, 5, 65, 41], [11, 36, 16, 5, 37, 17], [11, 36, 12, 5, 37, 13],
    [5, 109, 87, 1, 110, 88], [5, 65, 41, 5, 66, 42], [5, 54, 24, 7, 55, 25], [11, 36, 12, 7, 37, 13],
    [5, 122, 98, 1, 123, 99], [7, 73, 45, 3, 74, 46], [15, 43, 19, 2, 44, 20], [3, 45, 15, 13, 46, 16],
    [1, 135, 107, 5, 136, 108], [10, 74, 46, 1, 75, 47], [1, 50, 22, 15, 51, 23], [2, 42, 14, 17, 43, 15],
    [5, 150, 120, 1, 151, 121], [9, 69, 43, 4, 70, 44], [17, 50, 22, 1, 51, 23], [2, 42, 14, 19, 43, 15],
    [3, 141, 113, 4, 142, 114], [3, 70, 44, 11, 71, 45], [17, 47, 21, 4, 48, 22], [9, 39, 13, 16, 40, 14],
    [3, 135, 107, 5, 136, 108], [3, 67, 41, 13, 68, 42], [15, 54, 24, 5, 55, 25], [15, 43, 15, 10, 44, 16],
    [4, 144, 116, 4, 145, 117], [17, 68, 42], [17, 50, 22, 6, 51, 23], [19, 46, 16, 6, 47, 17],
    [2, 139, 111, 7, 140, 112], [17, 74, 46], [7, 54, 24, 16, 55, 25], [34, 37, 13],
    [4, 151, 121, 5, 152, 122], [4, 75, 47, 14, 76, 48], [11, 54, 24, 14, 55, 25], [16, 45, 15, 14, 46, 16],
    [6, 147, 117, 4, 148, 118], [6, 73, 45, 14, 74, 46], [11, 54, 24, 16, 55, 25], [30, 46, 16, 2, 47, 17],
    [8, 132, 106, 4, 133, 107], [8, 75, 47, 13, 76, 48], [7, 54, 24, 22, 55, 25], [22, 45, 15, 13, 46, 16],
    [10, 142, 114, 2, 143, 115], [19, 74, 46, 4, 75, 47], [28, 50, 22, 6, 51, 23], [33, 46, 16, 4, 47, 17],
    [8, 152, 122, 4, 153, 123], [22, 73, 45, 3, 74, 46], [8, 53, 23, 26, 54, 24], [12, 45, 15, 28, 46, 16],
    [3, 147, 117, 10, 148, 118], [3, 73, 45, 23, 74, 46], [4, 54, 24, 31, 55, 25], [11, 45, 15, 31, 46, 16],
    [7, 146, 116, 7, 147, 117], [21, 73, 45, 7, 74, 46], [1, 53, 23, 37, 54, 24], [19, 45, 15, 26, 46, 16],
    [5, 145, 115, 10, 146, 116], [19, 75, 47, 10, 76, 48], [15, 54, 24, 25, 55, 25], [23, 45, 15, 25, 46, 16],
    [13, 145, 115, 3, 146, 116], [2, 74, 46, 29, 75, 47], [42, 54, 24, 1, 55, 25], [23, 45, 15, 28, 46, 16],
    [17, 145, 115], [10, 74, 46, 23, 75, 47], [10, 54, 24, 35, 55, 25], [19, 45, 15, 35, 46, 16],
    [17, 145, 115, 1, 146, 116], [14, 74, 46, 21, 75, 47], [29, 54, 24, 19, 55, 25], [11, 45, 15, 46, 46, 16],
    [13, 145, 115, 6, 146, 116], [14, 74, 46, 23, 75, 47], [44, 54, 24, 7, 55, 25], [59, 46, 16, 1, 47, 17],
    [12, 151, 121, 7, 152, 122], [12, 75, 47, 26, 76, 48], [39, 54, 24, 14, 55, 25], [22, 45, 15, 41, 46, 16],
    [6, 151, 121, 14, 152, 122], [6, 75, 47, 34, 76, 48], [46, 54, 24, 10, 55, 25], [2, 45, 15, 64, 46, 16],
    [17, 152, 122, 4, 153, 123], [29, 74, 46, 14, 75, 47], [49, 54, 24, 10, 55, 25], [24, 45, 15, 46, 46, 16],
    [4, 152, 122, 18, 153, 123], [13, 74, 46, 32, 75, 47], [48, 54, 24, 14, 55, 25], [42, 45, 15, 32, 46, 16],
    [20, 147, 117, 4, 148, 118], [40, 75, 47, 7, 76, 48], [43, 54, 24, 22, 55, 25], [10, 45, 15, 67, 46, 16],
    [19, 148, 118, 6, 149, 119], [18, 75, 47, 31, 76, 48], [34, 54, 24, 34, 55, 25], [20, 45, 15, 61, 46, 16],
]


def rs_block_spec(version: int, ecc: str) -> list[tuple[int, int]]:
    entry = _RS_BLOCK_TABLE[(version - 1) * 4 + _ECC_INDEX[ecc]]
    spec = []
    for i in range(0, len(entry), 3):
        count, total, data = entry[i], entry[i + 1], entry[i + 2]
        spec.extend([(total, data)] * count)
    return spec


def max_byte_capacity(version: int, ecc: str) -> int:
    spec = rs_block_spec(version, ecc)
    total_data = sum(d for _, d in spec)
    count_bits = 8 if version <= 9 else 16
    return max(0, (total_data * 8 - 4 - count_bits) // 8)


def alignment_positions(version: int) -> list[int]:
    if version == 1:
        return []
    size = version * 4 + 17
    num = version // 7 + 2
    step = 26 if version == 32 else ((version * 4 + num * 2 + 1) // (num * 2 - 2)) * 2
    result = [6]
    pos = size - 7
    for _ in range(num - 1):
        result.insert(1, pos)
        pos -= step
    return result


_RS_GENERATOR_CACHE: dict[int, list[int]] = {}


def _rs_generator(degree: int) -> list[int]:
    gen = _RS_GENERATOR_CACHE.get(degree)
    if gen is None:
        gen = [1]
        for i in range(degree):
            factor = _GF_EXP[i]
            out = [0] * (len(gen) + 1)
            for j, c in enumerate(gen):
                out[j] ^= c
                out[j + 1] ^= gf_mul(c, factor)
            gen = out
        _RS_GENERATOR_CACHE[degree] = gen
    return gen


def rs_ecc(data: bytes, degree: int) -> bytes:
    gen = _rs_generator(degree)
    rem = [0] * degree
    for b in data:
        factor = b ^ rem[0]
        del rem[0]
        rem.append(0)
        if factor:
            for j in range(degree):
                rem[j] ^= gf_mul(gen[j + 1], factor)
    return bytes(rem)


def _bch_format(data: int) -> int:
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def _bch_version(version: int) -> int:
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return (version << 12) | rem


class QRCode:
    """字节模式 QR 编码器，强制指定版本与纠错级别。"""

    def __init__(self, data: bytes, version: int, ecc: str):
        if not (1 <= version <= 40):
            raise ValueError("QR version must be 1..40")
        if ecc not in _ECC_INDEX:
            raise ValueError("ECC level must be L/M/Q/H")
        self.data = bytes(data)
        self.version = version
        self.ecc = ecc
        self.size = version * 4 + 17
        self.spec = rs_block_spec(version, ecc)
        self.total_data_codewords = sum(d for _, d in self.spec)
        self.capacity = max_byte_capacity(version, ecc)
        if len(self.data) > self.capacity:
            raise ValueError(
                f"{len(self.data)} 字节超出 V{version}-{ecc} 的容量 "
                f"({self.capacity} 字节)")
        self.codewords = self._make_codewords()
        self.matrix = self._make_matrix()

    # -- 数据码字 ---------------------------------------------------------
    def _make_codewords(self) -> bytes:
        bits: list[int] = []

        def put(value: int, length: int) -> None:
            for i in range(length - 1, -1, -1):
                bits.append((value >> i) & 1)

        put(0b0100, 4)  # byte mode
        put(len(self.data), 8 if self.version <= 9 else 16)
        for b in self.data:
            put(b, 8)

        cap = self.total_data_codewords * 8
        put(0, min(4, cap - len(bits)))
        while len(bits) % 8:
            bits.append(0)

        data = bytearray()
        for i in range(0, len(bits), 8):
            v = 0
            for bit in bits[i:i + 8]:
                v = (v << 1) | bit
            data.append(v)
        pad = (0xEC, 0x11)
        pad_idx = 0
        while len(data) < self.total_data_codewords:
            data.append(pad[pad_idx])
            pad_idx ^= 1

        # 分块、计算 RS 校验码字、交织
        blocks = []
        offset = 0
        for total, data_len in self.spec:
            block = bytes(data[offset:offset + data_len])
            offset += data_len
            blocks.append((block, rs_ecc(block, total - data_len)))

        out = bytearray()
        max_data = max(len(b) for b, _ in blocks)
        for i in range(max_data):
            for block, _ in blocks:
                if i < len(block):
                    out.append(block[i])
        max_ecc = max(len(e) for _, e in blocks)
        for i in range(max_ecc):
            for _, ecc in blocks:
                if i < len(ecc):
                    out.append(ecc[i])
        return bytes(out)

    def _make_matrix(self) -> list[list[bool]]:
        size = self.size
        mod = [[False] * size for _ in range(size)]
        func = [[False] * size for _ in range(size)]

        def set_func(x: int, y: int, dark: bool) -> None:
            if 0 <= x < size and 0 <= y < size:
                mod[y][x] = bool(dark)
                func[y][x] = True

        # 定位图形 + 分隔符
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    dist = max(abs(dx), abs(dy))
                    set_func(cx + dx, cy + dy, dist not in (2, 4))

        # 定时图形
        for i in range(8, size - 8):
            set_func(i, 6, i % 2 == 0)
            set_func(6, i, i % 2 == 0)

        # 校正图形（与定位图形重叠的三处除外；可覆盖定时图形）
        centers = alignment_positions(self.version)
        for cy in centers:
            for cx in centers:
                if ((cx < 8 and cy < 8)
                        or (cx > size - 9 and cy < 8)
                        or (cx < 8 and cy > size - 9)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        set_func(cx + dx, cy + dy,
                                 max(abs(dx), abs(dy)) != 1)

        # 预留格式信息与版本信息
        self._draw_format(0, mod, func)
        if self.version >= 7:
            self._draw_version(mod, func)

        # 数据位放置
        data_bits: list[int] = []
        for byte in self.codewords:
            for i in range(7, -1, -1):
                data_bits.append((byte >> i) & 1)
        bit_idx = 0
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = size - 1 - vert if upward else vert
                    if not func[y][x] and bit_idx < len(data_bits):
                        mod[y][x] = bool(data_bits[bit_idx])
                        bit_idx += 1
            right -= 2

        # 选择掩码
        best_penalty = None
        best_matrix = None
        for mask in range(8):
            candidate = [row[:] for row in mod]
            for y in range(size):
                for x in range(size):
                    if not func[y][x] and _mask_bit(mask, x, y):
                        candidate[y][x] = not candidate[y][x]
            self._draw_format(mask, candidate, func)
            penalty = _penalty(candidate)
            if best_penalty is None or penalty < best_penalty:
                best_penalty = penalty
                best_matrix = candidate
        return best_matrix

    def _draw_format(self, mask: int, mod: list[list[bool]],
                     func: list[list[bool]]) -> None:
        size = self.size
        data = (_ECC_FORMAT_BITS[self.ecc] << 3) | mask
        bits = _bch_format(data)

        def setf(x: int, y: int, dark: bool) -> None:
            mod[y][x] = bool(dark)
            func[y][x] = True

        for i in range(6):
            setf(8, i, (bits >> i) & 1)
        setf(8, 7, (bits >> 6) & 1)
        setf(8, 8, (bits >> 7) & 1)
        setf(7, 8, (bits >> 8) & 1)
        for i in range(9, 15):
            setf(14 - i, 8, (bits >> i) & 1)

        for i in range(8):
            setf(size - 1 - i, 8, (bits >> i) & 1)
        for i in range(8, 15):
            setf(8, size - 15 + i, (bits >> i) & 1)
        setf(8, size - 8, True)

    def _draw_version(self, mod: list[list[bool]],
                      func: list[list[bool]]) -> None:
        size = self.size
        bits = _bch_version(self.version)
        for i in range(18):
            bit = (bits >> i) & 1
            a = size - 11 + i % 3
            b = i // 3
            mod[b][a] = bool(bit)
            func[b][a] = True
            mod[a][b] = bool(bit)
            func[a][b] = True


def _mask_bit(mask: int, x: int, y: int) -> bool:
    if mask == 0:
        return (x + y) % 2 == 0
    if mask == 1:
        return y % 2 == 0
    if mask == 2:
        return x % 3 == 0
    if mask == 3:
        return (x + y) % 3 == 0
    if mask == 4:
        return (x // 3 + y // 2) % 2 == 0
    if mask == 5:
        return (x * y) % 2 + (x * y) % 3 == 0
    if mask == 6:
        return ((x * y) % 2 + (x * y) % 3) % 2 == 0
    return ((x + y) % 2 + (x * y) % 3) % 2 == 0


def _penalty(matrix: list[list[bool]]) -> int:
    size = len(matrix)
    score = 0

    # N1: 行/列连续同色 >= 5
    for y in range(size):
        run = 1
        for x in range(1, size):
            if matrix[y][x] == matrix[y][x - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + run - 5
                run = 1
        if run >= 5:
            score += 3 + run - 5
    for x in range(size):
        run = 1
        for y in range(1, size):
            if matrix[y][x] == matrix[y - 1][x]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + run - 5
                run = 1
        if run >= 5:
            score += 3 + run - 5

    # N2: 2x2 同色块
    for y in range(size - 1):
        row0 = matrix[y]
        row1 = matrix[y + 1]
        for x in range(size - 1):
            if row0[x] == row0[x + 1] == row1[x] == row1[x + 1]:
                score += 3

    # N3: 1:1:3:1:1 定位图形样式（前或后带 4 个浅色模块）
    pattern = [True, False, True, True, True, False, True]
    for y in range(size):
        row = matrix[y]
        for x in range(size - 6):
            if row[x:x + 7] != pattern:
                continue
            before = x >= 4 and not any(row[x - 4:x])
            after = x + 11 <= size and not any(row[x + 7:x + 11])
            if before:
                score += 40
            if after:
                score += 40
    for x in range(size):
        col = [matrix[y][x] for y in range(size)]
        for y in range(size - 6):
            if col[y:y + 7] != pattern:
                continue
            before = y >= 4 and not any(col[y - 4:y])
            after = y + 11 <= size and not any(col[y + 7:y + 11])
            if before:
                score += 40
            if after:
                score += 40

    # N4: 深色比例
    dark = sum(1 for row in matrix for v in row if v)
    total = size * size
    score += (abs(dark * 20 - total * 10) // total) * 10
    return score


# ---------------------------------------------------------------------------
# 终端渲染
# ---------------------------------------------------------------------------

_BLOCK_FULL = "\u2588"
_BLOCK_UPPER = "\u2580"
_BLOCK_LOWER = "\u2584"


def render_terminal(matrix: list[list[bool]], quiet: int = 4,
                    ansi: bool = True) -> list[str]:
    size = len(matrix)
    width = size + quiet * 2
    pad_rows = math.ceil(quiet / 2)
    prefix = "\x1b[47m\x1b[30m" if ansi else ""
    suffix = "\x1b[0m" if ansi else ""
    lines = [prefix + " " * width + suffix for _ in range(pad_rows)]

    for y in range(0, size, 2):
        line = [" "] * quiet
        for x in range(size):
            top = matrix[y][x]
            bottom = matrix[y + 1][x] if y + 1 < size else False
            if top and bottom:
                line.append(_BLOCK_FULL)
            elif top:
                line.append(_BLOCK_UPPER)
            elif bottom:
                line.append(_BLOCK_LOWER)
            else:
                line.append(" ")
        line.extend([" "] * quiet)
        lines.append(prefix + "".join(line) + suffix)

    lines.extend(prefix + " " * width + suffix for _ in range(pad_rows))
    return lines


def write_png_gray(path: str, matrix: list[list[bool]], scale: int = 6,
                   border: int = 4) -> None:
    size = len(matrix)
    dim = (size + border * 2) * scale
    raw = bytearray()
    white = bytes((255,)) * dim
    for _ in range(border * scale):
        raw += b"\x00" + white
    for row in matrix:
        line = bytearray()
        line += bytes((255,)) * (border * scale)
        for dark in row:
            line += (bytes((0,)) if dark else bytes((255,))) * scale
        line += bytes((255,)) * (border * scale)
        for _ in range(scale):
            raw += b"\x00" + bytes(line)
    for _ in range(border * scale):
        raw += b"\x00" + white

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", dim, dim, 8, 0, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


# ---------------------------------------------------------------------------
# 命令行界面
# ---------------------------------------------------------------------------

def human_size(n: int) -> str:
    units = ("B", "KiB", "MiB", "GiB")
    value = float(n)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{n} B"
        value /= 1024
    return f"{n} B"


def enable_windows_ansi() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


class KeyReader:
    """跨平台非阻塞按键读取（Windows: msvcrt，POSIX: termios）。"""

    def __init__(self):
        self.win = os.name == "nt"
        self.fd = None
        self.old_attrs = None
        if not self.win:
            try:
                import termios
                import tty
                self.fd = sys.stdin.fileno()
                self.old_attrs = termios.tcgetattr(self.fd)
                tty.setcbreak(self.fd)
            except Exception:
                self.fd = None

    def read(self, timeout: float = 0.0) -> str | None:
        if self.win:
            import msvcrt
            if msvcrt.kbhit():
                return msvcrt.getwch()
            if timeout > 0:
                time.sleep(timeout)
            return None
        if self.fd is None:
            if timeout > 0:
                time.sleep(timeout)
            return None
        import select
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if ready:
            try:
                ch = os.read(self.fd, 1)
            except OSError:
                return None
            return ch.decode("utf-8", "ignore")
        return None

    def close(self) -> None:
        if not self.win and self.fd is not None and self.old_attrs is not None:
            try:
                import termios
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_attrs)
            except Exception:
                pass


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[:max(0, width - 1)] + "…"


def _draw(out, lines: list[str], first: bool, count: int) -> None:
    body = "".join(line + "\x1b[K\n" for line in lines)
    if first:
        out.write(body)
    else:
        out.write(f"\x1b[{count}A\x1b[G" + body)
    out.flush()


def play(packets: list[bytes], version: int, ecc: str, symbol_size: int,
         title: str, total_size: int, fps: float, once: bool,
         quiet: int, compress_note: str) -> None:
    """在终端里循环播放 QR 帧。"""
    enable_windows_ansi()
    out = sys.stdout
    term_width = shutil.get_terminal_size((80, 24)).columns
    term_height = shutil.get_terminal_size((80, 24)).lines
    qr_modules = version * 4 + 17
    qr_width = qr_modules + quiet * 2
    qr_lines = (qr_modules + 1) // 2 + 2 * math.ceil(quiet / 2)
    total_lines = qr_lines + 3
    too_narrow = qr_width > term_width
    too_short = total_lines > term_height

    if too_narrow:
        print(f"提示：QR 宽 {qr_width} 列，当前终端只有 {term_width} 列；"
              f"请缩小终端字体或改用更小的 --qr-version。", file=sys.stderr)
    if too_short:
        print(f"提示：整屏需要 {total_lines} 行，当前终端只有 {term_height} 行；"
              f"可缩小字体或把终端窗口拉高。", file=sys.stderr)
    tail = ("receiver: https://qr.linkto.host/  (phone browser + camera)"
            if not (too_narrow or too_short)
            else "WARNING: terminal too small - shrink font / enlarge window")

    cache: dict[int, list[str]] = {}
    cache_order: list[int] = []

    def frame_lines(index: int) -> list[str]:
        lines = cache.get(index)
        if lines is None:
            matrix = QRCode(packets[index], version, ecc).matrix
            lines = render_terminal(matrix, quiet=quiet, ansi=True)
            cache[index] = lines
            cache_order.append(index)
            if len(cache_order) > 4096:
                old = cache_order.pop(0)
                cache.pop(old, None)
        return lines

    # 先渲染第一帧，尽早暴露容量/编码错误
    first_frame = frame_lines(0)

    key_reader = KeyReader()
    entered = False
    running = True
    paused = False
    frame_idx = 0
    loop_no = 1
    shown = 0
    start = time.perf_counter()
    next_tick = start

    try:
        out.write("\x1b[?1049h\x1b[2J\x1b[H\x1b[?25l")
        entered = True
        first_draw = True
        no_count = len(first_frame) + 3

        while running:
            if not paused:
                qr = first_frame if frame_idx == 0 else frame_lines(frame_idx)
                percent = (frame_idx + 1) / len(packets) * 100.0
                status1 = _fit(
                    f"qrstream  {title}  {human_size(total_size)}  "
                    f"V{version}-{ecc}  {symbol_size} B/frame  {compress_note}",
                    term_width)
                status2 = _fit(
                    f"frame {frame_idx + 1}/{len(packets)}  loop {loop_no}  "
                    f"{fps:g} fps  {percent:5.1f}%   "
                    f"q quit   space pause",
                    term_width)
                footer = _fit(tail, term_width)
                lines = [status1, status2] + qr + [footer]
                _draw(out, lines, first_draw, no_count)
                first_draw = False

                shown += 1
                frame_idx += 1
                if frame_idx >= len(packets):
                    frame_idx = 0
                    loop_no += 1
                    if once:
                        running = False
                        break
                next_tick += 1.0 / fps
                delay = next_tick - time.perf_counter()
                if delay > 0:
                    # 等待期间也响应按键，保证 q 能及时退出
                    ch = key_reader.read(delay)
                    if ch:
                        if ch in ("q", "Q", "\x1b", "\x03"):
                            running = False
                        elif ch == " ":
                            paused = True
                            next_tick = time.perf_counter()
                else:
                    # 渲染跟不上目标帧率时不要“追帧”，按实际速度播放
                    next_tick = time.perf_counter()
                    ch = key_reader.read(0)
                    if ch:
                        if ch in ("q", "Q", "\x1b", "\x03"):
                            running = False
                        elif ch == " ":
                            paused = True
            else:
                ch = key_reader.read(0.05)
                if ch:
                    if ch in ("q", "Q", "\x1b", "\x03"):
                        running = False
                    elif ch == " ":
                        paused = False
                        next_tick = time.perf_counter()
                if not running:
                    break
                # 暂停时只刷新状态行
                status1 = _fit(
                    f"qrstream  {title}  {human_size(total_size)}  "
                    f"V{version}-{ecc}  {symbol_size} B/frame  {compress_note}",
                    term_width)
                status2 = _fit(
                    f"PAUSED  frame {frame_idx + 1}/{len(packets)}  "
                    f"loop {loop_no}   q quit   space resume",
                    term_width)
                footer = _fit(tail, term_width)
                lines = [status1, status2] + frame_lines(frame_idx) + [footer]
                _draw(out, lines, first_draw, no_count)
                first_draw = False
    except KeyboardInterrupt:
        pass
    finally:
        key_reader.close()
        if entered:
            out.write("\x1b[?25h\x1b[0m\x1b[?1049l")
            out.flush()
        elapsed = time.perf_counter() - start
        print(f"已停止：共显示 {shown} 帧，用时 {elapsed:.1f} 秒。")


def export_frames(packets: list[bytes], version: int, ecc: str,
                  outdir: str, scale: int, quiet: int, meta: dict) -> None:
    os.makedirs(outdir, exist_ok=True)
    total = len(packets)
    for i, packet in enumerate(packets, 1):
        matrix = QRCode(packet, version, ecc).matrix
        path = os.path.join(outdir, f"frame_{i:05d}.png")
        write_png_gray(path, matrix, scale=scale, border=quiet)
        if i % 50 == 0 or i == total:
            print(f"\r导出帧 {i}/{total}", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    manifest = dict(meta)
    manifest.update({
        "frames": total,
        "frame_pattern": "frame_%05d.png",
        "png_scale": scale,
        "quiet_zone_modules": quiet,
    })
    with open(os.path.join(outdir, "manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"已导出 {total} 张 PNG 到 {outdir}（manifest.json 含元数据）。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qrstream",
        description="把文件/文本编码成动画二维码，在终端里循环播放；"
                    "用手机浏览器打开 https://qr.linkto.host/ 扫码即可接收。",
        epilog="示例：\n"
               "  python qrstream.py 简历.pdf\n"
               "  python qrstream.py --qr-version 20 --ecc L 视频.mp4\n"
               "  echo hello | python qrstream.py --text\n"
               "  python qrstream.py 简历.pdf --export frames --scale 6\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", nargs="?",
                        help="要传输的文件；省略时从标准输入读取文本")
    parser.add_argument("--qr-version", "-V", type=int, dest="qr_version",
                        choices=QR_VERSION_OPTIONS, default=10,
                        help="QR 版本（默认 10；越大每帧容量越大、屏幕越大）")
    parser.add_argument("--ecc", "-e", choices=ECC_LEVEL_OPTIONS, default="M",
                        help="QR 纠错级别（默认 M）")
    parser.add_argument("--fps", "-f", type=float, default=10.0,
                        help="播放帧率（默认 10，范围 0.5-30）")
    parser.add_argument("--text", action="store_true",
                        help="按纯文本传输（不带文件名/类型元数据）")
    parser.add_argument("--no-compress", action="store_true",
                        help="关闭 DEFLATE 预处理压缩")
    parser.add_argument("--once", action="store_true",
                        help="只播放一轮，播完退出")
    parser.add_argument("--export", metavar="DIR",
                        help="不播放，改为把每一帧导出为 PNG 到目录")
    parser.add_argument("--scale", type=int, default=6,
                        help="导出 PNG 时每个模块的像素数（默认 6）")
    parser.add_argument("--quiet", type=int, default=4,
                        help="QR 静区模块数（默认 4）")
    parser.add_argument("--info", action="store_true",
                        help="只打印编码计划，不播放")
    parser.add_argument("--about", action="store_true",
                        help="显示版本与协议说明")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = build_parser().parse_args(argv)

    if args.about:
        print(f"qrstream {__version__}")
        print("协议：RaptorQR 兼容的 8 字节包头 + CRC32C + RLNC(K=16,R=8) "
              "+ 3% 外层 RS")
        print("接收端：手机浏览器打开 https://qr.linkto.host/ 扫描终端二维码")
        return 0

    if not (0.5 <= args.fps <= 30):
        print("错误：--fps 必须在 0.5 到 30 之间。", file=sys.stderr)
        return 2
    if args.scale < 1 or args.scale > 20:
        print("错误：--scale 必须在 1 到 20 之间。", file=sys.stderr)
        return 2
    if args.quiet < 0 or args.quiet > 16:
        print("错误：--quiet 必须在 0 到 16 之间。", file=sys.stderr)
        return 2

    is_text = args.text
    filename = None
    mime = None
    if args.file:
        path = args.file
        if not os.path.isfile(path):
            print(f"错误：找不到文件：{path}", file=sys.stderr)
            return 1
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as exc:
            print(f"错误：读取文件失败：{exc}", file=sys.stderr)
            return 1
        filename = os.path.basename(path)
        mime = derive_mime(path)
    else:
        if sys.stdin.isatty():
            print("错误：请提供文件路径，或通过管道输入文本。", file=sys.stderr)
            return 1
        data = sys.stdin.buffer.read()
        is_text = True

    if not data:
        print("错误：输入为空。", file=sys.stderr)
        return 1

    symbol_size = (max_byte_capacity(args.qr_version, args.ecc)
                   - HEADER_SIZE - CRC32C_SIZE)
    if symbol_size <= 0:
        print("错误：该 QR 配置容量不足。", file=sys.stderr)
        return 1

    payload, data_length, is_compressed = preprocess_payload(
        data, is_text, not args.no_compress, filename, mime)
    if data_length > MAX_DATA_LENGTH:
        print(f"错误：预处理后 {data_length} 字节，超过协议 24 位长度上限 "
              f"({MAX_DATA_LENGTH} 字节)。请先压缩或切分文件。",
              file=sys.stderr)
        return 1

    try:
        packets, source_gens, total_gens, data_length = build_packets(
            payload, symbol_size, is_text, is_compressed)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    title = filename or "stdin"
    if is_text:
        title = "stdin" if not filename else filename
    compress_note = "deflate" if is_compressed else "raw"
    max_packet = max(len(p) for p in packets)
    if max_packet > max_byte_capacity(args.qr_version, args.ecc):
        print("内部错误：数据包超出 QR 容量。", file=sys.stderr)
        return 1

    if args.info:
        qr_modules = args.qr_version * 4 + 17
        qr_width = qr_modules + args.quiet * 2
        qr_lines = (qr_modules + 1) // 2 + 2 * math.ceil(args.quiet / 2)
        loop_time = len(packets) / args.fps
        print(f"文件        : {title}")
        print(f"原始大小    : {human_size(len(data))} ({len(data)} 字节)")
        print(f"预处理      : {human_size(data_length)} "
              f"({'DEFLATE 压缩' if is_compressed else '未压缩'})")
        print(f"配置        : V{args.qr_version}-{args.ecc}，"
              f"每帧 {symbol_size} 字节")
        print(f"分代        : {source_gens} 源代 + "
              f"{total_gens - source_gens} 校验代 = {total_gens} 代")
        print(f"帧数        : {len(packets)}")
        print(f"一轮耗时    : {loop_time:.1f} 秒 @ {args.fps:g} fps")
        print(f"QR 尺寸     : {qr_modules}x{qr_modules} 模块，"
              f"终端约 {qr_width} 列 x {qr_lines} 行")
        print(f"理论吞吐    : {symbol_size * args.fps / 1024:.1f} KiB/s "
              f"(未计重复帧/纠错)")
        return 0

    meta = {
        "tool": "qrstream",
        "version": __version__,
        "file": filename,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "is_text": is_text,
        "compressed": is_compressed,
        "preprocessed_size": data_length,
        "qr_version": args.qr_version,
        "ecc": args.ecc,
        "symbol_size": symbol_size,
        "source_generations": source_gens,
        "total_generations": total_gens,
        "fps": args.fps,
    }

    if args.export:
        export_frames(packets, args.qr_version, args.ecc, args.export,
                      args.scale, args.quiet, meta)
        return 0

    if not sys.stdout.isatty():
        matrix = QRCode(packets[0], args.qr_version, args.ecc).matrix
        print("\n".join(render_terminal(matrix, quiet=args.quiet, ansi=False)))
        print("\nstdout 不是终端，仅输出第一帧；"
              "请用 --export 导出 PNG 帧，或在终端中运行。", file=sys.stderr)
        return 0

    play(packets, args.qr_version, args.ecc, symbol_size, title, len(data),
         args.fps, args.once, args.quiet, compress_note)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
