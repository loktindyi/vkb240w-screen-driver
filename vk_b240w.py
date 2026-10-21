from __future__ import annotations

import argparse
import ctypes
import datetime
import faulthandler
import itertools
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Callable, Iterator, Protocol

# NumPy here only does element-wise pixel conversion, never BLAS work.
# OpenBLAS reserves a per-thread buffer when NumPy is imported (~32 MB per
# logical CPU), which cost about 750 MB of commit charge on a 24-core machine.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import numpy as np
from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps


WIDTH = 320
HEIGHT = 240
FRAME_PREFIX = bytes.fromhex("ff 00 00 00 00 14 00 f0")
FRAME_SUFFIX = bytes.fromhex("ff c0 00 00 00 00 00 00")
FRAME_SIZE = WIDTH * HEIGHT * 2 + len(FRAME_PREFIX) + len(FRAME_SUFFIX)
CACHE_MAGIC = b"VKB240WCACHE1"
CACHE_END_MAGIC = b"VKB240WEND!"
CACHE_DURATION_SIZE = struct.calcsize("<d")
CACHE_FOOTER_SIZE = len(CACHE_END_MAGIC) + struct.calcsize("<Q")
USB_VENDOR_ID = 0x345F
USB_PRODUCT_ID = 0x9132
USB_INTERFACE_NUMBER = 3
USB_BULK_OUT_PIPE = 0x04
USB_TRANSFER_CHUNK_SIZES = (65_536, 65_536, 22_544)
USB_WRITE_TIMEOUT_MS = 5_000
USB_CLEANUP_TIMEOUT_MS = 1_000
DEFAULT_REFRESH_RATE = 5
HID_FEATURE_REPORT_SIZE = 9
VIDEO_WIDTH = 320
VIDEO_HEIGHT = 240
VIDEO_COLOR_UYVY = 0x22
VIDEO_MODE = 3
VIDEO_VIC = 0x94

STATUS_OTHER = {
    "power": 0xC454,
    "transfer_mode": 0xC558,
    "video_in": 0xC555,
    "video_out": 0xC557,
    "video_on": 0xC555,
    "transfer": 0xC555,
}


class FrameWriter(Protocol):
    def write_frame(self, chunks: tuple[bytes, ...]) -> None: ...


_RUN_LOGGER: RunLogger | None = None


def _cache_directory() -> Path:
    local_app_data = Path(
        os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
    )
    return local_app_data / "VKCache"


class RunLogger:
    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self._log_file = log_path.open("x", encoding="utf-8", buffering=1)
        self._crash_log_path = log_path.with_suffix(".crash.log")
        self._crash_file = self._crash_log_path.open("x", encoding="utf-8")
        self._started_at = time.monotonic()
        faulthandler.enable(file=self._crash_file, all_threads=True)
        self.log("process_started", pid=os.getpid())

    @classmethod
    def create(cls) -> RunLogger:
        log_dir = _cache_directory() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        log_path = log_dir / f"run_{timestamp}_{os.getpid()}.jsonl"
        return cls(log_path)

    def log(self, event: str, **details: object) -> None:
        record = {
            "timestamp": datetime.datetime.now().astimezone().isoformat(
                timespec="milliseconds"
            ),
            "event": event,
            "elapsed_seconds": round(time.monotonic() - self._started_at, 3),
            **details,
        }
        self._log_file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._log_file.flush()

    def close(self) -> None:
        self.log("process_stopped")
        faulthandler.disable()
        self._log_file.close()
        self._crash_file.close()
        if self._crash_log_path.stat().st_size == 0:
            self._crash_log_path.unlink()


def display_frames(
    writer: FrameWriter,
    frame: bytes,
    *,
    fps: float = DEFAULT_REFRESH_RATE,
    frame_count: int | None = None,
    stop_at: float | None = None,
) -> None:
    if fps <= 0:
        raise ValueError("fps must be greater than zero")
    if frame_count is not None and frame_count <= 0:
        raise ValueError("frame_count must be greater than zero")

    if stop_at is not None and time.monotonic() >= stop_at:
        return
    chunks = _frame_chunks(frame)
    interval = 1.0 / fps
    next_frame = time.monotonic()
    frames_sent = 0
    while frame_count is None or frames_sent < frame_count:
        if stop_at is not None and time.monotonic() >= stop_at:
            return
        writer.write_frame(chunks)
        frames_sent += 1
        if frame_count is not None and frames_sent == frame_count:
            break

        next_frame += interval
        delay = next_frame - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            next_frame = time.monotonic()


def animation_frames(
    path: Path,
    *,
    max_fps: float | None = None,
    loop: bool = True,
    on_video_metadata: Callable[[dict[str, object], float], None] | None = None,
) -> Iterator[tuple[Image.Image, float]]:
    if max_fps is not None and (
        not math.isfinite(max_fps) or max_fps <= 0
    ):
        raise ValueError("max_fps must be greater than zero")
    if path.suffix.lower() == ".gif":
        while True:
            with Image.open(path) as animation:
                frame_count = getattr(animation, "n_frames", 1)
                source_time = 0.0
                next_output_time = 0.0
                accumulated_duration = 0.0
                held_image: Image.Image | None = None
                for index in range(frame_count):
                    animation.seek(index)
                    duration_ms = animation.info.get("duration", 100)
                    duration = max(float(duration_ms) / 1000, 0.02)
                    image = animation.convert("RGB").copy()
                    if max_fps is None:
                        yield image, duration
                    else:
                        if held_image is None:
                            held_image = image
                            next_output_time = 1 / max_fps
                        elif source_time >= next_output_time:
                            yield held_image, accumulated_duration
                            held_image = image
                            accumulated_duration = 0.0
                            next_output_time = source_time + 1 / max_fps
                        accumulated_duration += duration
                    source_time += duration
                if max_fps is not None and held_image is not None:
                    yield held_image, accumulated_duration
            if not loop:
                return

    try:
        import imageio_ffmpeg
    except ImportError as error:
        raise OSError(
            "Video playback requires imageio-ffmpeg. Install it with: "
            "py -m pip install -r requirements.txt"
        ) from error

    output_filters = [
        "scale=320:240:force_original_aspect_ratio=decrease:force_divisible_by=2",
        "pad=320:240:(ow-iw)/2:(oh-ih)/2:color=black",
    ]
    if max_fps is not None:
        output_filters.insert(0, f"fps=fps=min(source_fps\\,{max_fps:g})")

    while True:
        reader = imageio_ffmpeg.read_frames(
            str(path),
            pix_fmt="rgb24",
            output_params=["-vf", ",".join(output_filters)],
        )
        frames_read = 0
        try:
            try:
                metadata = next(reader)
            except StopIteration as error:
                raise OSError(f"The video contains no frames: {path}") from error
            try:
                width, height = metadata["size"]
                source_fps = float(metadata["fps"])
            except (KeyError, TypeError, ValueError) as error:
                raise OSError(
                    f"Could not read video dimensions or frame rate: {path}"
                ) from error
            if (
                width <= 0
                or height <= 0
                or not math.isfinite(source_fps)
                or source_fps <= 0
            ):
                raise OSError(
                    f"The video has invalid dimensions or frame rate: {path}"
                )

            effective_fps = min(source_fps, max_fps) if max_fps else source_fps
            frame_duration = 1 / effective_fps
            if on_video_metadata is not None:
                on_video_metadata(metadata, effective_fps)
            for pixels in reader:
                expected_size = width * height * 3
                if len(pixels) != expected_size:
                    raise OSError(
                        f"Video decoder returned {len(pixels)} bytes; "
                        f"expected {expected_size} for {width}x{height} RGB."
                    )
                frames_read += 1
                yield Image.frombytes("RGB", (width, height), pixels), frame_duration
        finally:
            reader.close()
        if frames_read == 0:
            raise OSError(f"The video contains no frames: {path}")
        if not loop:
            return


