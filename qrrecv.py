#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qrrecv.py - 从视频/图片序列中读取 qrstream 的二维码流并还原文件。

依赖（仅接收端需要）：
    pip install opencv-python numpy zxing-cpp
Windows 可直接使用打包好的 dist/qrrecv.exe（无需安装 Python）。

协议与 qrstream.py / RaptorQR 的 legacy js-rlnc 路径一致：
    8 字节包头 + FEC 负载 + 4 字节 CRC32C
    K=16 源符号/代，R=8 个 RLNC 校验符号/代，外层 3% Reed-Solomon 校验代。
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import re
import sys
import time
import zlib

__version__ = "1.0.0"

MAGIC_BYTE = 0x51
HEADER_SIZE = 8
CRC32C_SIZE = 4
K = 16
R = 8
RAPTORQ_SYMBOL_INDEX = 31
OUTER_EC_OVERHEAD = 0.03
GF256_RS_MAX_POINTS = 255


# ---------------------------------------------------------------------------
# GF(256)
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


def gf_inv(a: int) -> int:
    if a == 0:
        return 0
    return _GF_EXP[255 - _GF_LOG[a]]


_MUL_TABLES: list[bytes | None] = [None] * 256


def _mul_table(c: int) -> bytes:
    tab = _MUL_TABLES[c]
    if tab is None:
        tab = bytes(gf_mul(c, v) for v in range(256))
        _MUL_TABLES[c] = tab
    return tab


def _scaled_int(value: int, length: int, coeff: int) -> int:
    """把大整数按 GF(256) 乘以 coeff（逐字节）。"""
    if value == 0 or coeff == 0:
        return 0
    raw = value.to_bytes(length, "big")
    return int.from_bytes(raw.translate(_mul_table(coeff)), "big")


# ---------------------------------------------------------------------------
# CRC32C
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
# xoshiro128** / RLNC 系数
# ---------------------------------------------------------------------------

_MASK32 = 0xFFFFFFFF
_MASK64 = 0xFFFFFFFFFFFFFFFF


def _rotl32(x: int, k: int) -> int:
    x &= _MASK32
    return ((x << k) | (x >> (32 - k))) & _MASK32


def _splitmix64_next(state: int) -> int:
    state = (state + 0x9E3779B97F4A8000) & _MASK64
    z = state
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E800
    z = (z ^ (z >> 27)) & _MASK64
    z = (z * 0x94D049BB13311000) & _MASK64
    z ^= z >> 31
    return z & _MASK64


class Xoshiro128:
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
# 协议
# ---------------------------------------------------------------------------

class PacketError(ValueError):
    pass


class RaptorQPacket(PacketError):
    pass


def parse_packet(data: bytes) -> dict:
    if len(data) < HEADER_SIZE + CRC32C_SIZE:
        raise PacketError("packet too short")
    if data[0] != MAGIC_BYTE:
        raise PacketError(f"bad magic 0x{data[0]:02x}")

    word = int.from_bytes(data[1:5], "little")
    header = {
        "generation": word & 0xFFF,
        "total_generations": (word >> 12) & 0xFFF,
        "symbol_index": (word >> 24) & 0x1F,
        "is_text": bool((word >> 29) & 1),
        "is_last": bool((word >> 30) & 1),
        "compressed": bool((word >> 31) & 1),
        "data_length": int.from_bytes(data[5:8], "little"),
    }
    payload = data[HEADER_SIZE:-CRC32C_SIZE]
    stored_crc = int.from_bytes(data[-CRC32C_SIZE:], "little")
    if crc32c(data[:-CRC32C_SIZE]) != stored_crc:
        raise PacketError("CRC32C mismatch")

    if header["symbol_index"] == RAPTORQ_SYMBOL_INDEX:
        raise RaptorQPacket("RaptorQ packet (unsupported)")
    if header["symbol_index"] >= K + R:
        raise PacketError("reserved symbol index")
    if not payload:
        raise PacketError("empty payload")
    if header["data_length"] == 0:
        raise PacketError("zero data length")
    if header["total_generations"] == 0:
        raise PacketError("zero generation count")

    header["payload"] = payload
    return header


