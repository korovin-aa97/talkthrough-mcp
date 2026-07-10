"""dHash, Hamming distance, and duplicate marking on known image pairs."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from talkthrough_mcp.core.dedup import dhash_file, dhash_image, hamming, mark_duplicates
from talkthrough_mcp.core.frames import Frame


def _image_with_box(left: int) -> Image.Image:
    image = Image.new("L", (320, 180), 240)
    draw = ImageDraw.Draw(image)
    draw.rectangle((left, 40, left + 90, 140), fill=10)
    return image


def test_identical_images_have_zero_distance() -> None:
    a = _image_with_box(30)
    b = _image_with_box(30)
    assert hamming(dhash_image(a), dhash_image(b)) == 0


def test_structurally_different_images_are_far_apart() -> None:
    a = _image_with_box(30)
    b = _image_with_box(200)
    assert hamming(dhash_image(a), dhash_image(b)) > 4


def test_mark_duplicates_chains_to_last_unique(tmp_path: Path) -> None:
    frames_dir = tmp_path
    _image_with_box(30).save(frames_dir / "t00000000.jpg")
    _image_with_box(30).save(frames_dir / "t00001000.jpg")  # same scene, 1s later
    _image_with_box(30).save(frames_dir / "t00002000.jpg")  # still the same scene
    _image_with_box(200).save(frames_dir / "t00006000.jpg")  # scene change

    frames = [
        Frame(ms=0, file="t00000000.jpg"),
        Frame(ms=1000, file="t00001000.jpg"),
        Frame(ms=2000, file="t00002000.jpg"),
        Frame(ms=6000, file="t00006000.jpg"),
    ]
    mark_duplicates(frames, frames_dir)

    assert frames[0].duplicate_of is None
    assert frames[1].duplicate_of == 0
    assert frames[2].duplicate_of == 0  # chained to the last UNIQUE frame, not the previous one
    assert frames[3].duplicate_of is None


def test_dhash_file_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "frame.jpg"
    image = _image_with_box(30)
    image.save(path, quality=95)
    assert hamming(dhash_file(path), dhash_image(image)) <= 2  # tolerate jpeg artifacts