def cache_animation_in_memory(
    path: Path, *, max_fps: float | None = None
) -> list[tuple[bytes, float]]:
    def report_estimate(metadata: dict[str, object], effective_fps: float) -> None:
        duration = metadata.get("duration")
        if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
            print(
                "Memory cache size cannot be estimated from video metadata; "
                "the exact size will be reported after decoding.",
                flush=True,
            )
            return
        estimated_frames = max(1, round(duration * effective_fps))
        estimated_bytes = estimated_frames * FRAME_SIZE
        print(
            f"Memory cache estimate: ~{estimated_bytes / 1_000_000:.1f} MB "
            f"({estimated_frames} frames at {FRAME_SIZE:,} bytes each; "
            "Python object overhead is additional). Loading the video once; "
            "FFmpeg will exit before playback.",
            flush=True,
        )

    if path.suffix.lower() == ".gif":
        with Image.open(path) as animation:
            estimated_frames = getattr(animation, "n_frames", 1)
        estimated_bytes = estimated_frames * FRAME_SIZE
        print(
            f"Memory cache estimate: ~{estimated_bytes / 1_000_000:.1f} MB "
            f"({estimated_frames} frames at {FRAME_SIZE:,} bytes each; "
            "Python object overhead is additional).",
            flush=True,
        )

    cached_frames = [
        (image_to_frame(image), duration)
        for image, duration in animation_frames(
            path,
            max_fps=max_fps,
            loop=False,
            on_video_metadata=report_estimate,
        )
    ]
    if not cached_frames:
        raise OSError(f"The animation contains no frames: {path}")

    actual_bytes = len(cached_frames) * FRAME_SIZE
    print(
        f"Memory cache ready: {len(cached_frames)} frames, "
        f"{actual_bytes / 1_000_000:.1f} MB of frame data.",
        flush=True,
    )
    return cached_frames


def _effective_animation_fps(path: Path, max_fps: float | None) -> float:
    if path.suffix.lower() == ".gif":
        if max_fps is not None:
            return max_fps
        with Image.open(path) as animation:
            frame_count = getattr(animation, "n_frames", 1)
            total_duration = 0.0
            for index in range(frame_count):
                animation.seek(index)
                total_duration += max(float(animation.info.get("duration", 100)), 20) / 1000
        return frame_count / total_duration

    try:
        import imageio_ffmpeg
    except ImportError as error:
        raise OSError(
            "Video playback requires imageio-ffmpeg. Install it with: "
            "py -m pip install -r requirements.txt"
        ) from error

    reader = imageio_ffmpeg.read_frames(str(path), pix_fmt="rgb24")
    try:
        metadata = next(reader)
        source_fps = float(metadata["fps"])
    except (StopIteration, KeyError, TypeError, ValueError) as error:
        raise OSError(f"Could not read video frame rate: {path}") from error
    finally:
        reader.close()
    if not math.isfinite(source_fps) or source_fps <= 0:
        raise OSError(f"The video has an invalid frame rate: {path}")
    return min(source_fps, max_fps) if max_fps is not None else source_fps


def _default_disk_cache_path(path: Path, fps: float) -> Path:
    fps_label = f"{fps:g}"
    return _cache_directory() / f"{path.stem}_{fps_label}fps.vkcache"


def _clear_disk_cache() -> int:
    cache_directory = _cache_directory()
    if not cache_directory.is_dir():
        return 0

    cache_files = (
        path for path in cache_directory.glob("*.vkcache") if path.is_file()
    )
    removed_count = 0
    for cache_file in cache_files:
        cache_file.unlink()
        removed_count += 1
    return removed_count


def _uninstall() -> None:
    cache_directory = _cache_directory()
    if cache_directory.exists():
        shutil.rmtree(cache_directory)


def _inspect_disk_cache(cache_path: Path, source_name: str) -> int | None:
    try:
        file_size = cache_path.stat().st_size
        with cache_path.open("rb") as cache:
            if cache.read(len(CACHE_MAGIC)) != CACHE_MAGIC:
                return None
            name_length_bytes = cache.read(2)
            if len(name_length_bytes) != 2:
                return None
            name_length = struct.unpack("<H", name_length_bytes)[0]
            encoded_name = cache.read(name_length)
            if len(encoded_name) != name_length:
                return None
            cached_name = encoded_name.decode("utf-8")
            if cached_name.casefold() != source_name.casefold():
                return None
            header_size = len(CACHE_MAGIC) + 2 + name_length
            if file_size < header_size + CACHE_FOOTER_SIZE:
                return None
            cache.seek(-CACHE_FOOTER_SIZE, os.SEEK_END)
            count_bytes = cache.read(8)
            end_magic = cache.read(len(CACHE_END_MAGIC))
            if len(count_bytes) != 8 or end_magic != CACHE_END_MAGIC:
                return None
            frame_count = struct.unpack("<Q", count_bytes)[0]
            expected_size = (
                header_size
                + frame_count * (CACHE_DURATION_SIZE + FRAME_SIZE)
                + CACHE_FOOTER_SIZE
            )
            if frame_count == 0 or file_size != expected_size:
                return None
            cache.seek(header_size)
            for _ in range(frame_count):
                duration_bytes = cache.read(CACHE_DURATION_SIZE)
                if len(duration_bytes) != CACHE_DURATION_SIZE:
                    return None
                duration = struct.unpack("<d", duration_bytes)[0]
                if not math.isfinite(duration) or duration <= 0:
                    return None
                cache.seek(FRAME_SIZE, os.SEEK_CUR)
            return frame_count
    except (FileNotFoundError, UnicodeDecodeError, struct.error):
        return None


