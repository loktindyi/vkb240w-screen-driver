import ctypes
import itertools
import json
import os
import random
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
import usb.core

from vk_b240w import (
    CACHE_MAGIC,
    RunLogger,
    FRAME_PREFIX,
    FRAME_SIZE,
    FRAME_SUFFIX,
    HEIGHT,
    HID_FEATURE_REPORT_SIZE,
    MS912xControl,
    USB_BULK_OUT_PIPE,
    USB_TRANSFER_CHUNK_SIZES,
    LibUSBTransport,
    WIDTH,
    _default_disk_cache_path,
    animation_frames,
    cache_animation_in_memory,
    cache_animation_to_disk,
    disk_cache_frames,
    display_animation,
    display_frames,
    image_to_frame,
    main,
    rgb_to_uyvy,
    send_frame,
    test_pattern_to_frame,
    text_to_frame,
    _launch_hidden_python,
    _open_transport_with_retries,
    _restart_process,
    _run_stream_session,
)


class RecordingWriter:
    def __init__(self) -> None:
        self.frames: list[tuple[bytes, ...]] = []

    def write_frame(self, chunks: tuple[bytes, ...]) -> None:
        self.frames.append(chunks)


class FakeAsyncLibrary:
    def __init__(self, fail_reap: bool = False) -> None:
        self.next_context = 1
        self.fail_reap = fail_reap
        self.submitted: dict[int, int] = {}
        self.events: list[tuple[str, int, int]] = []

    def usb_bulk_setup_async(self, handle, context_pointer, endpoint) -> int:
        context_pointer._obj.value = self.next_context
        self.next_context += 1
        self.events.append(("setup", context_pointer._obj.value, endpoint))
        return 0

    def usb_submit_async(self, context, data, size) -> int:
        self.submitted[context.value] = size
        self.events.append(("submit", context.value, size))
        return 0

    def usb_reap_async(self, context, timeout) -> int:
        self.events.append(("reap", context.value, timeout))
        if self.fail_reap:
            return -116
        return self.submitted[context.value]

    def usb_cancel_async(self, context) -> int:
        self.events.append(("cancel", context.value, 0))
        return 0

    def usb_reap_async_nocancel(self, context, timeout) -> int:
        self.events.append(("reap_cancelled", context.value, timeout))
        return 0

    def usb_free_async(self, context_pointer) -> int:
        self.events.append(("free", context_pointer._obj.value, 0))
        context_pointer._obj.value = None
        return 0

    def usb_strerror(self) -> bytes:
        return b"simulated transfer timeout"


def fake_transport(library: FakeAsyncLibrary) -> LibUSBTransport:
    class RecordingControl:
        def __init__(self) -> None:
            self.frame_ready_calls = 0

        def frame_ready(self) -> None:
            self.frame_ready_calls += 1

    transport = LibUSBTransport.__new__(LibUSBTransport)
    transport._device = SimpleNamespace(
        _ctx=SimpleNamespace(handle=ctypes.c_void_p(123)),
        writes=[],
    )

    def write(endpoint, data, timeout):
        transport._device.writes.append((endpoint, data, timeout))
        library.events.append(("write", endpoint, len(data)))
        return 0

    transport._device.write = write
    transport._usb_core = usb.core
    transport._usb_library = library
    transport._control = RecordingControl()
    transport._frame_buffers = None
    return transport


class FakeHIDDevice:
    def __init__(self) -> None:
        self.features: list[bytes] = []
        self.address = 0
        self.registers = {
            0xF000: 0xB7,
            0xF001: 0x16,
            0xF002: 0x0A,
            0x0030: 2,
            0x0031: 5,
            0x0032: 1,
        }

    def send_feature_report(self, report: bytes) -> int:
        self.features.append(report)
        payload = report[1:]
        if payload[0] == 0xB5:
            self.address = (payload[1] << 8) | payload[2]
        elif payload[0] == 0xB6:
            address = (payload[1] << 8) | payload[2]
            self.registers[address] = payload[3]
        return len(report)

    def get_feature_report(self, report_id: int, size: int) -> list[int]:
        self.assert_report_shape(report_id, size)
        response = bytearray(8)
        response[0] = 0xB5
        response[1] = self.address >> 8
        response[2] = self.address & 0xFF
        for offset in range(4):
            response[3 + offset] = self.registers.get(
                self.address + offset, 0
            )
        return [0, *response]

    @staticmethod
    def assert_report_shape(report_id: int, size: int) -> None:
        if report_id != 0 or size != HID_FEATURE_REPORT_SIZE:
            raise AssertionError("unexpected HID Feature Report request")

    def close(self) -> None:
        pass


