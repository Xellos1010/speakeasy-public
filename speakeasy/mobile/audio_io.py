"""Multipart parsing + audio decoding for POST /v1/transcribe.

WAV PCM is decoded in-process; every other container (m4a/aac, ogg/webm opus, 3gp/amr) and any
non-16 kHz WAV goes through the workstation's own ffmpeg. Compressed input is written to a
private 0600 temp file because MP4 (m4a) needs a seekable input (moov atom at the end).
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Optional

import numpy as np

SAMPLE_RATE = 16000
MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_SECONDS = 600


class AudioError(Exception):
    pass


class UnsupportedAudio(AudioError):
    pass


def parse_multipart(content_type: str, body: bytes) -> tuple[dict, dict]:
    """Returns (fields: name->str, files: name->{"filename","content_type","data"})."""
    boundary = None
    for piece in content_type.split(";"):
        piece = piece.strip()
        if piece.lower().startswith("boundary="):
            boundary = piece.split("=", 1)[1].strip().strip('"')
    if not content_type.lower().startswith("multipart/form-data") or not boundary:
        raise AudioError("expected multipart/form-data with a boundary")
    delim = b"--" + boundary.encode("latin-1")
    fields: dict = {}
    files: dict = {}
    chunks = body.split(delim)
    for chunk in chunks[1:]:
        if chunk.startswith(b"--"):
            break
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        headers = {}
        for line in head.decode("utf-8", "replace").split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        disp = headers.get("content-disposition", "")
        params = {}
        for item in disp.split(";")[1:]:
            if "=" in item:
                k, v = item.split("=", 1)
                params[k.strip().lower()] = v.strip().strip('"')
        name = params.get("name")
        if not name:
            continue
        if "filename" in params or name == "audio":
            files[name] = {"filename": params.get("filename", ""),
                           "content_type": headers.get("content-type", ""), "data": data}
        else:
            fields[name] = data.decode("utf-8", "replace")
    return fields, files


def _ffmpeg() -> Optional[str]:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for cand in ("/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if os.path.exists(cand):
            return cand
    return None


def _decode_wav(data: bytes) -> Optional[np.ndarray]:
    try:
        w = wave.open(io.BytesIO(data))
    except (wave.Error, EOFError):
        return None
    with w:
        ch, rate, width, n = w.getnchannels(), w.getframerate(), w.getsampwidth(), w.getnframes()
        if rate != SAMPLE_RATE or width not in (1, 2, 4):
            return None
        frames = w.readframes(n)
    if width == 2:
        x = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        x = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        x = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return np.ascontiguousarray(x, dtype=np.float32)


def decode_audio(data: bytes, filename: str = "", tmp_dir: Optional[Path] = None,
                 run=subprocess.run) -> np.ndarray:
    if not data:
        raise AudioError("empty audio")
    if len(data) > MAX_AUDIO_BYTES:
        raise AudioError("audio too large")
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        x = _decode_wav(data)
        if x is not None:
            return _check_len(x)
    ff = _ffmpeg()
    if ff is None:
        raise UnsupportedAudio("only 16 kHz PCM WAV is supported on this host (no ffmpeg)")
    suffix = Path(filename).suffix[:8] if filename else ".bin"
    tmp_dir = Path(tmp_dir) if tmp_dir else None
    if tmp_dir:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(tmp_dir, 0o700)
    fd, path = tempfile.mkstemp(suffix=suffix or ".bin", dir=str(tmp_dir) if tmp_dir else None)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        proc = run([ff, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", path,
                    "-t", str(MAX_SECONDS), "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE),
                    "pipe:1"], capture_output=True, timeout=120)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if proc.returncode != 0 or not proc.stdout:
        raise UnsupportedAudio("could not decode audio (ffmpeg rc=%s)" % proc.returncode)
    x = np.frombuffer(proc.stdout[: len(proc.stdout) // 4 * 4], dtype="<f4").astype(np.float32)
    return _check_len(x)


def _check_len(x: np.ndarray) -> np.ndarray:
    if x.size == 0:
        raise AudioError("audio has no samples")
    if x.size > MAX_SECONDS * SAMPLE_RATE:
        raise AudioError("audio longer than 10 minutes")
    return x
