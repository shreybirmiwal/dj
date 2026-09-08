import hashlib
import time
from pathlib import Path

import numpy as np

from setmix.cue_audio import NativeCueEngine


class FakeOutputStream:
    writes = []
    settings = None

    def __init__(self, **settings):
        self.__class__.settings = settings

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def write(self, samples):
        self.__class__.writes.append(samples.copy())


class FakeSoundDevice:
    OutputStream = FakeOutputStream

    @staticmethod
    def query_devices():
        return [
            {"name": "MacBook Speakers", "max_output_channels": 2, "default_samplerate": 48000},
            {"name": "DDJ-FLX4", "max_output_channels": 4, "default_samplerate": 44100},
        ]


class FakeStdout:
    def __init__(self):
        self.chunks = [(np.ones((4, 2), dtype="<f4") * 0.5).tobytes(), b""]

    def read(self, _size):
        return self.chunks.pop(0)


class FakeProcess:
    def __init__(self):
        self.stdout = FakeStdout()
        self.terminated = False

    def terminate(self):
        self.terminated = True


def track_id(path: Path, root: Path) -> str:
    return hashlib.sha1(str(path.relative_to(root)).encode()).hexdigest()[:16]


def test_cue_engine_routes_only_to_usb_channels_three_and_four(tmp_path: Path):
    source = tmp_path / "preview.wav"
    source.write_bytes(b"not decoded by the fake process")
    process = FakeProcess()
    FakeOutputStream.writes = []
    engine = NativeCueEngine(
        tmp_path,
        sounddevice_module=FakeSoundDevice(),
        popen_factory=lambda *_args, **_kwargs: process,
    )

    result = engine.start(track_id(source, tmp_path), offset_seconds=12.5, level=0.4)
    assert result["ok"] is True
    for _ in range(100):
        if FakeOutputStream.writes:
            break
        time.sleep(0.005)

    routed = FakeOutputStream.writes[0]
    assert FakeOutputStream.settings["channels"] == 4
    assert np.all(routed[:, :2] == 0)
    assert np.allclose(routed[:, 2:], 0.2)


def test_cue_engine_requires_a_four_channel_flx4(tmp_path: Path):
    source = tmp_path / "preview.mp3"
    source.write_bytes(b"audio")
    backend = FakeSoundDevice()
    backend.query_devices = lambda: [
        {"name": "DDJ-FLX4", "max_output_channels": 2, "default_samplerate": 48000}
    ]
    engine = NativeCueEngine(tmp_path, sounddevice_module=backend)

    result = engine.start(track_id(source, tmp_path))
    assert result["ok"] is False
    assert "4-channel USB audio" in result["error"]