def parity_count(source_generations: int) -> int:
    if source_generations <= 0:
        return 0
    requested = int(math.floor(source_generations * OUTER_EC_OVERHEAD))
    max_safe = max(0, GF256_RS_MAX_POINTS - source_generations)
    return min(requested, max_safe)


def source_generations_from_length(data_length: int, symbol_size: int) -> int:
    total_symbols = max(1, (data_length + symbol_size - 1) // symbol_size)
    return max(1, (total_symbols + K - 1) // K)


# ---------------------------------------------------------------------------
# 单代 RLNC 解码（高斯消元 + 回代）
# ---------------------------------------------------------------------------

class _Row:
    __slots__ = ("coeffs", "data", "pivot")

    def __init__(self, coeffs: list[int], data: int, pivot: int):
        self.coeffs = coeffs
        self.data = data
        self.pivot = pivot


class GenerationDecoder:
    def __init__(self, k: int, symbol_size: int):
        self.k = k
        self.symbol_size = symbol_size
        self.rows: list[_Row] = []
        self._sources: list[bytes] | None = None
        self.rank = 0

    @property
    def solved(self) -> bool:
        return self._sources is not None

    def add(self, symbol: bytes, coeffs: bytes) -> bool:
        if self._sources is not None:
            return False
        if len(coeffs) != self.k:
            raise ValueError("bad coefficient length")
        n = self.symbol_size
        c = list(coeffs)
        d = int.from_bytes(symbol, "big")

        # 用已有的主元行消去
        for row in self.rows:
            f = c[row.pivot]
            if f:
                rc = row.coeffs
                for j in range(row.pivot, self.k):
                    c[j] ^= gf_mul(f, rc[j])
                d ^= _scaled_int(row.data, n, f)

        # 找新的主元
        pivot = -1
        for j in range(self.k):
            if c[j]:
                pivot = j
                break
        if pivot < 0:
            return False

        # 归一化
        if c[pivot] != 1:
            inv = gf_inv(c[pivot])
            for j in range(pivot, self.k):
                c[j] = gf_mul(c[j], inv)
            d = _scaled_int(d, n, inv)

        # 用新行消去所有已有行的该列，保持行阶梯形
        for row in self.rows:
            f = row.coeffs[pivot]
            if f:
                rc = row.coeffs
                for j in range(pivot, self.k):
                    rc[j] ^= gf_mul(f, c[j])
                row.data ^= _scaled_int(d, n, f)

        new_row = _Row(c, d, pivot)
        insert_at = 0
        while insert_at < len(self.rows) and self.rows[insert_at].pivot < pivot:
            insert_at += 1
        self.rows.insert(insert_at, new_row)
        self.rank += 1

        if self.rank == self.k:
            self._solve()
        return True

    def _solve(self) -> None:
        # 满秩时主元列恰好是 0..K-1
        sources = [0] * self.k
        for row in reversed(self.rows):
            d = row.data
            for j in range(row.pivot + 1, self.k):
                f = row.coeffs[j]
                if f:
                    d ^= _scaled_int(sources[j], self.symbol_size, f)
            sources[row.pivot] = d
        self._sources = [v.to_bytes(self.symbol_size, "big") for v in sources]

    def get_sources(self) -> list[bytes] | None:
        return list(self._sources) if self._sources is not None else None

    def chunk(self) -> bytes | None:
        if self._sources is None:
            return None
        return b"".join(self._sources)


# ---------------------------------------------------------------------------
# 外层 Reed-Solomon 恢复
# ---------------------------------------------------------------------------

class RecoveryError(Exception):
    pass


def _lagrange_denominators(source_points: list[int]) -> list[int]:
    g = len(source_points)
    den = [1] * g
    for i in range(g):
        xi = source_points[i]
        acc = 1
        for j in range(g):
            if i != j:
                acc = gf_mul(acc, xi ^ source_points[j])
        den[i] = acc
    return den


def _lagrange_row(z: int, source_points: list[int],
                  denominators: list[int]) -> list[int]:
    numer = 1
    for x in source_points:
        numer = gf_mul(numer, z ^ x)
    return [gf_div(numer, gf_mul(z ^ x, den))
            for x, den in zip(source_points, denominators)]


def _invert_matrix(matrix: list[list[int]]) -> list[list[int]]:
    n = len(matrix)
    aug = [row[:] + [1 if i == j else 0 for j in range(n)]
           for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = -1
        for r in range(col, n):
            if aug[r][col]:
                pivot = r
                break
        if pivot < 0:
            raise RecoveryError("singular matrix in outer RS")
        if pivot != col:
            aug[col], aug[pivot] = aug[pivot], aug[col]
        inv = gf_inv(aug[col][col])
        if inv != 1:
            aug[col] = [gf_mul(v, inv) for v in aug[col]]
        for r in range(n):
            if r == col:
                continue
            f = aug[r][col]
            if f:
                aug[r] = [a ^ gf_mul(f, b) for a, b in zip(aug[r], aug[col])]
    return [row[n:] for row in aug]


def recover_missing_generations(solved: dict[int, bytes], source_generations: int,
                                parity_generations: int,
                                symbol_size: int) -> list[bytes]:
    """返回按代顺序排列的 G 个 chunk。"""
    g = source_generations
    p = parity_generations
    total = g + p

    present = [i for i in range(g) if i in solved]
    missing = [i for i in range(g) if i not in solved]
    if not missing:
        return [solved[i] for i in range(g)]

    available_parity = [i for i in range(g, total) if i in solved]
    if len(available_parity) < len(missing):
        raise RecoveryError(
            f"缺少 {len(missing)} 代，但只收到 {len(available_parity)} 个校验代")

    if g + p > GF256_RS_MAX_POINTS:
        raise RecoveryError("代数超出 GF(256) RS 上限")

    source_points = [_GF_EXP[i % 255] for i in range(g)]
    den = _lagrange_denominators(source_points)
    used_parity = available_parity[:len(missing)]

    a_matrix = []
    parity_rows = []
    for gen in used_parity:
        z = _GF_EXP[gen % 255]
        row = _lagrange_row(z, source_points, den)
        parity_rows.append((solved[gen], row))
        a_matrix.append([row[m] for m in missing])
    a_inv = _invert_matrix(a_matrix)

    n = K * symbol_size
    rhs = []
    for chunk, row in parity_rows:
        val = int.from_bytes(chunk, "big")
        for idx in present:
            f = row[idx]
            if f:
                val ^= _scaled_int(int.from_bytes(solved[idx], "big"), n, f)
        rhs.append(val)

    recovered = {}
    for mi, gen in enumerate(missing):
        val = 0
        for q in range(len(rhs)):
            f = a_inv[mi][q]
            if f:
                val ^= _scaled_int(rhs[q], n, f)
        recovered[gen] = val.to_bytes(n, "big")

    return [solved[i] if i in solved else recovered[i] for i in range(g)]


# ---------------------------------------------------------------------------
# 传输状态机
# ---------------------------------------------------------------------------

class ProtocolError(Exception):
    pass


class TransferResult:
    def __init__(self, data: bytes, filename: str, mime: str, is_text: bool):
        self.data = data
        self.filename = filename
        self.mime = mime
        self.is_text = is_text


class Transfer:
    """一次传输的接收状态。"""

    def __init__(self, packet: dict):
        self.symbol_size = len(packet["payload"])
        self.data_length = packet["data_length"]
        self.total_generations = packet["total_generations"]
        self.is_text = packet["is_text"]
        self.is_compressed = packet["compressed"]
        self.source_generations = source_generations_from_length(
            self.data_length, self.symbol_size)

        if self.total_generations < self.source_generations:
            raise ProtocolError("总代数小于源代数")
        self.parity_generations = self.total_generations - self.source_generations
        expected = parity_count(self.source_generations)
        if self.parity_generations != expected:
            raise ProtocolError(
                f"校验代数不一致：收到 {self.parity_generations}，"
                f"按协议应为 {expected}")

        self.decoders: dict[int, GenerationDecoder] = {}
        self.solved: dict[int, bytes] = {}
        self.solved_source_count = 0
        self.solved_parity_count = 0
        self.dedup: set[tuple[int, int]] = set()
        self.accepted = 0
        self.duplicates = 0
        self.last_error: str | None = None
        self.result: TransferResult | None = None

    def matches(self, packet: dict) -> bool:
        return (len(packet["payload"]) == self.symbol_size
                and packet["data_length"] == self.data_length
                and packet["total_generations"] == self.total_generations
                and packet["is_text"] == self.is_text
                and packet["compressed"] == self.is_compressed)

    def add(self, packet: dict) -> bool:
        if self.result is not None:
            return False
        gen = packet["generation"]
        sym = packet["symbol_index"]
        if gen >= self.total_generations:
            return False

        key = (gen, sym)
        if key in self.dedup:
            self.duplicates += 1
            return False
        self.dedup.add(key)

        if sym < K:
            coeffs = bytearray(K)
            coeffs[sym] = 1
            coeffs = bytes(coeffs)
        else:
            coeffs = generate_coefficients(
                K, derive_coefficient_seed(gen, sym - K))

        decoder = self.decoders.get(gen)
        if decoder is None:
            decoder = GenerationDecoder(K, self.symbol_size)
            self.decoders[gen] = decoder

        if decoder.add(packet["payload"], coeffs):
            self.accepted += 1
            if decoder.solved and gen not in self.solved:
                self.solved[gen] = decoder.chunk()
                if gen < self.source_generations:
                    self.solved_source_count += 1
                else:
                    self.solved_parity_count += 1
        else:
            self.duplicates += 1

        if self.result is None and len(self.solved) >= self.source_generations:
            missing = self.source_generations - self.solved_source_count
            if missing == 0 or self.solved_parity_count >= missing:
                self._try_complete()
        return True

    def _try_complete(self) -> None:
        try:
            chunks = recover_missing_generations(
                self.solved, self.source_generations,
                self.parity_generations, self.symbol_size)
        except RecoveryError as exc:
            self.last_error = str(exc)
            return

        combined = b"".join(chunks)
        if len(combined) < self.data_length:
            self.last_error = "重组数据长度不足"
            return
        preprocessed = combined[:self.data_length]

        if self.is_compressed:
            try:
                preprocessed = zlib.decompress(preprocessed, -15)
            except zlib.error as exc:
                self.last_error = f"解压失败：{exc}"
                return

        filename = ""
        mime = "application/octet-stream"
        data = preprocessed
        if not self.is_text:
            try:
                if len(preprocessed) >= 2:
                    name_len = preprocessed[0]
                    if 2 + name_len <= len(preprocessed):
                        mime_len = preprocessed[1 + name_len]
                        meta_end = 2 + name_len + mime_len
                        if meta_end <= len(preprocessed):
                            filename = preprocessed[1:1 + name_len].decode(
                                "utf-8", "replace")
                            mime = preprocessed[2 + name_len:meta_end].decode(
                                "utf-8", "replace")
                            data = preprocessed[meta_end:]
            except Exception:
                data = preprocessed
                filename = ""
                mime = "application/octet-stream"

        self.result = TransferResult(data, filename, mime, self.is_text)

    @property
    def progress(self) -> tuple[int, int]:
        return self.solved_source_count, self.source_generations


# ---------------------------------------------------------------------------
# 帧源
# ---------------------------------------------------------------------------

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff")


class VideoSource:
    kind = "video"

    def __init__(self, cv2, cap, path: str):
        self.cv2 = cv2
        self.cap = cap
        self.path = path
        self.fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        if not (1 <= self.fps <= 240):
            self.fps = 25.0
        self.total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        self.height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        try:
            if hasattr(cv2, "CAP_PROP_ORIENTATION_AUTO"):
                cap.set(cv2.CAP_PROP_ORIENTATION_AUTO, 1)
        except Exception:
            pass

    def frames(self, start: float, end: float | None, stride: int,
               max_frames: int, rotate: int):
        cap = self.cap
        start_frame = max(0, int(start * self.fps)) if start > 0 else 0
        if start_frame:
            cap.set(self.cv2.CAP_PROP_POS_FRAMES, start_frame)
            actual = int(cap.get(self.cv2.CAP_PROP_POS_FRAMES) or 0)
            if actual > 0:
                start_frame = actual
        end_frame = int(end * self.fps) if end else None
        idx = start_frame
        produced = 0
        while True:
            if end_frame is not None and idx >= end_frame:
                break
            if max_frames and produced >= max_frames:
                break
            if stride > 1 and idx % stride:
                if not cap.grab():
                    break
                idx += 1
                continue
            ok, frame = cap.read()
            if not ok:
                break
            if rotate:
                if rotate == 90:
                    frame = self.cv2.rotate(frame, self.cv2.ROTATE_90_CLOCKWISE)
                elif rotate == 180:
                    frame = self.cv2.rotate(frame, self.cv2.ROTATE_180)
                elif rotate == 270:
                    frame = self.cv2.rotate(frame, self.cv2.ROTATE_90_COUNTERCLOCKWISE)
            yield idx, frame
            idx += 1
            produced += 1

    def close(self) -> None:
        self.cap.release()


class ImageSource:
    def __init__(self, cv2, np, paths: list[str], is_single: bool = False):
        self.cv2 = cv2
        self.np = np
        self.paths = paths
        self.kind = "image" if is_single else "images"
        self.fps = 1.0
        self.total = len(paths)
        self.width = 0
        self.height = 0

    def frames(self, start: float, end: float | None, stride: int,
               max_frames: int, rotate: int):
        begin = max(0, int(start)) if start > 0 else 0
        stop = int(end) if end else len(self.paths)
        produced = 0
        for idx in range(begin, min(stop, len(self.paths))):
            if max_frames and produced >= max_frames:
                break
            if stride > 1 and (idx - begin) % stride:
                continue
            path = self.paths[idx]
            try:
                buf = self.np.fromfile(path, dtype=self.np.uint8)
                frame = self.cv2.imdecode(buf, self.cv2.IMREAD_COLOR)
            except Exception:
                frame = None
            if frame is None:
                yield idx, None
                continue
            if rotate:
                if rotate == 90:
                    frame = self.cv2.rotate(frame, self.cv2.ROTATE_90_CLOCKWISE)
                elif rotate == 180:
                    frame = self.cv2.rotate(frame, self.cv2.ROTATE_180)
                elif rotate == 270:
                    frame = self.cv2.rotate(frame, self.cv2.ROTATE_90_COUNTERCLOCKWISE)
            produced += 1
            yield idx, frame

    def close(self) -> None:
        pass


def _natural_key(name: str):
    return [(0, int(part)) if part.isdigit() else (1, part.lower())
            for part in re.split(r"(\d+)", name)]


def open_source(cv2, np, path: str):
    if os.path.isdir(path):
        names = [n for n in os.listdir(path)
                 if n.lower().endswith(_IMAGE_EXTS)]
        names.sort(key=_natural_key)
        if not names:
            raise FileNotFoundError("目录中没有可识别的图片")
        return ImageSource(cv2, np, [os.path.join(path, n) for n in names])

    cap = cv2.VideoCapture(path)
    if cap.isOpened():
        return VideoSource(cv2, cap, path)
    cap.release()

    if path.lower().endswith(_IMAGE_EXTS):
        return ImageSource(cv2, np, [path], is_single=True)

    raise IOError(
        "无法打开视频；请确认文件完整，或改用 OpenCV 支持的格式"
        "（mp4 / mov / avi / mkv / webm 等）")


# ---------------------------------------------------------------------------
# 进度显示
# ---------------------------------------------------------------------------

class Progress:
    def __init__(self, total: int, enabled: bool = True):
        self.total = total
        self.show = enabled
        self.enabled = enabled and sys.stderr.isatty()
        self.start = time.perf_counter()
        self.last_print = 0.0
        self.last_len = 0
        self.count = 0

    def update(self, scanned: int, qr_count: int, transfer: Transfer | None,
               duplicates: int, bad: int) -> None:
        if not self.show:
            return
        self.count += 1
        if not self.enabled:
            if self.count % 200 == 0:
                self._plain(scanned, qr_count, transfer, duplicates, bad)
            return
        now = time.perf_counter()
        if now - self.last_print < 0.1:
            return
        self.last_print = now
        elapsed = now - self.start
        rate = scanned / elapsed if elapsed > 0 else 0
        if self.total > 0:
            head = f"scan {scanned}/{self.total} ({scanned * 100.0 / self.total:4.1f}%)"
        else:
            head = f"scan {scanned}"
        if transfer is not None:
            solved, need = transfer.progress
            mid = f"gen {solved}/{need}"
        else:
            mid = "gen 0/?"
        text = (f"{head}  qr {qr_count}  pkt {transfer.accepted if transfer else 0}"
                f"  {mid}  dup {duplicates}  bad {bad}  {rate:4.1f} fps")
        width = shutil_width()
        if len(text) > width:
            text = text[:max(0, width - 1)]
        pad = " " * max(0, self.last_len - len(text))
        sys.stderr.write("\r" + text + pad + "\x1b[K")
        sys.stderr.flush()
        self.last_len = len(text)

    def _plain(self, scanned, qr_count, transfer, duplicates, bad) -> None:
        solved = transfer.progress[0] if transfer else 0
        need = transfer.progress[1] if transfer else 0
        print(f"scan {scanned}  qr {qr_count}  pkt {transfer.accepted if transfer else 0}"
              f"  gen {solved}/{need}  dup {duplicates}  bad {bad}",
              file=sys.stderr, flush=True)

    def finish(self) -> None:
        if self.enabled:
            sys.stderr.write("\x1b[K\n")
            sys.stderr.flush()


def shutil_width() -> int:
    try:
        import shutil
        return max(20, shutil.get_terminal_size((100, 24)).columns - 1)
    except Exception:
        return 100


# ---------------------------------------------------------------------------
# 输出文件
# ---------------------------------------------------------------------------

_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(name: str) -> str | None:
    if not name:
        return None
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name).strip(" .")
    if not name:
        return None
    stem = name.split(".")[0].upper()
    if stem in _WINDOWS_RESERVED:
        name = "_" + name
    return name[:150]


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    for i in range(1, 1000):
        candidate = f"{root}-{i}{ext}"
        if not os.path.exists(candidate):
            return candidate
    return f"{root}-{int(time.time())}{ext}"


def write_output(result: TransferResult, output: str | None, outdir: str,
                 overwrite: bool) -> str | None:
    if output == "-":
        sys.stdout.buffer.write(result.data)
        sys.stdout.buffer.flush()
        return None

    if output:
        path = output
    else:
        default = safe_filename(result.filename)
        if not default:
            default = "recovered.txt" if result.is_text else "recovered.bin"
        path = os.path.join(outdir or ".", default)

    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    if os.path.exists(path) and not overwrite:
        path = _unique_path(path)

    with open(path, "wb") as f:
        f.write(result.data)
    return path


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------

def load_dependencies():
    missing = []
    modules = {}
    try:
        import cv2
        modules["cv2"] = cv2
    except ImportError:
        missing.append("opencv-python")
    try:
        import numpy
        modules["numpy"] = numpy
    except ImportError:
        missing.append("numpy")
    try:
        import zxingcpp
        modules["zxingcpp"] = zxingcpp
    except ImportError:
        missing.append("zxing-cpp")
    if missing:
        print("缺少依赖：" + ", ".join(missing), file=sys.stderr)
        print("请安装：pip install opencv-python numpy zxing-cpp", file=sys.stderr)
        print("或直接使用打包好的 dist/qrrecv.exe（无需安装 Python）。",
              file=sys.stderr)
        return None
    return modules["cv2"], modules["numpy"], modules["zxingcpp"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qrrecv",
        description="从视频或图片序列中读取 qrstream 二维码流，还原原始文件。",
        epilog="示例：\n"
               "  python qrrecv.py demo.mp4\n"
               "  python qrrecv.py screen_record.mp4 -o recovered.zip\n"
               "  python qrrecv.py frames/ --stride 1\n"
               "  python qrrecv.py demo.mp4 --start 3 --end 20\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", nargs="?",
                        help="视频文件，或包含帧图片的目录")
    parser.add_argument("--output", "-o", metavar="PATH",
                        help="输出文件路径；'-' 表示写到标准输出")
    parser.add_argument("--outdir", default=".",
                        help="未指定 --output 时的输出目录（默认当前目录）")
    parser.add_argument("--start", type=float, default=0.0,
                        help="从第几秒开始扫描（图片目录则按帧序号）")
    parser.add_argument("--end", type=float, default=None,
                        help="扫描到第几秒结束（图片目录则按帧序号）")
    parser.add_argument("--stride", type=int, default=0,
                        help="每 N 帧取一帧；0=自动（按视频 fps 约取 15 帧/秒），"
                             "1=每帧都扫")
    parser.add_argument("--max-frames", type=int, default=0,
                        help="最多扫描多少个采样帧（0=不限制）")
    parser.add_argument("--rotate", type=int, choices=(0, 90, 180, 270),
                        default=0, help="手动旋转每一帧")
    parser.add_argument("--overwrite", action="store_true",
                        help="允许覆盖已存在的输出文件")
    parser.add_argument("--no-progress", action="store_true",
                        help="不显示实时进度")
    parser.add_argument("--about", action="store_true",
                        help="显示版本与依赖说明")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = build_parser().parse_args(argv)

    if args.about:
        print(f"qrrecv {__version__}")
        print("从视频/图片序列读取 qrstream 二维码流，"
              "按 RaptorQR 兼容协议（js-rlnc）还原文件。")
        print("依赖：opencv-python、numpy、zxing-cpp；"
              "dist/qrrecv.exe 已内置这些依赖。")
        return 0

    if not args.source:
        print("错误：请提供视频文件或帧图片目录。", file=sys.stderr)
        return 2
    if not os.path.exists(args.source):
        print(f"错误：找不到输入：{args.source}", file=sys.stderr)
        return 1
    if args.stride < 0:
        print("错误：--stride 不能为负数。", file=sys.stderr)
        return 2

    deps = load_dependencies()
    if deps is None:
        return 2
    cv2, np, zxingcpp = deps

    try:
        source = open_source(cv2, np, args.source)
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    stride = args.stride
    if stride == 0:
        if source.kind == "video":
            stride = max(1, int(round(source.fps / 15.0)))
        else:
            stride = 1

    if source.kind == "video":
        print(f"输入：{args.source}", file=sys.stderr)
        print(f"视频：{source.width}x{source.height}  {source.fps:.3g} fps  "
              f"{source.total if source.total > 0 else '?'} 帧  "
              f"采样步长 {stride}", file=sys.stderr)
    else:
        print(f"输入：{args.source}（{source.total} 帧图片，步长 {stride}）",
              file=sys.stderr)

    transfer: Transfer | None = None
    result: TransferResult | None = None
    fatal: str | None = None
    scanned = 0
    qr_count = 0
    bad = 0
    reset_count = 0
    start_time = time.perf_counter()
    progress = Progress(source.total, enabled=not args.no_progress)

    barcode_format = zxingcpp.BarcodeFormat.QRCode

    try:
        for frame_idx, frame in source.frames(
                args.start, args.end, stride, args.max_frames, args.rotate):
            scanned += 1
            if frame is None:
                progress.update(scanned, qr_count, transfer,
                                transfer.duplicates if transfer else 0, bad)
                continue
            try:
                symbols = zxingcpp.read_barcodes(
                    frame, formats=barcode_format,
                    try_rotate=True, try_downscale=True, try_invert=True)
            except Exception:
                symbols = []
            qr_count += len(symbols)

            for symbol in symbols:
                try:
                    raw = bytes(symbol.bytes)
                except Exception:
                    continue
                if not raw:
                    continue
                try:
                    packet = parse_packet(raw)
                except RaptorQPacket:
                    fatal = ("该视频使用的是 RaptorQ 编码，当前接收端只支持"
                             " qrstream 的 js-rlnc 兼容编码。")
                    break
                except PacketError:
                    bad += 1
                    continue

                if transfer is not None and not transfer.matches(packet) \
                        and transfer.result is None:
                    reset_count += 1
                    if reset_count <= 3:
                        print(f"\n提示：检测到新的/不同的传输参数，重置接收状态。",
                              file=sys.stderr)
                    transfer = None

                if transfer is None:
                    try:
                        transfer = Transfer(packet)
                    except ProtocolError as exc:
                        bad += 1
                        if bad <= 3:
                            print(f"\n跳过参数不一致的数据包：{exc}",
                                  file=sys.stderr)
                        continue

                transfer.add(packet)
                if transfer.result is not None:
                    result = transfer.result
                    break

            if fatal or result is not None:
                break
            progress.update(scanned, qr_count, transfer,
                            transfer.duplicates if transfer else 0, bad)
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        progress.finish()
        return 130
    finally:
        source.close()

    progress.finish()
    elapsed = time.perf_counter() - start_time
    solved = transfer.progress[0] if transfer else 0
    need = transfer.progress[1] if transfer else 0
    accepted = transfer.accepted if transfer else 0

    print(f"扫描 {scanned} 帧，识别到 {qr_count} 个二维码，"
          f"接受 {accepted} 个数据包，解出 {solved}/{need} 代，"
          f"用时 {elapsed:.1f} 秒。", file=sys.stderr)

    if result is None:
        if fatal:
            print(f"错误：{fatal}", file=sys.stderr)
        elif qr_count == 0:
            print("没有识别到二维码。请确认：\n"
                  "  * 视频中的二维码完整、清晰、对焦准确；\n"
                  "  * 二维码没有被播放器裁切或拉伸；\n"
                  "  * 可以尝试 --stride 1 逐帧扫描（默认会跳帧）。",
                  file=sys.stderr)
        elif accepted == 0:
            print("识别到二维码，但没有解析出有效数据包。"
                  "可能是二维码版本/内容不匹配，或画面过于模糊。",
                  file=sys.stderr)
        else:
            extra = f"（{transfer.last_error}）" if transfer and transfer.last_error else ""
            print(f"数据不完整：还需要更多帧才能恢复{extra}。\n"
                  "可尝试：--stride 1、--rotate 90/180/270，"
                  "或录制更长/更清晰的视频。", file=sys.stderr)
        return 1

    path = write_output(result, args.output, args.outdir, args.overwrite)
    digest = hashlib.sha256(result.data).hexdigest()
    if path is None:
        print(f"已写出 {len(result.data)} 字节到标准输出，"
              f"SHA-256 {digest}", file=sys.stderr)
    else:
        print(f"完成：{os.path.abspath(path)}  "
              f"{len(result.data)} 字节  SHA-256 {digest}")
        if result.filename and not result.is_text:
            print(f"原始文件名：{result.filename}  MIME：{result.mime}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