def cache_animation_to_disk(
    path: Path,
    *,
    max_fps: float | None = None,
    cache_path: Path | None = None,
    flush: bool = False,
) -> Path:
    if cache_path is None:
        fps = _effective_animation_fps(path, max_fps)
        cache_path = _default_disk_cache_path(path, fps)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    source_name = path.name
    cached_count = None if flush else _inspect_disk_cache(cache_path, source_name)
    if cached_count is not None:
        if _RUN_LOGGER is not None:
            _RUN_LOGGER.log(
                "disk_cache_reused",
                source_file=source_name,
                frame_count=cached_count,
                cache_path=str(cache_path),
                cache_bytes=cache_path.stat().st_size,
            )
        print(
            f"Using disk cache for {source_name}: {cached_count} frames, "
            f"{cache_path.stat().st_size / 1_000_000:.1f} MB.",
            flush=True,
        )
        return cache_path

    if cache_path.exists():
        reason = "forced rebuild" if flush else "source filename changed or cache is invalid"
        if _RUN_LOGGER is not None:
            _RUN_LOGGER.log(
                "disk_cache_rebuild_started",
                source_file=source_name,
                reason=reason,
                cache_path=str(cache_path),
            )
        print(f"Rebuilding the single disk cache ({reason}).", flush=True)
    elif _RUN_LOGGER is not None:
        _RUN_LOGGER.log(
            "disk_cache_build_started",
            source_file=source_name,
            cache_path=str(cache_path),
        )

    encoded_name = source_name.encode("utf-8")
    if len(encoded_name) > 0xFFFF:
        raise ValueError("source filename is too long to mark in the disk cache")
    temp_path: Path | None = None

    def report_estimate(metadata: dict[str, object], effective_fps: float) -> None:
        duration = metadata.get("duration")
        if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
            print(
                "Disk cache size cannot be estimated from video metadata; "
                "the exact size will be reported after decoding.",
                flush=True,
            )
            return
        estimated_frames = max(1, round(duration * effective_fps))
        estimated_bytes = estimated_frames * (CACHE_DURATION_SIZE + FRAME_SIZE)
        print(
            f"Disk cache estimate: ~{estimated_bytes / 1_000_000:.1f} MB "
            f"for {estimated_frames} frames.",
            flush=True,
        )

    if path.suffix.lower() == ".gif":
        with Image.open(path) as animation:
            estimated_frames = getattr(animation, "n_frames", 1)
        estimated_bytes = estimated_frames * (CACHE_DURATION_SIZE + FRAME_SIZE)
        print(
            f"Disk cache estimate: ~{estimated_bytes / 1_000_000:.1f} MB "
            f"for {estimated_frames} GIF frames.",
            flush=True,
        )

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f"{cache_path.name}.",
            suffix=".tmp",
            dir=cache_path.parent,
            delete=False,
        ) as cache:
            temp_path = Path(cache.name)
            cache.write(CACHE_MAGIC)
            cache.write(struct.pack("<H", len(encoded_name)))
            cache.write(encoded_name)

            frame_count = 0
            for image, duration in animation_frames(
                path,
                max_fps=max_fps,
                loop=False,
                on_video_metadata=report_estimate,
            ):
                frame = image_to_frame(image)
                if len(frame) != FRAME_SIZE:
                    raise OSError(
                        f"Cannot cache invalid {len(frame)}-byte frame; "
                        f"expected {FRAME_SIZE} bytes."
                    )
                cache.write(struct.pack("<d", duration))
                cache.write(frame)
                frame_count += 1

            if frame_count == 0:
                raise OSError(f"The animation contains no frames: {path}")
            cache.write(struct.pack("<Q", frame_count))
            cache.write(CACHE_END_MAGIC)
            cache.flush()
            os.fsync(cache.fileno())

        os.replace(temp_path, cache_path)
        for previous_cache in cache_path.parent.glob("*.vkcache"):
            if previous_cache != cache_path:
                previous_cache.unlink()
    except (OSError, RuntimeError, ValueError):
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise

    print(
        f"Disk cache ready: {frame_count} frames, "
        f"{cache_path.stat().st_size / 1_000_000:.1f} MB at {cache_path}.",
        flush=True,
    )
    if _RUN_LOGGER is not None:
        _RUN_LOGGER.log(
            "disk_cache_written",
            source_file=source_name,
            frame_count=frame_count,
            cache_path=str(cache_path),
            cache_bytes=cache_path.stat().st_size,
        )
    return cache_path


def disk_cache_frames(
    cache_path: Path, source_name: str
) -> Iterator[tuple[bytes, float]]:
    while True:
        with cache_path.open("rb") as cache:
            if cache.read(len(CACHE_MAGIC)) != CACHE_MAGIC:
                raise OSError(f"Invalid VK B240W disk cache: {cache_path}")
            name_length_bytes = cache.read(2)
            if len(name_length_bytes) != 2:
                raise OSError(f"Truncated VK B240W disk cache: {cache_path}")
            name_length = struct.unpack("<H", name_length_bytes)[0]
            try:
                cached_name = cache.read(name_length).decode("utf-8")
            except UnicodeDecodeError as error:
                raise OSError(f"Invalid cache filename marker: {cache_path}") from error
            if cached_name.casefold() != source_name.casefold():
                raise OSError(
                    f"Disk cache is for {cached_name!r}, not {source_name!r}."
                )

            footer_offset = cache.seek(-CACHE_FOOTER_SIZE, os.SEEK_END)
            cache.seek(footer_offset)
            count_bytes = cache.read(8)
            if len(count_bytes) != 8 or cache.read(len(CACHE_END_MAGIC)) != CACHE_END_MAGIC:
                raise OSError(f"Incomplete VK B240W disk cache: {cache_path}")
            frame_count = struct.unpack("<Q", count_bytes)[0]

            header_size = len(CACHE_MAGIC) + 2 + name_length
            expected_size = (
                header_size
                + frame_count * (CACHE_DURATION_SIZE + FRAME_SIZE)
                + CACHE_FOOTER_SIZE
            )
            if (
                frame_count == 0
                or cache.seek(0, os.SEEK_END) != expected_size
            ):
                raise OSError(f"Invalid VK B240W disk cache size: {cache_path}")
            cache.seek(header_size)
            for _ in range(frame_count):
                duration_bytes = cache.read(CACHE_DURATION_SIZE)
                frame = cache.read(FRAME_SIZE)
                if len(duration_bytes) != CACHE_DURATION_SIZE or len(frame) != FRAME_SIZE:
                    raise OSError(f"Truncated frame in VK B240W disk cache: {cache_path}")
                yield frame, struct.unpack("<d", duration_bytes)[0]