class FrameTests(unittest.TestCase):
    def test_hidden_launcher_uses_pythonw_and_hidden_startup_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            python = Path(directory) / "python.exe"
            pythonw = Path(directory) / "pythonw.exe"
            python.touch()
            pythonw.touch()
            arguments = ["vk_b240w.py", "test-pattern", "--hide"]
            with (
                patch("vk_b240w.sys.executable", str(python)),
                patch("vk_b240w.sys.argv", arguments),
                patch("vk_b240w.subprocess.Popen") as popen,
            ):
                _launch_hidden_python()

        popen.assert_called_once()
        command = popen.call_args.args[0]
        options = popen.call_args.kwargs
        self.assertEqual(command, [str(pythonw), *arguments])
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["stdout"], subprocess.DEVNULL)
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        self.assertEqual(options["creationflags"], subprocess.CREATE_NO_WINDOW)
        self.assertEqual(options["startupinfo"].wShowWindow, subprocess.SW_HIDE)

    def test_process_restart_reexecutes_python_with_original_arguments(self) -> None:
        arguments = [
            "vk_b240w.py",
            "play",
            "sample.mp4",
            "--cache-disk",
            "--hide",
        ]
        with (
            patch("vk_b240w.sys.executable", "python.exe"),
            patch("vk_b240w.sys.argv", arguments),
            patch("vk_b240w.os.execv") as execv,
        ):
            with self.assertRaisesRegex(OSError, "unexpectedly returned"):
                _restart_process()

        execv.assert_called_once_with("python.exe", ["python.exe", *arguments])

    def test_startup_retries_ms912x_completion_timeout(self) -> None:
        expected_error = OSError(
            "MS912x command transfer_mode did not complete; "
            "status register 0xC558 remained nonzero."
        )

        class FakeTransport:
            pass

        transport = FakeTransport()
        with (
            patch(
                "vk_b240w.LibUSBTransport",
                side_effect=[expected_error, expected_error, transport],
            ) as factory,
            patch("vk_b240w.time.sleep") as sleep,
            patch("builtins.print"),
        ):
            result = _open_transport_with_retries(3)

        self.assertIs(result, transport)
        self.assertEqual(factory.call_count, 3)
        self.assertEqual([item.args for item in sleep.call_args_list], [(1,), (1,)])

    def test_startup_does_not_retry_permanent_initialization_errors(self) -> None:
        with patch(
            "vk_b240w.LibUSBTransport",
            side_effect=OSError("USB device was not found"),
        ) as factory, patch("vk_b240w.time.sleep") as sleep:
            with self.assertRaisesRegex(OSError, "was not found"):
                _open_transport_with_retries(3)

        factory.assert_called_once()
        sleep.assert_not_called()

    def test_stream_session_deadline_requests_process_restart(self) -> None:
        class FakeTransport:
            closed = False

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                self.closed = True

        device = FakeTransport()
        with (
            patch("vk_b240w.LibUSBTransport", return_value=device),
            patch("vk_b240w.time.monotonic", side_effect=(100.0, 3700.0)),
        ):
            restart = _run_stream_session(
                lambda active_device, deadline: self.assertEqual(
                    deadline, 3700.0
                ),
                startup_attempts=3,
                restart_after_hours=1,
            )

        self.assertTrue(restart)
        self.assertTrue(device.closed)

    def test_zero_restart_interval_disables_restart_deadline(self) -> None:
        class FakeTransport:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return None

        with patch("vk_b240w.LibUSBTransport", return_value=FakeTransport()):
            restart = _run_stream_session(
                lambda active_device, deadline: self.assertIsNone(deadline),
                startup_attempts=1,
                restart_after_hours=0,
            )

        self.assertFalse(restart)

    def test_cli_creates_normal_log_by_default(self) -> None:
        class FakeTransport:
            def __enter__(self):
                return RecordingWriter()

            def __exit__(self, exc_type, exc_value, traceback):
                return None

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"LOCALAPPDATA": directory}
        ), patch("vk_b240w.LibUSBTransport", return_value=FakeTransport()):
            with patch("builtins.print"):
                result = main(["test-pattern", "--once"])
            log_files = list((Path(directory) / "VKCache" / "logs").glob("*.jsonl"))
            self.assertEqual(result, 0)
            self.assertEqual(len(log_files), 1)
            records = [
                json.loads(line)
                for line in log_files[0].read_text(encoding="utf-8").splitlines()
            ]

        self.assertIn("command_started", [record["event"] for record in records])
        self.assertIn("command_completed", [record["event"] for record in records])
        self.assertIn("usb_device_ready", [record["event"] for record in records])
        command_started = next(
            record for record in records if record["event"] == "command_started"
        )
        self.assertTrue(command_started["once"])
        self.assertEqual(command_started["fps"], 5)
        self.assertNotIn("memory_sample", [record["event"] for record in records])

    def test_no_log_option_disables_file_creation(self) -> None:
        class FakeTransport:
            def __enter__(self):
                return RecordingWriter()

            def __exit__(self, exc_type, exc_value, traceback):
                return None

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"LOCALAPPDATA": directory}
        ), patch("vk_b240w.LibUSBTransport", return_value=FakeTransport()):
            with patch("builtins.print"):
                result = main(["test-pattern", "--once", "--no-log"])

        self.assertEqual(result, 0)
        self.assertFalse((Path(directory) / "VKCache" / "logs").exists())

    def test_run_logger_writes_timestamped_lifecycle_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "run.jsonl"
            logger = RunLogger(log_path)
            logger.log("startup_retry", attempt=2, error="status timeout")
            logger.close()

            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertFalse(log_path.with_suffix(".crash.log").exists())

        events = [record["event"] for record in records]
        self.assertEqual(events, ["process_started", "startup_retry", "process_stopped"])
        self.assertIn("timestamp", records[0])
        self.assertEqual(records[1]["attempt"], 2)

    def test_default_disk_cache_path_uses_video_name_and_fps(self) -> None:
        with patch.dict("os.environ", {"LOCALAPPDATA": r"C:\Users\rin\AppData\Local"}):
            cache_path = _default_disk_cache_path(Path("sample video.mp4"), 25)

        self.assertEqual(
            cache_path,
            Path(r"C:\Users\rin\AppData\Local\VKCache\sample video_25fps.vkcache"),
        )

    def test_disk_cache_is_reused_by_filename_and_can_be_flushed(self) -> None:
        image = Image.new("RGB", (WIDTH, HEIGHT), "purple")
        source_frames = ((image, 0.2), (image, 0.4))
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "current.vkcache"
            source = Path(directory) / "clip.mp4"
            with patch(
                "vk_b240w.animation_frames",
                side_effect=lambda *args, **kwargs: iter(source_frames),
            ) as decode:
                cache_animation_to_disk(source, cache_path=cache_path)
                decode.assert_called_once()

                cached = list(
                    itertools.islice(disk_cache_frames(cache_path, "clip.mp4"), 3)
                )
                self.assertEqual(
                    [duration for _, duration in cached],
                    [0.2, 0.4, 0.2],
                )

                cache_animation_to_disk(
                    source.with_name("CLIP.MP4"), cache_path=cache_path
                )
                self.assertEqual(decode.call_count, 1)

                cache_animation_to_disk(
                    source, cache_path=cache_path, flush=True
                )
                self.assertEqual(decode.call_count, 2)

            with self.assertRaisesRegex(OSError, "not 'other.mp4'"):
                next(disk_cache_frames(cache_path, "other.mp4"))

    def test_disk_cache_rebuilds_when_source_filename_changes(self) -> None:
        image = Image.new("RGB", (WIDTH, HEIGHT), "cyan")
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "current.vkcache"
            with patch(
                "vk_b240w.animation_frames",
                side_effect=lambda *args, **kwargs: iter(((image, 0.1),)),
            ) as decode:
                cache_animation_to_disk(
                    Path("first.mp4"), cache_path=cache_path
                )
                cache_animation_to_disk(
                    Path("second.mp4"), cache_path=cache_path
                )

            self.assertEqual(decode.call_count, 2)
            self.assertEqual(
                list(itertools.islice(disk_cache_frames(cache_path, "second.mp4"), 1))[0][1],
                0.1,
            )

    def test_disk_cache_rebuilds_when_cached_frame_metadata_is_invalid(self) -> None:
        image = Image.new("RGB", (WIDTH, HEIGHT), "orange")
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "current.vkcache"
            with patch(
                "vk_b240w.animation_frames",
                side_effect=lambda *args, **kwargs: iter(((image, 0.1),)),
            ) as decode:
                cache_animation_to_disk(Path("clip.mp4"), cache_path=cache_path)
                with cache_path.open("r+b") as cache:
                    cache.seek(len(CACHE_MAGIC) + 2 + len("clip.mp4"))
                    cache.write(b"\x00" * 8)

                cache_animation_to_disk(Path("clip.mp4"), cache_path=cache_path)

            self.assertEqual(decode.call_count, 2)
            self.assertEqual(
                list(itertools.islice(disk_cache_frames(cache_path, "clip.mp4"), 1))[0][1],
                0.1,
            )
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_memory_cache_stores_encoded_frames_for_one_pass(self) -> None:
        image = Image.new("RGB", (WIDTH, HEIGHT), "blue")
        decoded_frames = iter(((image, 0.2), (image, 0.4)))

        with (
            patch("vk_b240w.animation_frames", return_value=decoded_frames) as decode,
            patch("vk_b240w.image_to_frame", side_effect=(b"frame-1", b"frame-2")),
            patch("builtins.print") as output,
        ):
            cached = cache_animation_in_memory(
                Path("sample.mp4"), max_fps=10
            )

        decode.assert_called_once()
        self.assertEqual(decode.call_args.args, (Path("sample.mp4"),))
        self.assertEqual(decode.call_args.kwargs["max_fps"], 10)
        self.assertFalse(decode.call_args.kwargs["loop"])
        self.assertTrue(callable(decode.call_args.kwargs["on_video_metadata"]))
        self.assertEqual(cached, [(b"frame-1", 0.2), (b"frame-2", 0.4)])
        self.assertTrue(
            any("Memory cache ready: 2 frames" in call.args[0] for call in output.call_args_list)
        )

    def test_memory_cache_rejects_empty_animation(self) -> None:
        with patch("vk_b240w.animation_frames", return_value=iter(())):
            with self.assertRaisesRegex(OSError, "contains no frames"):
                cache_animation_in_memory(Path("empty.mp4"))

    def test_display_animation_reuses_cached_encoded_frames(self) -> None:
        writer = RecordingWriter()
        frame = text_to_frame("cached frame")
        frames = itertools.cycle(((frame, 0.5),))

        display_animation(writer, frames, frame_count=3)

        self.assertEqual(len(writer.frames), 3)
        self.assertTrue(all(b"".join(chunks) == frame for chunks in writer.frames))

    def test_gif_frames_repeat_and_preserve_frame_delays(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "loop.gif"
            first = Image.new("RGB", (2, 2), "red")
            second = Image.new("RGB", (2, 2), "blue")
            first.save(
                path,
                save_all=True,
                append_images=(second,),
                duration=(200, 400),
                loop=0,
            )

            frames = list(itertools.islice(animation_frames(path), 3))

        self.assertEqual([duration for _, duration in frames], [0.2, 0.4, 0.2])
        self.assertEqual(frames[0][0].getpixel((0, 0)), (255, 0, 0))
        self.assertEqual(frames[1][0].getpixel((0, 0)), (0, 0, 255))
        self.assertEqual(frames[2][0].getpixel((0, 0)), (255, 0, 0))

    def test_gif_max_fps_drops_frames_without_shortening_animation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fast.gif"
            images = [
                Image.new("RGB", (2, 2), (index * 20, 0, 0))
                for index in range(10)
            ]
            images[0].save(
                path,
                save_all=True,
                append_images=images[1:],
                duration=100,
                loop=0,
            )
            frames = list(
                itertools.islice(
                    animation_frames(path, max_fps=5, loop=False), 10
                )
            )

        self.assertEqual(len(frames), 5)
        self.assertEqual(sum(duration for _, duration in frames), 1.0)

    def test_video_frames_are_decoded_and_reopened_for_looping(self) -> None:
        readers_opened = 0
        output_filters: list[str] = []

        def read_frames(path: str, pix_fmt: str, output_params: list[str]):
            nonlocal readers_opened
            readers_opened += 1
            self.assertEqual(path, "sample.mp4")
            self.assertEqual(pix_fmt, "rgb24")
            output_filters.append(output_params[1])
            yield {"size": (WIDTH, HEIGHT), "fps": 30}
            yield bytes(WIDTH * HEIGHT * 3)

        fake_ffmpeg = SimpleNamespace(read_frames=read_frames)
        with patch.dict(sys.modules, {"imageio_ffmpeg": fake_ffmpeg}):
            frames = list(
                itertools.islice(
                    animation_frames(Path("sample.mp4"), max_fps=10), 2
                )
            )

        self.assertEqual(readers_opened, 2)
        self.assertEqual([duration for _, duration in frames], [0.1, 0.1])
        self.assertEqual(frames[0][0].size, (WIDTH, HEIGHT))
        self.assertTrue(all("min(source_fps\\,10)" in value for value in output_filters))
        self.assertTrue(all("scale=320:240" in value for value in output_filters))

    def test_video_decoder_scales_without_applying_a_default_fps_cap(self) -> None:
        output_filters: list[str] = []

        def read_frames(path: str, pix_fmt: str, output_params: list[str]):
            output_filters.append(output_params[1])
            yield {"size": (WIDTH, HEIGHT), "fps": 24}
            yield bytes(WIDTH * HEIGHT * 3)

        fake_ffmpeg = SimpleNamespace(read_frames=read_frames)
        with patch.dict(sys.modules, {"imageio_ffmpeg": fake_ffmpeg}):
            next(animation_frames(Path("sample.mp4")))

        self.assertIn("scale=320:240", output_filters[0])
        self.assertNotIn("fps=", output_filters[0])

    def test_video_reader_closes_when_metadata_processing_fails(self) -> None:
        class FakeReader:
            def __init__(self, values) -> None:
                self._values = iter(values)
                self.closed = False

            def __iter__(self):
                return self

            def __next__(self):
                return next(self._values)

            def close(self) -> None:
                self.closed = True

        def fail_metadata_callback(metadata, effective_fps) -> None:
            raise RuntimeError("metadata callback failed")

        cases = (
            ([{}], None, OSError),
            (
                [{"size": (WIDTH, HEIGHT), "fps": 24}],
                fail_metadata_callback,
                RuntimeError,
            ),
        )
        for values, callback, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                reader = FakeReader(values)
                fake_ffmpeg = SimpleNamespace(read_frames=lambda *args, **kwargs: reader)
                with patch.dict(sys.modules, {"imageio_ffmpeg": fake_ffmpeg}):
                    with self.assertRaises(expected_error):
                        list(
                            animation_frames(
                                Path("sample.mp4"),
                                on_video_metadata=callback,
                            )
                        )
                self.assertTrue(reader.closed)

    def test_display_animation_sends_requested_number_of_frames(self) -> None:
        writer = RecordingWriter()
        image = Image.new("RGB", (WIDTH, HEIGHT), "green")
        frames = iter(((image, 0.5), (image, 0.5), (image, 0.5)))

        with patch("vk_b240w.time.sleep"):
            display_animation(writer, frames, max_fps=5, frame_count=2)

        self.assertEqual(len(writer.frames), 2)

    def test_display_animation_drops_frames_above_output_rate(self) -> None:
        image = Image.new("RGB", (1, 1), "green")
        frames = iter(((image, 0.05),) * 4)

        with (
            patch("vk_b240w.time.monotonic", return_value=0.0),
            patch("vk_b240w.image_to_frame", return_value=b"frame"),
            patch("vk_b240w.send_frame") as send_frame,
        ):
            display_animation(
                RecordingWriter(), frames, max_fps=5, frame_count=4
            )

        send_frame.assert_called_once()

    def test_uyvy_numpy_encoder_matches_reference_for_random_pixels(self) -> None:
        random_source = random.Random(1234)
        width, height = 8, 5
        rgb = bytes(
            random_source.randrange(256)
            for _ in range(width * height * 3)
        )
        expected = bytearray()
        for y in range(height):
            for x in range(0, width, 2):
                index0 = (y * width + x) * 3
                index1 = index0 + 3
                r0, g0, b0 = rgb[index0 : index0 + 3]
                r1, g1, b1 = rgb[index1 : index1 + 3]
                y0 = ((33 * r0 + 65 * g0 + 13 * b0) >> 7) + 16
                y1 = ((33 * r1 + 65 * g1 + 13 * b1) >> 7) + 16
                if y + 1 < height:
                    next0 = ((y + 1) * width + x) * 3
                    next1 = next0 + 3
                    r = (r0 + r1 + rgb[next0] + rgb[next1]) // 4
                    g = (g0 + g1 + rgb[next0 + 1] + rgb[next1 + 1]) // 4
                    b = (b0 + b1 + rgb[next0 + 2] + rgb[next1 + 2]) // 4
                else:
                    r, g, b = (
                        (r0 + r1) // 2,
                        (g0 + g1) // 2,
                        (b0 + b1) // 2,
                    )
                u = ((112 * b - 74 * g - 38 * r - 32640) >> 8) & 0xFF
                v = ((112 * r - 94 * g - 18 * b - 32640) >> 8) & 0xFF
                expected.extend((u, y0 & 0xFF, v, y1 & 0xFF))

        self.assertEqual(rgb_to_uyvy(rgb, width, height), bytes(expected))

    def test_play_command_accepts_optional_max_fps(self) -> None:
        from vk_b240w import _build_parser

        parser = _build_parser()
        self.assertIsNone(parser.parse_args(["play", "clip.mp4"]).max_fps)
        self.assertEqual(
            parser.parse_args(["play", "clip.mp4", "--max-fps", "12"]).max_fps,
            12,
        )

    def test_image_frame_has_capture_envelope_and_uyvy_body(self) -> None:
        image = Image.new("RGB", (WIDTH, HEIGHT), "red")

        frame = image_to_frame(image)

        self.assertEqual(len(frame), FRAME_SIZE)
        self.assertEqual(frame[: len(FRAME_PREFIX)], FRAME_PREFIX)
        self.assertEqual(frame[-len(FRAME_SUFFIX) :], FRAME_SUFFIX)
        self.assertEqual(
            frame[len(FRAME_PREFIX) : len(FRAME_PREFIX) + 4],
            bytes((90, 81, 240, 81)),
        )

    def test_uyvy_chroma_uses_the_captured_controllers_2x2_sampling(self) -> None:
        image = Image.new("RGB", (2, 2), "red")
        image.putpixel((0, 1), (0, 0, 255))
        image.putpixel((1, 1), (0, 0, 255))

        self.assertEqual(
            rgb_to_uyvy(image.tobytes(), 2, 2)[:4],
            bytes((165, 81, 175, 81)),
        )

    def test_images_are_fitted_inside_the_screen(self) -> None:
        frame = image_to_frame(Image.new("RGBA", (640, 480), (0, 0, 0, 0)))
        body = frame[len(FRAME_PREFIX) : -len(FRAME_SUFFIX)]

        self.assertEqual(len(body), WIDTH * HEIGHT * 2)
        self.assertEqual(set(body), {16, 128})

    def test_text_renders_to_a_complete_frame(self) -> None:
        frame = text_to_frame("VK B240W")

        self.assertEqual(len(frame), FRAME_SIZE)
        self.assertTrue(frame.startswith(FRAME_PREFIX))
        self.assertTrue(frame.endswith(FRAME_SUFFIX))
        self.assertNotEqual(set(frame[len(FRAME_PREFIX) : -len(FRAME_SUFFIX)]), {16, 128})

    def test_calibration_pattern_has_complete_envelope_and_eight_bars(self) -> None:
        frame = test_pattern_to_frame()

        self.assertEqual(len(frame), FRAME_SIZE)
        body = frame[len(FRAME_PREFIX) : -len(FRAME_SUFFIX)]
        self.assertEqual(len(body), WIDTH * HEIGHT * 2)
        expected_y = (237, 211, 171, 145, 107, 81, 41, 16)
        for bar, y_value in enumerate(expected_y):
            x = WIDTH // 8 * bar + 4
            offset = 4 * WIDTH * 2 + x * 2 + 1
            self.assertEqual(body[offset], y_value)

    def test_sends_capture_chunk_sizes_as_a_single_frame(self) -> None:
        writer = RecordingWriter()
        send_frame(writer, text_to_frame("test"))

        self.assertEqual(len(writer.frames), 1)
        self.assertEqual(
            tuple(map(len, writer.frames[0])), USB_TRANSFER_CHUNK_SIZES
        )
        self.assertEqual(
            b"".join(writer.frames[0])[-len(FRAME_SUFFIX) :], FRAME_SUFFIX
        )

    def test_rejects_invalid_frame_before_writing(self) -> None:
        writer = RecordingWriter()
        with self.assertRaises(ValueError):
            send_frame(writer, b"not a display frame")
        self.assertEqual(writer.frames, [])

    def test_submits_all_frame_chunks_before_waiting_for_completion(self) -> None:
        library = FakeAsyncLibrary()
        transport = fake_transport(library)
        chunks = tuple(bytes((index,)) * size for index, size in enumerate(USB_TRANSFER_CHUNK_SIZES))

        transport.write_frame(chunks)

        events = library.events
        self.assertEqual(
            [event[2] for event in events if event[0] == "setup"],
            [USB_BULK_OUT_PIPE] * 3,
        )
        submission_positions = [
            index for index, event in enumerate(events) if event[0] == "submit"
        ]
        completion_positions = [
            index for index, event in enumerate(events) if event[0] == "reap"
        ]
        self.assertEqual(len(submission_positions), 3)
        self.assertEqual(len(completion_positions), 3)
        self.assertLess(max(submission_positions), min(completion_positions))
        self.assertEqual(
            transport._device.writes,
            [(USB_BULK_OUT_PIPE, b"", 5_000)],
        )
        self.assertGreater(
            next(index for index, event in enumerate(events) if event[0] == "write"),
            max(completion_positions),
        )

    def test_reuses_usb_transfer_buffers_across_frames(self) -> None:
        transport = fake_transport(FakeAsyncLibrary())
        chunks = tuple(
            bytes((index,)) * size
            for index, size in enumerate(USB_TRANSFER_CHUNK_SIZES)
        )
        next_chunks = tuple(
            bytes((index + 3,)) * size
            for index, size in enumerate(USB_TRANSFER_CHUNK_SIZES)
        )

        transport.write_frame(chunks)
        buffers = transport._frame_buffers
        transport.write_frame(next_chunks)

        self.assertIs(transport._frame_buffers, buffers)
        self.assertEqual(
            tuple(bytes(buffer) for buffer in buffers),
            next_chunks,
        )

    def test_cancels_and_reaps_remaining_chunks_on_timeout(self) -> None:
        library = FakeAsyncLibrary(fail_reap=True)
        transport = fake_transport(library)
        chunks = tuple(
            bytes((index,)) * size
            for index, size in enumerate(USB_TRANSFER_CHUNK_SIZES)
        )

        with self.assertRaisesRegex(OSError, "USB bulk frame transfer failed"):
            transport.write_frame(chunks)

        events = library.events
        first_reap = next(i for i, event in enumerate(events) if event[0] == "reap")
        self.assertEqual(
            sum(event[0] == "submit" for event in events[:first_reap]),
            3,
        )
        self.assertFalse(transport._device.writes)
        self.assertEqual(sum(event[0] == "cancel" for event in events), 3)
        self.assertEqual(sum(event[0] == "free" for event in events), 3)

    def test_initializes_ms912c_control_plane_before_video_stream(self) -> None:
        hid_device = FakeHIDDevice()
        control = MS912xControl(hid_device)

        control.initialize_stream()

        self.assertEqual(control.video_port, 5)
        self.assertEqual(control.sdram_type, 2)
        commands = [
            report[1:9]
            for report in hid_device.features
            if report[1] == 0xA6
        ]
        self.assertEqual(
            commands,
            [
                bytes((0xA6, 0x03, 3, 0, 0, 0, 0, 0)),
                bytes((0xA6, 0x07, 1, 0, 0, 0, 0, 0)),
                bytes((0xA6, 0x05, 0, 0, 0, 0, 0, 0)),
                bytes((0xA6, 0x01, 7, 128, 4, 56, 0x22, 0)),
                bytes((0xA6, 0x02, 0x81, 0, 7, 128, 4, 56)),
                bytes((0xA6, 0x01, 1, 64, 0, 240, 0x22, 0)),
                bytes((0xA6, 0x02, 0x94, 0, 1, 64, 0, 240)),
                bytes((0xA6, 0x04, 1, 0, 0, 0, 0, 0)),
            ],
        )

    def test_frame_ready_triggers_frame_then_enables_port_five_output(self) -> None:
        hid_device = FakeHIDDevice()
        control = MS912xControl(hid_device)
        control.video_port = 5

        control.frame_ready()
        control.frame_ready()

        commands = [
            report[1:9]
            for report in hid_device.features
            if report[1] == 0xA6
        ]
        self.assertEqual(commands[0], bytes((0xA6, 0, 1, 100, 0, 0, 0, 0)))
        self.assertEqual(commands[1], bytes((0xA6, 5, 1, 0, 0, 0, 0, 0)))
        self.assertEqual(commands[-1], bytes((0xA6, 0, 0, 100, 0, 0, 0, 0)))
        self.assertEqual(len(commands), 3)
        self.assertTrue(control._output_enabled)

    def test_display_frames_repeats_at_requested_rate_and_can_send_once(self) -> None:
        writer = RecordingWriter()
        frame = text_to_frame("persistent")

        with patch("vk_b240w.time.sleep"):
            display_frames(writer, frame, fps=10, frame_count=3)

        self.assertEqual(len(writer.frames), 3)
        self.assertIs(writer.frames[0], writer.frames[1])
        self.assertIs(writer.frames[1], writer.frames[2])
        with self.assertRaises(ValueError):
            display_frames(writer, frame, fps=0, frame_count=1)


class CacheMaintenanceTests(unittest.TestCase):
    def test_clear_cache_removes_only_vkcache_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_directory = Path(temp_dir) / "VKCache"
            logs_directory = cache_directory / "logs"
            logs_directory.mkdir(parents=True)
            (cache_directory / "video_5fps.vkcache").write_bytes(b"cache")
            (cache_directory / "keep.txt").write_text("keep", encoding="utf-8")
            log_file = logs_directory / "run.jsonl"
            log_file.write_text("log", encoding="utf-8")

            with patch.dict(os.environ, {"LOCALAPPDATA": temp_dir}):
                self.assertEqual(main(["--clear-cache"]), 0)

            self.assertFalse((cache_directory / "video_5fps.vkcache").exists())
            self.assertTrue((cache_directory / "keep.txt").is_file())
            self.assertTrue(log_file.is_file())

    def test_uninstall_removes_vkcache_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_directory = Path(temp_dir) / "VKCache"
            cache_directory.mkdir()
            (cache_directory / "keep.txt").write_text("remove", encoding="utf-8")

            with patch.dict(os.environ, {"LOCALAPPDATA": temp_dir}):
                self.assertEqual(main(["--uninstall"]), 0)

            self.assertFalse(cache_directory.exists())

    def test_cache_maintenance_options_must_be_used_alone(self) -> None:
        with self.assertRaises(SystemExit) as error:
            main(["--clear-cache", "test-pattern"])

        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
