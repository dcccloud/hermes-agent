"""Perceptual hashing for screenshot-based page fingerprinting.

Provides lightweight visual fingerprints for app pages so that the
system can quickly determine "does this look like a page I've seen
before?" without calling the VLM every time.

Uses average-hash (aHash): resize → grayscale → threshold against mean.
The result is a compact hex string. Two hashes are compared via
Hamming distance — low distance means visually similar pages.
"""

from __future__ import annotations

from typing import Any

from openclaw_agent.engine.common.logger import get_logger

logger = get_logger("screenshot_hash")

_HASH_SIZE = 8          # 8x8 = 64-bit hash
_STATUS_BAR_CROP = 0.05  # crop top 5% to ignore time/battery


def compute_phash(device: Any, hash_size: int = _HASH_SIZE) -> str:
    """Capture a screenshot and compute its perceptual hash.

    Steps:
      1. Screenshot via u2 (PIL Image)
      2. Crop status bar (top 5%) to avoid clock/battery noise
      3. Resize to hash_size x hash_size, convert to grayscale
      4. Threshold each pixel against the mean → 1/0
      5. Pack bits into a hex string

    Returns:
        Hex string of length ``hash_size * hash_size // 4``.
    """
    img = device.screenshot(format="pillow")
    return compute_phash_from_image(img, hash_size=hash_size)


def compute_phash_from_image(
    img: Any, hash_size: int = _HASH_SIZE,
) -> str:
    """Compute perceptual hash from a PIL Image (no device needed)."""
    w, h = img.size
    crop_top = int(h * _STATUS_BAR_CROP)
    img = img.crop((0, crop_top, w, h))

    img = img.resize((hash_size, hash_size)).convert("L")

    pixels = list(img.getdata())
    mean_val = sum(pixels) / len(pixels)

    bits = 0
    for px in pixels:
        bits = (bits << 1) | (1 if px >= mean_val else 0)

    hex_len = (hash_size * hash_size) // 4
    return format(bits, f"0{hex_len}x")


def hamming_distance(hash1: str, hash2: str) -> int:
    """Compute the Hamming distance between two hex hash strings.

    Returns the number of differing bits (0 = identical).
    """
    if len(hash1) != len(hash2):
        raise ValueError(
            f"Hash length mismatch: {len(hash1)} vs {len(hash2)}")
    val1 = int(hash1, 16)
    val2 = int(hash2, 16)
    xor = val1 ^ val2
    return bin(xor).count("1")
