"""Native, pre-fader headphone cue output for DJ audio interfaces."""

from __future__ import annotations

import hashlib
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

import numpy as np


AUDIO_SUFFIXES = {".aif", ".aiff", ".flac", ".m4a", ".mp3", ".wav"}


class NativeCueEngine:
    """Decode one deck and route it only to USB output channels 3/4."""

    def __init__(
        self,
        music_dir: Path,
        *,
        sounddevice_module: Any | None = None,
        popen_factory: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self.music_dir = music_dir.resolve()
        self._sounddevice = sounddevice_module
        self._popen_factory = popen_factory
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: Any | None = None
        self._generation = 0
        self._level = 0.7
        self._active_track_id: str | None = None
        self._active_device: str | None = None
        self._tracks = self._index_tracks()

    def _index_tracks(self) -> dict[str, Path]:
        tracks: dict[str, Path] = {}
        for path in self.music_dir.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in AUDIO_SUFFIXES:
                continue
            relative = path.relative_to(self.music_dir)
            track_id = hashlib.sha1(str(relative).encode()).hexdigest()[:16]
            tracks[track_id] = path.resolve()
        return tracks

    def _audio_backend(self) -> Any:
        if self._sounddevice is None:
            try:
                import sounddevice
            except ImportError as error:
                raise RuntimeError(
                    "Native cue support is not installed; run pip install -r requirements-desktop.txt"
                ) from error
            self._sounddevice = sounddevice
        return self._sounddevice

    def list_outputs(self) -> list[dict[str, Any]]:
        backend = self._audio_backend()
        devices = backend.query_devices()
        default_output: int | None = None
        try:
            configured = backend.default.device
            try:
                # sounddevice uses a sequence-like DeviceList on macOS rather
                # than a literal list/tuple for the input/output pair.
                default_output = int(configured[1])
            except (IndexError, TypeError):
                default_output = int(configured)
        except (AttributeError, IndexError, TypeError, ValueError):
            pass
        outputs = []
        for index, device in enumerate(devices):
            channels = int(device.get("max_output_channels", 0))
            if channels <= 0:
                continue
            outputs.append(
                {
                    "index": index,
                    "name": str(device.get("name", f"Audio output {index}")),
                    "outputChannels": channels,
                    "sampleRate": int(float(device.get("default_samplerate", 48000))),
                    "separateCue": channels >= 4,
                    "isDefault": index == default_output,
                }
            )
        return outputs

    def _cue_device(self, preferred_name: str | None = None) -> dict[str, Any]:
        outputs = self.list_outputs()
        candidates = [device for device in outputs if device["separateCue"]]
        if preferred_name:
            preferred = [device for device in candidates if preferred_name.casefold() in device["name"].casefold()]
            if preferred:
                return preferred[0]
        flx4 = [device for device in candidates if "flx4" in device["name"].casefold()]
        if flx4:
            return flx4[0]
        raise RuntimeError("DDJ-FLX4 4-channel USB audio is not available")

    def status(self) -> dict[str, Any]:
        try:
            outputs = self.list_outputs()
            cue_outputs = [device for device in outputs if device["separateCue"]]
            flx4 = next((device for device in cue_outputs if "flx4" in device["name"].casefold()), None)
            return {
                "ok": True,
                "available": flx4 is not None,
                "active": self._active_track_id is not None,
                "trackId": self._active_track_id,
                "device": self._active_device or (flx4["name"] if flx4 else None),
                "defaultOutput": next((device["name"] for device in outputs if device["isDefault"]), None),
                "masterRouted": bool(flx4 and flx4["isDefault"]),
                "level": self._level,
                "outputs": outputs,
            }
        except Exception as error:
            return {"ok": False, "available": False, "active": False, "error": str(error), "outputs": []}

    def start(
        self,
        track_id: str,
        *,
        offset_seconds: float = 0,
        level: float | None = None,
        preferred_device: str | None = None,
    ) -> dict[str, Any]:
        path = self._tracks.get(str(track_id))
        if path is None:
            return {"ok": False, "error": "Cue track is not in the active music library"}
        try:
            device = self._cue_device(preferred_device)
        except Exception as error:
            return {"ok": False, "error": str(error)}

        self.stop()
        with self._lock:
            if level is not None:
                self._level = max(0.0, min(1.0, float(level)))
            self._generation += 1
            generation = self._generation
            self._stop_event = threading.Event()
            self._active_track_id = str(track_id)
            self._active_device = device["name"]
            self._thread = threading.Thread(
                target=self._play,
                args=(generation, path, max(0.0, float(offset_seconds)), device, self._stop_event),
                name="setmix-headphone-cue",
                daemon=True,
            )
            self._thread.start()
        return {
            "ok": True,
            "active": True,
            "trackId": str(track_id),
            "device": device["name"],
            "channels": "3/4",
            "level": self._level,
        }

    def set_level(self, level: float) -> dict[str, Any]:
        with self._lock:
            self._level = max(0.0, min(1.0, float(level)))
        return {"ok": True, "level": self._level}

    def test_route(
        self,
        route: str,
        *,
        duration: float = 0.8,
        level: float = 0.06,
    ) -> dict[str, Any]:
        """Play a short, quiet diagnostic tone on MASTER or PHONES only."""
        if route not in {"master", "phones"}:
            return {"ok": False, "error": "Audio test route must be master or phones"}
        try:
            device = self._cue_device()
            self.stop()
            sample_rate = int(device["sampleRate"] or 48000)
            duration = max(0.25, min(2.0, float(duration)))
            level = max(0.0, min(0.12, float(level)))
            frames = max(1, round(sample_rate * duration))
            frequency = 440.0 if route == "master" else 660.0
            timeline = np.arange(frames, dtype=np.float32) / sample_rate
            tone = np.sin(2.0 * np.pi * frequency * timeline).astype(np.float32)
            fade_frames = min(frames // 2, max(1, round(sample_rate * 0.015)))
            envelope = np.ones(frames, dtype=np.float32)
            envelope[:fade_frames] = np.linspace(0.0, 1.0, fade_frames, dtype=np.float32)
            envelope[-fade_frames:] = np.linspace(1.0, 0.0, fade_frames, dtype=np.float32)
            stereo = np.column_stack((tone, tone)) * envelope[:, None] * level
            routed = np.zeros((frames, 4), dtype=np.float32)
            channel_slice = slice(0, 2) if route == "master" else slice(2, 4)
            routed[:, channel_slice] = stereo
            backend = self._audio_backend()
            with backend.OutputStream(
                samplerate=sample_rate,
                device=device["index"],
                channels=4,
                dtype="float32",
                blocksize=1024,
                latency="low",
            ) as stream:
                stream.write(routed)
            return {
                "ok": True,
                "route": route,
                "device": device["name"],
                "channels": "1/2" if route == "master" else "3/4",
                "duration": duration,
                "level": level,
            }
        except Exception as error:
            return {"ok": False, "error": str(error), "route": route}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._generation += 1
            self._stop_event.set()
            process = self._process
            thread = self._thread
            self._process = None
            self._thread = None
            self._active_track_id = None
            self._active_device = None
        if process is not None:
            try:
                process.terminate()
            except Exception:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        return {"ok": True, "active": False}

    def _play(
        self,
        generation: int,
        path: Path,
        offset_seconds: float,
        device: dict[str, Any],
        stop_event: threading.Event,
    ) -> None:
        sample_rate = int(device["sampleRate"] or 48000)
        command = [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            f"{offset_seconds:.6f}",
            "-i",
            str(path),
            "-vn",
            "-f",
            "f32le",
            "-acodec",
            "pcm_f32le",
            "-ac",
            "2",
            "-ar",
            str(sample_rate),
            "pipe:1",
        ]
        process = None
        try:
            process = self._popen_factory(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            with self._lock:
                if generation != self._generation:
                    process.terminate()
                    return
                self._process = process
            block_frames = 2048
            backend = self._audio_backend()
            with backend.OutputStream(
                samplerate=sample_rate,
                device=device["index"],
                channels=4,
                dtype="float32",
                blocksize=block_frames,
                latency="low",
            ) as stream:
                while not stop_event.is_set():
                    raw = process.stdout.read(block_frames * 2 * 4)
                    if not raw:
                        break
                    samples = np.frombuffer(raw, dtype="<f4")
                    samples = samples[: samples.size - (samples.size % 2)].reshape(-1, 2)
                    with self._lock:
                        level = self._level
                    routed = np.zeros((samples.shape[0], 4), dtype=np.float32)
                    routed[:, 2:4] = samples * level
                    stream.write(routed)
        except Exception:
            # Device removal or decoder failure should silence cue, not stop master playback.
            pass
        finally:
            if process is not None:
                try:
                    process.terminate()
                except Exception:
                    pass
            with self._lock:
                if generation == self._generation:
                    self._process = None
                    self._thread = None
                    self._active_track_id = None
                    self._active_device = None

    def shutdown(self) -> None:
        self.stop()
