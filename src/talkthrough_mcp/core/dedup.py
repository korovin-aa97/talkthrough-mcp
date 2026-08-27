"""Perceptual dedup of extracted frames: dHash + Hamming distance + luma.

The 1 fps floor keeps static scenes flowing into the frame set; dHash marks
near-identical consecutive frames as duplicates so OCR and frame serving
work on unique frames only. Absolute mean luma is the tie-break dHash needs
for otherwise indistinguishable solid-color frames. Pillow-only (9x8
grayscale thumbnail), no numpy.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from .frames import Frame

HASH_SIZE = 8  # 8x8 bits from a 9x8 grayscale downscale
DEFAULT_HAMMING_THRESHOLD = 4
DEFAULT_MEAN_LUMA_THRESHOLD = 12.0


def _fingerprint_image(image: Image.Image, hash_size: int) -> tuple[int, float]:
    """Return the dHash and mean luma of the same grayscale thumbnail."""
    gray = image.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
    pixels = gray.tobytes()  # mode "L": one byte per pixel, row-major
    bits = 0
    for row in range(hash_size):
        for col in range(hash_size):
            left = pixels[row * (hash_size + 1) + col]
            right = pixels[row * (hash_size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits, sum(pixels) / len(pixels)


def dhash_image(image: Image.Image, hash_size: int = HASH_SIZE) -> int:
    """Difference hash: brightness gradient sign between horizontal neighbours."""
    return _fingerprint_image(image, hash_size)[0]


def dhash_file(path: Path, hash_size: int = HASH_SIZE) -> int:
    with Image.open(path) as image:
        return dhash_image(image, hash_size)


def _fingerprint_file(path: Path, hash_size: int = HASH_SIZE) -> tuple[int, float]:
    with Image.open(path) as image:
        return _fingerprint_image(image, hash_size)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def mark_duplicates(
    frames: list[Frame],
    frames_dir: Path,
    *,
    threshold: int = DEFAULT_HAMMING_THRESHOLD,
    mean_luma_threshold: float = DEFAULT_MEAN_LUMA_THRESHOLD,
) -> None:
    """Mark frames near-identical to the last unique frame as duplicates (in time order)."""
    last_unique_hash: int | None = None
    last_unique_luma: float | None = None
    last_unique_ms: int | None = None
    for frame in sorted(frames, key=lambda item: item.ms):
        frame_hash, frame_luma = _fingerprint_file(frames_dir / frame.file)
        if (
            last_unique_hash is not None
            and last_unique_luma is not None
            and last_unique_ms is not None
            and hamming(frame_hash, last_unique_hash) <= threshold
            # dHash intentionally discards absolute brightness. The separate
            # threshold keeps black/white and red/blue cuts reachable while
            # tolerating the small mean shifts introduced by JPEG encoding.
            and abs(frame_luma - last_unique_luma) <= mean_luma_threshold
        ):
            frame.duplicate_of = last_unique_ms
            continue
        frame.duplicate_of = None
        last_unique_hash = frame_hash
        last_unique_luma = frame_luma
        last_unique_ms = frame.ms