def display_animation(
    writer: FrameWriter,
    frames: Iterator[tuple[Image.Image | bytes, float]],
    *,
    max_fps: float | None = None,
    frame_count: int | None = None,
    stop_at: float | None = None,
) -> None:
    if max_fps is not None and (
        not math.isfinite(max_fps) or max_fps <= 0
    ):
        raise ValueError("max_fps must be greater than zero")
    if frame_count is not None and frame_count <= 0:
        raise ValueError("frame_count must be greater than zero")

    timeline_start = time.monotonic()
    source_time = 0.0
    next_output_time = timeline_start
    frames_sent = 0
    selected_frames = (
        itertools.islice(frames, frame_count)
        if frame_count is not None
        else frames
    )
    for image, source_duration in selected_frames:
        if stop_at is not None and time.monotonic() >= stop_at:
            return
        if not math.isfinite(source_duration) or source_duration <= 0:
            raise ValueError("animation frame durations must be greater than zero")

        frame_time = timeline_start + source_time
        source_time += source_duration
        now = time.monotonic()
        output_interval = (
            max(source_duration, 1.0 / max_fps)
            if max_fps is not None
            else source_duration
        )
        if frames_sent and (
            frame_time < next_output_time
            or now >= frame_time + source_duration
        ):
            continue
        if frame_time > now:
            sleep_duration = frame_time - now
            if stop_at is not None:
                sleep_duration = min(sleep_duration, max(0, stop_at - now))
            if sleep_duration > 0:
                time.sleep(sleep_duration)

        frame = image if isinstance(image, bytes) else image_to_frame(image)
        send_frame(writer, frame)
        next_output_time = frame_time + output_interval
        frames_sent += 1


def _to_rgb(image: Image.Image) -> Image.Image:
    if "A" in image.getbands() or "transparency" in image.info:
        rgba = image.convert("RGBA")
        rgb = Image.new("RGB", rgba.size, "black")
        rgb.paste(rgba, mask=rgba.getchannel("A"))
        return rgb
    return image.convert("RGB")


def image_to_frame(image: Image.Image) -> bytes:
    """Convert an image into the captured 320x240 UYVY frame envelope."""
    image = ImageOps.contain(_to_rgb(image), (WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (WIDTH, HEIGHT), "black")
    canvas.paste(image, ((WIDTH - image.width) // 2, (HEIGHT - image.height) // 2))

    body = rgb_to_uyvy(canvas.tobytes(), WIDTH, HEIGHT)

    return FRAME_PREFIX + body + FRAME_SUFFIX


def rgb_to_uyvy(rgb: bytes, width: int, height: int) -> bytes:
    if width % 2:
        raise ValueError("UYVY width must be even")
    if len(rgb) != width * height * 3:
        raise ValueError("RGB byte count does not match image dimensions")

    pixels = np.frombuffer(rgb, dtype=np.uint8).reshape(height, width, 3).astype(
        np.int32
    )
    body = np.empty((height, width // 2, 4), dtype=np.uint8)
    x_even = slice(0, width, 2)
    x_odd = slice(1, width, 2)
    for y in range(height):
        row = pixels[y]
        y0 = ((33 * row[x_even, 0] + 65 * row[x_even, 1] + 13 * row[x_even, 2]) >> 7) + 16
        y1 = ((33 * row[x_odd, 0] + 65 * row[x_odd, 1] + 13 * row[x_odd, 2]) >> 7) + 16
        if y + 1 < height:
            next_row = pixels[y + 1]
            r = (
                row[x_even, 0] + row[x_odd, 0]
                + next_row[x_even, 0] + next_row[x_odd, 0]
            ) // 4
            g = (
                row[x_even, 1] + row[x_odd, 1]
                + next_row[x_even, 1] + next_row[x_odd, 1]
            ) // 4
            b = (
                row[x_even, 2] + row[x_odd, 2]
                + next_row[x_even, 2] + next_row[x_odd, 2]
            ) // 4
        else:
            r = (row[x_even, 0] + row[x_odd, 0]) // 2
            g = (row[x_even, 1] + row[x_odd, 1]) // 2
            b = (row[x_even, 2] + row[x_odd, 2]) // 2

        body[y, :, 0] = ((112 * b - 74 * g - 38 * r - 32640) >> 8) & 0xFF
        body[y, :, 1] = y0 & 0xFF
        body[y, :, 2] = ((112 * r - 94 * g - 18 * b - 32640) >> 8) & 0xFF
        body[y, :, 3] = y1 & 0xFF

    return body.tobytes()


def _load_font(font_path: str | None, font_size: int) -> ImageFont.ImageFont:
    if font_path:
        return ImageFont.truetype(font_path, font_size)

    windows_fonts = Path(r"C:\Windows\Fonts")
    for name in ("msyh.ttc", "simhei.ttf", "arial.ttf"):
        candidate = windows_fonts / name
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), font_size)
    return ImageFont.load_default()


def text_to_frame(
    text: str,
    *,
    color: str = "white",
    background: str = "black",
    font_size: int = 24,
    font_path: str | None = None,
) -> bytes:
    if font_size <= 0:
        raise ValueError("font_size must be greater than zero")

    canvas = Image.new("RGB", (WIDTH, HEIGHT), ImageColor.getrgb(background))
    draw = ImageDraw.Draw(canvas)
    font = _load_font(font_path, font_size)
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        line = ""
        for character in paragraph:
            candidate = line + character
            if line and draw.textlength(candidate, font=font) > WIDTH - 16:
                lines.append(line)
                line = character
            else:
                line = candidate
        lines.append(line)

    draw.multiline_text(
        (8, 8),
        "\n".join(lines),
        fill=ImageColor.getrgb(color),
        font=font,
        spacing=4,
    )
    return image_to_frame(canvas)


def test_pattern_to_frame() -> bytes:
    canvas = Image.new("RGB", (WIDTH, HEIGHT), "black")
    draw = ImageDraw.Draw(canvas)
    colors = (
        "#FFFFFF",
        "#FFFF00",
        "#00FFFF",
        "#00FF00",
        "#FF00FF",
        "#FF0000",
        "#0000FF",
        "#000000",
    )
    bar_width = WIDTH // len(colors)
    for index, color in enumerate(colors):
        left = index * bar_width
        right = WIDTH - 1 if index == len(colors) - 1 else (index + 1) * bar_width - 1
        draw.rectangle((left, 0, right, HEIGHT - 1), fill=color)
    draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline="#808080")
    draw.line((WIDTH // 2, 0, WIDTH // 2, HEIGHT - 1), fill="#808080")
    draw.line((0, HEIGHT // 2, WIDTH - 1, HEIGHT // 2), fill="#808080")
    return image_to_frame(canvas)


def send_frame(writer: FrameWriter, frame: bytes) -> None:
    writer.write_frame(_frame_chunks(frame))


def _frame_chunks(frame: bytes) -> tuple[bytes, ...]:
    if len(frame) != FRAME_SIZE:
        raise ValueError(f"expected a {FRAME_SIZE}-byte frame, got {len(frame)}")
    if not frame.startswith(FRAME_PREFIX) or not frame.endswith(FRAME_SUFFIX):
        raise ValueError("frame does not have the VK B240W capture envelope")

    chunks: list[bytes] = []
    offset = 0
    for size in USB_TRANSFER_CHUNK_SIZES:
        chunks.append(frame[offset : offset + size])
        offset += size
    if offset != len(frame):
        raise ValueError("USB transfer sizes do not match the frame size")
    return tuple(chunks)


class MS912xControl:
    def __init__(self, hid_device) -> None:
        self._hid_device = hid_device
        self._output_enabled = False
        self._next_frame_id = 1
        self.video_port = 0
        self.sdram_type = 0

    @classmethod
    def open(cls) -> MS912xControl:
        try:
            import hid
        except ImportError as error:
            raise OSError(
                "Install the Python dependencies with: py -m pip install -r requirements.txt"
            ) from error

        candidates = hid.enumerate(USB_VENDOR_ID, USB_PRODUCT_ID)
        interface = next(
            (
                item
                for item in candidates
                if item.get("interface_number") == 0
                or b"&mi_00" in bytes(item.get("path", b"")).lower()
            ),
            None,
        )
        if interface is None:
            raise OSError(
                "The VK B240W HID control interface (MI_00) was not found."
            )

        device = hid.device()
        try:
            device.open_path(interface["path"])
        except OSError as error:
            raise OSError(
                "Could not open the VK B240W HID control interface. Close MythCool "
                f"or other software using the screen, then try again: {error}"
            ) from error
        return cls(device)

    def close(self) -> None:
        self._hid_device.close()

    def _set_feature(self, payload: bytes) -> None:
        report = bytes((0,)) + payload[:8].ljust(8, b"\x00")
        sent = self._hid_device.send_feature_report(report)
        if sent != len(report):
            raise OSError(
                f"Short HID Feature Report write: sent {sent} of {len(report)} bytes"
            )

    def _get_feature(self) -> bytes:
        report = bytes(self._hid_device.get_feature_report(0, HID_FEATURE_REPORT_SIZE))
        if len(report) != HID_FEATURE_REPORT_SIZE or report[0] != 0:
            raise OSError(
                "The VK B240W returned an invalid HID Feature Report "
                f"(expected {HID_FEATURE_REPORT_SIZE} bytes with report ID 0)."
            )
        return report[1:]

    def _xdata_read(self, address: int) -> int:
        self._set_feature(
            bytes((0xB5, address >> 8, address & 0xFF, 0, 0, 0, 0, 0))
        )
        return self._get_feature()[3]

    def _xdata_read_bytes(self, address: int, count: int) -> bytes:
        result = bytearray()
        offset = 0
        while offset < count:
            self._set_feature(
                bytes(
                    (
                        0xB5,
                        (address + offset) >> 8,
                        (address + offset) & 0xFF,
                        0,
                        0,
                        0,
                        0,
                        0,
                    )
                )
            )
            result.extend(self._get_feature()[3 : 3 + min(4, count - offset)])
            offset += 4
        return bytes(result)

    def _xdata_write(self, address: int, value: int) -> None:
        self._set_feature(
            bytes((0xB6, address >> 8, address & 0xFF, value & 0xFF))
        )

    def _wait_command(self, key: str, settle_seconds: float) -> None:
        time.sleep(settle_seconds)
        address = STATUS_OTHER[key]
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if self._xdata_read(address) == 0:
                return
            time.sleep(0.002)
        raise OSError(
            f"MS912x command {key} did not complete; status register "
            f"0x{address:04X} remained nonzero."
        )

    def _command(
        self,
        payload: bytes,
        *,
        status: str | None = None,
        settle_seconds: float = 0.02,
    ) -> None:
        self._set_feature(payload)
        if status is not None:
            self._wait_command(status, settle_seconds)
        elif settle_seconds:
            time.sleep(settle_seconds)

    def _set_video_in(self, width: int, height: int, color: int) -> None:
        self._command(
            bytes(
                (
                    0xA6,
                    0x01,
                    width >> 8,
                    width & 0xFF,
                    height >> 8,
                    height & 0xFF,
                    color,
                    0,
                )
            ),
            status="video_in",
            settle_seconds=0.01,
        )

    def _set_video_out(self, width: int, height: int, vic: int) -> None:
        self._command(
            bytes(
                (
                    0xA6,
                    0x02,
                    vic,
                    0,
                    width >> 8,
                    width & 0xFF,
                    height >> 8,
                    height & 0xFF,
                )
            ),
            status="video_out",
        )

    def _set_screen_on(self, enabled: bool) -> None:
        if self.video_port != 5:
            raise OSError(
                f"Unsupported MS912x video port {self.video_port}; "
                "the VK B240W port-5 screen gate is not known."
            )
        address = 0xF507
        bit = 0x02
        current = self._xdata_read(address)
        updated = (current | bit) if not enabled else (current & ~bit)
        if updated != current:
            self._xdata_write(address, updated)

    def initialize_stream(self) -> None:
        signature = self._xdata_read_bytes(0xF000, 3)
        if signature[1:] != b"\x16\x0a":
            raise OSError(
                f"Unsupported display controller signature at 0xF000: "
                f"{signature.hex(' ')}"
            )

        signature_9132 = self._xdata_read_bytes(0xFF00, 3)
        if signature_9132[1:] == b"\x13\x0a":
            raise OSError(
                "The display reports the MS9132 controller family; its command "
                "status registers differ from the verified MS912x path."
            )
        self.video_port = self._xdata_read(0x0031)
        self.sdram_type = self._xdata_read(0x0030)
        hpd = self._xdata_read(0x0032)
        if self.video_port != 5 or hpd == 0:
            raise OSError(
                f"Unexpected display controller state: video port {self.video_port}, "
                f"HPD {hpd}."
            )

        self._command(
            bytes((0xA6, 0x03, VIDEO_MODE)), status="transfer_mode", settle_seconds=0.01
        )
        self._xdata_write(0xDEEE, 1)
        self._command(
            bytes((0xA6, 0x07, 1)), status="power", settle_seconds=0.1
        )
        self._command(
            bytes((0xA6, 0x05, 0)), status="video_on", settle_seconds=0.01
        )
        self._set_screen_on(False)

        self._set_video_in(1920, 1080, VIDEO_COLOR_UYVY)
        self._set_video_out(1920, 1080, 0x81)
        time.sleep(0.05)
        self._set_video_in(VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_COLOR_UYVY)
        self._set_video_out(VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_VIC)
        self._command(
            bytes((0xA6, 0x04, 1)), status="transfer", settle_seconds=0.01
        )

    def frame_ready(self) -> None:
        frame_id = self._next_frame_id
        self._next_frame_id ^= 1
        self._command(
            bytes((0xA6, 0x00, frame_id, 100)), settle_seconds=0
        )
        if self._output_enabled:
            return
        self._set_screen_on(True)
        self._command(
            bytes((0xA6, 0x05, 1)), status="video_on", settle_seconds=0.01
        )
        self._output_enabled = True


class LibUSBTransport:
    def __init__(self) -> None:
        try:
            import usb.core
            import usb.util
            from usb.backend import libusb0
        except ImportError as error:
            raise OSError(
                "Install the Python dependencies with: py -m pip install -r requirements.txt"
            ) from error

        self._usb_core = usb.core
        self._usb_util = usb.util
        self._device = None
        self._claimed = False
        self._control: MS912xControl | None = None
        self._frame_buffers: tuple[ctypes.Array[ctypes.c_ubyte], ...] | None = None

        backend = libusb0.get_backend()
        if backend is None:
            raise OSError(
                "Could not load libusb0.dll. Install the libusb-win32 device "
                "driver and make sure its DLL is available."
            )
        # PyUSB exposes synchronous writes only; the capture queues all three
        # bulk transfers before waiting for any completion.
        self._usb_library = libusb0._lib
        if any(
            not hasattr(self._usb_library, name)
            for name in (
                "usb_bulk_setup_async",
                "usb_submit_async",
                "usb_reap_async",
                "usb_cancel_async",
                "usb_reap_async_nocancel",
                "usb_free_async",
            )
        ):
            raise OSError(
                "The installed libusb0.dll does not provide asynchronous "
                "bulk transfers required by the display."
            )

        try:
            self._device = self._usb_core.find(
                idVendor=USB_VENDOR_ID,
                idProduct=USB_PRODUCT_ID,
                backend=backend,
            )
        except self._usb_core.USBError as error:
            raise OSError(f"Could not enumerate the VK B240W USB device: {error}") from error

        if self._device is None:
            raise OSError(
                "USB\\VID_345F&PID_9132 was not found. Check that the cooler is connected "
                "and its MI_03 interface has the libusb-win32 driver."
            )

        try:
            configuration = self._device.get_active_configuration()
            interface = next(
                (
                    configuration[(index, 0)]
                    for index in range(configuration.bNumInterfaces)
                    if configuration[(index, 0)].bInterfaceNumber
                    == USB_INTERFACE_NUMBER
                ),
                None,
            )
            if interface is None:
                raise OSError("USB interface 3 was not found in the active configuration.")
            endpoint = self._usb_util.find_descriptor(
                interface,
                custom_match=lambda item: (
                    item.bEndpointAddress == USB_BULK_OUT_PIPE
                    and item.bmAttributes & 0x03 == self._usb_util.ENDPOINT_TYPE_BULK
                ),
            )
            if endpoint is None:
                raise OSError(
                    "Bulk OUT endpoint 0x04 was not found on interface 3."
                )
            self._usb_util.claim_interface(self._device, USB_INTERFACE_NUMBER)
            self._claimed = True
            self._control = MS912xControl.open()
            self._control.initialize_stream()
        except self._usb_core.USBError as error:
            self.close()
            raise OSError(
                "Could not claim the VK B240W display interface. Close MythCool "
                "or other software using the screen, then try again: "
                f"{error}"
            ) from error
        except OSError:
            self.close()
            raise

    def _check_libusb_result(self, operation: str, result: int) -> int:
        if result >= 0:
            return result
        import usb.core

        message = self._usb_library.usb_strerror().decode(errors="replace")
        raise usb.core.USBError(f"libusb0 {operation} failed: {message}", result)

    def write_frame(self, chunks: tuple[bytes, ...]) -> None:
        if self._device is None:
            raise OSError("The USB device is closed.")

        if tuple(map(len, chunks)) != USB_TRANSFER_CHUNK_SIZES:
            raise ValueError(
                f"expected USB frame chunks of {USB_TRANSFER_CHUNK_SIZES}, "
                f"got {tuple(map(len, chunks))}"
            )

        handle = self._device._ctx.handle
        contexts: list[ctypes.c_void_p] = []
        if self._frame_buffers is None:
            self._frame_buffers = tuple(
                (ctypes.c_ubyte * size)() for size in USB_TRANSFER_CHUNK_SIZES
            )
        buffers = self._frame_buffers
        for buffer, chunk in zip(buffers, chunks):
            ctypes.memmove(buffer, chunk, len(chunk))
        pending: set[int] = set()
        try:
            for index, chunk in enumerate(chunks):
                context = ctypes.c_void_p()
                result = self._usb_library.usb_bulk_setup_async(
                    handle,
                    ctypes.byref(context),
                    USB_BULK_OUT_PIPE,
                )
                self._check_libusb_result(
                    f"setup for frame chunk {index + 1}", result
                )
                contexts.append(context)

                buffer = buffers[index]
                result = self._usb_library.usb_submit_async(
                    context,
                    ctypes.cast(buffer, ctypes.c_char_p),
                    len(chunk),
                )
                self._check_libusb_result(
                    f"submission for frame chunk {index + 1}", result
                )
                pending.add(index)

            for index, context in enumerate(contexts):
                result = self._usb_library.usb_reap_async(
                    context, USB_WRITE_TIMEOUT_MS
                )
                transferred = self._check_libusb_result(
                    f"completion for frame chunk {index + 1}", result
                )
                pending.remove(index)
                if transferred != len(chunks[index]):
                    raise OSError(
                        f"Short USB write: sent {transferred} of "
                        f"{len(chunks[index])} bytes in frame chunk {index + 1}"
                    )

            transferred = self._device.write(
                USB_BULK_OUT_PIPE, b"", USB_WRITE_TIMEOUT_MS
            )
            if transferred != 0:
                raise OSError(
                    f"Unexpected nonzero result from USB frame terminator: {transferred}"
                )
            if self._control is None:
                raise OSError("The MS912x display control interface is closed.")
            self._control.frame_ready()
        except (self._usb_core.USBError, OSError) as error:
            cleanup_errors = []
            for index in sorted(pending):
                result = self._usb_library.usb_cancel_async(contexts[index])
                if result < 0:
                    cleanup_errors.append(f"cancel chunk {index + 1}: {result}")
            for index in sorted(pending):
                result = self._usb_library.usb_reap_async_nocancel(
                    contexts[index], USB_CLEANUP_TIMEOUT_MS
                )
                if result < 0:
                    cleanup_errors.append(f"reap cancelled chunk {index + 1}: {result}")
            cleanup_detail = (
                f"; async cleanup errors: {', '.join(cleanup_errors)}"
                if cleanup_errors
                else ""
            )
            raise OSError(
                f"USB bulk frame transfer failed: {error}{cleanup_detail}"
            ) from error
        finally:
            for context in contexts:
                result = self._usb_library.usb_free_async(ctypes.byref(context))
                self._check_libusb_result("free async transfer", result)


    def close(self) -> None:
        if self._control is not None:
            self._control.close()
            self._control = None
        self._frame_buffers = None
        if self._device is None:
            return
        try:
            if self._claimed:
                self._usb_util.release_interface(
                    self._device, USB_INTERFACE_NUMBER
                )
                self._claimed = False
        finally:
            self._usb_util.dispose_resources(self._device)
            self._device = None

    def __enter__(self) -> LibUSBTransport:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()


def _open_transport_with_retries(startup_attempts: int) -> LibUSBTransport:
    for attempt in range(1, startup_attempts + 1):
        try:
            return LibUSBTransport()
        except OSError as error:
            retryable = (
                "MS912x command" in str(error)
                and "did not complete" in str(error)
            )
            if not retryable or attempt == startup_attempts:
                raise
            message = (
                f"Display initialization did not complete "
                f"(attempt {attempt}/{startup_attempts}); retrying in 1 second: {error}"
            )
            print(message, file=sys.stderr, flush=True)
            if _RUN_LOGGER is not None:
                _RUN_LOGGER.log(
                    "startup_retry",
                    attempt=attempt,
                    startup_attempts=startup_attempts,
                    error=str(error),
                )
            time.sleep(1)
    raise RuntimeError("transport startup retry loop ended unexpectedly")


def _run_stream_session(
    stream: Callable[[LibUSBTransport, float | None], None],
    *,
    startup_attempts: int,
    restart_after_hours: float,
) -> bool:
    with _open_transport_with_retries(startup_attempts) as device:
        if _RUN_LOGGER is not None:
            _RUN_LOGGER.log("usb_device_ready")
        deadline = (
            time.monotonic() + restart_after_hours * 60 * 60
            if restart_after_hours > 0
            else None
        )
        if deadline is not None and _RUN_LOGGER is not None:
            _RUN_LOGGER.log(
                "scheduled_process_restart",
                restart_after_hours=restart_after_hours,
            )
        stream(device, deadline)
    return deadline is not None and time.monotonic() >= deadline


def _restart_process() -> None:
    global _RUN_LOGGER
    if _RUN_LOGGER is not None:
        _RUN_LOGGER.log("process_restarting", reason="restart_after duration reached")
        _RUN_LOGGER.close()
        _RUN_LOGGER = None
    os.execv(sys.executable, [sys.executable, *sys.argv])
    raise OSError("Python process restart unexpectedly returned")


def _launch_hidden_python() -> None:
    if os.name != "nt":
        raise OSError("--hide is supported only on Windows.")
    executable = Path(sys.executable)
    if executable.stem.casefold() == "pythonw":
        return

    hidden_executable = executable.with_name("pythonw.exe")
    if not hidden_executable.is_file():
        raise OSError(
            f"Could not find the windowless Python interpreter: {hidden_executable}"
        )

    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup_info.wShowWindow = subprocess.SW_HIDE
    try:
        subprocess.Popen(
            [str(hidden_executable), *sys.argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=startup_info,
            creationflags=subprocess.CREATE_NO_WINDOW,
            close_fds=True,
            shell=False,
        )
    except OSError as error:
        raise OSError(f"Could not start hidden Python process: {error}") from error


def _redirect_hidden_stdio() -> None:
    sys.stdin = open(os.devnull, "r", encoding="utf-8")
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _add_runtime_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--hide",
        action="store_true",
        help="run without a console window (Windows only)",
    )
    parser.add_argument(
        "--startup-attempts",
        type=int,
        default=3,
        help=(
            "maximum attempts to initialize the display after an MS912x command "
            "timeout (default: 3)"
        ),
    )
    parser.add_argument(
        "--restart-after",
        type=float,
        default=12,
        metavar="HOURS",
        help=(
            "restart the Python process after this many streaming hours; "
            "0 disables automatic restart (default: 12)"
        ),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Display images, text, GIFs, or videos on the VK B240W screen."
    )
    maintenance_options = parser.add_mutually_exclusive_group()
    maintenance_options.add_argument(
        "--clear-cache",
        action="store_true",
        help="delete only .vkcache files",
    )
    maintenance_options.add_argument(
        "--uninstall",
        action="store_true",
        help="delete the entire VKCache folder",
    )
    subparsers = parser.add_subparsers(dest="command")

    image_parser = subparsers.add_parser("image", help="display an image file")
    image_parser.add_argument("file", type=Path)
    image_parser.add_argument(
        "--fps", type=float, default=DEFAULT_REFRESH_RATE,
        help=f"refresh rate while streaming (default: {DEFAULT_REFRESH_RATE})",
    )
    image_parser.add_argument(
        "--once", action="store_true",
        help="send a single frame instead of continuously refreshing the screen",
    )
    image_parser.add_argument(
        "--no-log",
        action="store_true",
        help="disable the default runtime log file",
    )
    _add_runtime_options(image_parser)
    text_parser = subparsers.add_parser("text", help="display simple text")
    text_parser.add_argument("text")
    text_parser.add_argument("--color", default="white")
    text_parser.add_argument("--background", default="black")
    text_parser.add_argument("--font-size", type=int, default=24)
    text_parser.add_argument("--font", dest="font_path")
    text_parser.add_argument(
        "--fps", type=float, default=DEFAULT_REFRESH_RATE,
        help=f"refresh rate while streaming (default: {DEFAULT_REFRESH_RATE})",
    )
    text_parser.add_argument(
        "--once", action="store_true",
        help="send a single frame instead of continuously refreshing the screen",
    )
    text_parser.add_argument(
        "--no-log",
        action="store_true",
        help="disable the default runtime log file",
    )
    _add_runtime_options(text_parser)
    play_parser = subparsers.add_parser(
        "play", help="loop a GIF or video file on the screen"
    )
    play_parser.add_argument("file", type=Path)
    play_parser.add_argument(
        "--max-fps", type=float,
        help="optional maximum frame rate; by default follow the source frame rate",
    )
    play_parser.add_argument(
        "--cache-memory",
        action="store_true",
        help="decode and encode once into RAM, then play without keeping FFmpeg running",
    )
    play_parser.add_argument(
        "--cache-disk",
        action="store_true",
        help="decode and encode once into a reusable single-file disk cache",
    )
    play_parser.add_argument(
        "--flush-cache",
        action="store_true",
        help="force regeneration of the disk cache (requires --cache-disk)",
    )
    play_parser.add_argument(
        "--no-log",
        action="store_true",
        help="disable the default runtime log file",
    )
    _add_runtime_options(play_parser)
    pattern_parser = subparsers.add_parser(
        "test-pattern", help="display eight color bars for screen calibration"
    )
    pattern_parser.add_argument(
        "--fps", type=float, default=DEFAULT_REFRESH_RATE,
        help=f"refresh rate while streaming (default: {DEFAULT_REFRESH_RATE})",
    )
    pattern_parser.add_argument(
        "--once", action="store_true",
        help="send a single frame instead of continuously refreshing the screen",
    )
    pattern_parser.add_argument(
        "--no-log",
        action="store_true",
        help="disable the default runtime log file",
    )
    _add_runtime_options(pattern_parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    supplied_args = argv if argv is not None else sys.argv[1:]
    if os.name == "nt" and (
        sys.stdin is None or sys.stdout is None or sys.stderr is None
    ):
        _redirect_hidden_stdio()
    if (
        os.name == "nt"
        and "--hide" in supplied_args
        and Path(sys.executable).stem.casefold() == "pythonw"
    ):
        _redirect_hidden_stdio()
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.clear_cache or args.uninstall:
        if len(supplied_args) != 1:
            parser.error("--clear-cache and --uninstall must be used alone")
        try:
            if args.clear_cache:
                removed_count = _clear_disk_cache()
                print(f"Removed {removed_count} .vkcache file(s).")
            else:
                _uninstall()
                print("Removed the VKCache folder.")
        except OSError as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1
        return 0
    if args.command is None:
        parser.error("a command is required")
    if args.startup_attempts < 1:
        parser.error("--startup-attempts must be at least 1")
    if not math.isfinite(args.restart_after) or args.restart_after < 0:
        parser.error("--restart-after must be a non-negative number of hours")
    if args.command == "play":
        if args.cache_memory and args.cache_disk:
            parser.error("--cache-memory and --cache-disk cannot be used together")
        if args.flush_cache and not args.cache_disk:
            parser.error("--flush-cache requires --cache-disk")
        if args.max_fps is not None and (
            not math.isfinite(args.max_fps) or args.max_fps <= 0
        ):
            parser.error("--max-fps must be greater than zero")
    elif not math.isfinite(args.fps) or args.fps <= 0:
        parser.error("--fps must be greater than zero")

    logger: RunLogger | None = None
    global _RUN_LOGGER
    try:
        if args.hide and os.name != "nt":
            parser.error("--hide is supported only on Windows")
        if (
            args.hide
            and Path(sys.executable).stem.casefold() != "pythonw"
            and "__compiled__" not in globals()
        ):
            _launch_hidden_python()
            return 0

        if not args.no_log:
            logger = RunLogger.create()
            _RUN_LOGGER = logger
            logger.log(
                "command_started",
                command=args.command,
                source_file=args.file.name if hasattr(args, "file") else None,
                max_fps=getattr(args, "max_fps", None),
                fps=getattr(args, "fps", None),
                cache_memory=getattr(args, "cache_memory", False),
                cache_disk=getattr(args, "cache_disk", False),
                once=getattr(args, "once", False),
                font_size=getattr(args, "font_size", None),
                text_color=getattr(args, "color", None),
                background_color=getattr(args, "background", None),
                startup_attempts=args.startup_attempts,
                restart_after_hours=args.restart_after,
            )
            if not args.hide:
                print(f"Log file: {logger.log_path}", flush=True)

        if args.command == "image":
            if logger is not None:
                logger.log("phase_started", phase="image_load_and_encode")
            with Image.open(args.file) as source:
                source.load()
                frame = image_to_frame(source)
        elif args.command == "test-pattern":
            if logger is not None:
                logger.log("phase_started", phase="test_pattern_encode")
            frame = test_pattern_to_frame()
        elif args.command == "play":
            if logger is not None:
                logger.log(
                    "playback_started",
                    source_file=args.file.name,
                    cache_mode=(
                        "memory"
                        if args.cache_memory
                        else "disk"
                        if args.cache_disk
                        else "none"
                    ),
                )
            print(
                f"Looping {args.file}"
                + (
                    f" at up to {args.max_fps:g} FPS"
                    if args.max_fps is not None
                    else " at the source frame rate"
                )
                + "; press Ctrl+C to stop.",
                flush=True,
            )
            if logger is not None:
                logger.log(
                    "phase_started",
                    phase="cache_memory_build"
                    if args.cache_memory
                    else "cache_disk_build"
                    if args.cache_disk
                    else "playback_decode",
                )
            cached_frames = (
                cache_animation_in_memory(args.file, max_fps=args.max_fps)
                if args.cache_memory
                else None
            )
            disk_cache = (
                cache_animation_to_disk(
                    args.file,
                    max_fps=args.max_fps,
                    flush=args.flush_cache,
                )
                if args.cache_disk
                else None
            )
            if logger is not None and (cached_frames is not None or disk_cache is not None):
                logger.log(
                    "cache_ready",
                    frame_count=len(cached_frames)
                    if cached_frames is not None else None,
                    cache_path=str(disk_cache) if disk_cache is not None else None,
                )
            if logger is not None:
                logger.log("phase_started", phase="usb_device_initialization")

            def play_stream(
                device: LibUSBTransport, deadline: float | None
            ) -> None:
                if logger is not None:
                    logger.log("phase_started", phase="playback")
                if cached_frames is not None:
                    display_animation(
                        device,
                        itertools.cycle(cached_frames),
                        max_fps=args.max_fps,
                        stop_at=deadline,
                    )
                elif disk_cache is not None:
                    display_animation(
                        device,
                        disk_cache_frames(disk_cache, args.file.name),
                        max_fps=args.max_fps,
                        stop_at=deadline,
                    )
                else:
                    display_animation(
                        device,
                        animation_frames(args.file, max_fps=args.max_fps),
                        max_fps=args.max_fps,
                        stop_at=deadline,
                    )

            restart_requested = _run_stream_session(
                play_stream,
                startup_attempts=args.startup_attempts,
                restart_after_hours=args.restart_after,
            )
            if restart_requested:
                if logger is not None:
                    logger.log(
                        "restart_requested",
                        reason="restart_after duration reached",
                    )
                print(
                    f"Streaming reached {args.restart_after:g} hours; "
                    "restarting the process.",
                    flush=True,
                )
                logger = None
                _restart_process()
            if logger is not None:
                logger.log("playback_finished")
                logger.log("command_completed")
            return 0
        else:
            if logger is not None:
                logger.log("phase_started", phase="text_render")
            frame = text_to_frame(
                args.text,
                color=args.color,
                background=args.background,
                font_size=args.font_size,
                font_path=args.font_path,
            )

        if logger is not None:
            logger.log("phase_started", phase="usb_device_initialization")

        def static_stream(device: LibUSBTransport, deadline: float | None) -> None:
            if logger is not None:
                logger.log("phase_started", phase="frame_stream")
            if args.once:
                display_frames(device, frame, frame_count=1)
            else:
                print(
                    f"Streaming at up to {args.fps:g} FPS; press Ctrl+C to stop.",
                    flush=True,
                )
                display_frames(device, frame, fps=args.fps, stop_at=deadline)

        restart_requested = _run_stream_session(
            static_stream,
            startup_attempts=args.startup_attempts,
            restart_after_hours=args.restart_after,
        )
        if restart_requested:
            if logger is not None:
                logger.log(
                    "restart_requested",
                    reason="restart_after duration reached",
                )
            print(
                f"Streaming reached {args.restart_after:g} hours; "
                "restarting the process.",
                flush=True,
            )
            logger = None
            _restart_process()
        if logger is not None:
            logger.log("command_completed")
    except KeyboardInterrupt:
        if logger is not None:
            logger.log("command_interrupted")
        print("\nStreaming stopped.")
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        if logger is not None:
            logger.log(
                "command_failed",
                error=repr(error),
                traceback=traceback.format_exc(),
            )
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        if logger is not None:
            logger.log(
                "unhandled_exception",
                error=repr(error),
                traceback=traceback.format_exc(),
            )
        raise
    finally:
        if logger is not None:
            logger.close()
            _RUN_LOGGER = None

    print("Display updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
