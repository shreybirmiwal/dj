"""Native desktop shell and MIDI bridge for SetMix."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any


FLX4_PATTERN = re.compile(r"DDJ[- ]?FLX4|Pioneer DJ", re.IGNORECASE)


def application_data_dir() -> Path:
    """Return a writable, platform-native home for desktop caches."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SetMix"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "SetMix"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "setmix"


def default_desktop_music_dir() -> Path:
    configured = os.environ.get("SETMIX_MUSIC_DIR")
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path.home() / "Documents" / "DJ Music",
        Path.home() / "Documents" / "dj-music",
        Path.home() / "Desktop" / "dj-music",
    ]
    return next((path for path in candidates if path and path.is_dir()), candidates[1])


class DesktopBridge:
    """Methods exposed to the UI plus native MIDI ownership."""

    def __init__(self, music_dir: Path, *, mido_module: Any | None = None) -> None:
        self.music_dir = music_dir
        self._mido = mido_module
        self._window: Any | None = None
        self._input: Any | None = None
        self._output: Any | None = None
        self._lock = threading.Lock()

    def set_window(self, window: Any) -> None:
        self._window = window

    def _midi_backend(self) -> Any:
        if self._mido is None:
            try:
                import mido
            except ImportError as error:
                raise RuntimeError(
                    "Desktop MIDI support is not installed; run pip install -r requirements-desktop.txt"
                ) from error
            self._mido = mido
        return self._mido

    def runtime_info(self) -> dict[str, Any]:
        return {
            "desktop": True,
            "platform": platform.system(),
            "musicDir": str(self.music_dir),
            "midiBackend": "python-rtmidi",
            "audioMode": "native window / system audio",
        }

    def list_midi_devices(self) -> dict[str, Any]:
        try:
            midi = self._midi_backend()
            inputs = list(midi.get_input_names())
            outputs = list(midi.get_output_names())
            return {
                "ok": True,
                "inputs": inputs,
                "outputs": outputs,
                "flx4Inputs": [name for name in inputs if FLX4_PATTERN.search(name)],
                "flx4Outputs": [name for name in outputs if FLX4_PATTERN.search(name)],
            }
        except Exception as error:
            return {"ok": False, "error": str(error), "inputs": [], "outputs": []}

    def connect_controller(self, preferred_name: str | None = None) -> dict[str, Any]:
        devices = self.list_midi_devices()
        if not devices.get("ok"):
            return devices
        matches = devices["flx4Inputs"]
        if preferred_name and preferred_name in devices["inputs"]:
            input_name = preferred_name
        elif matches:
            input_name = matches[0]
        else:
            return {"ok": False, "error": "DDJ-FLX4 not detected. Check USB and close other DJ software."}

        midi = self._midi_backend()
        output_names = devices["flx4Outputs"]
        with self._lock:
            self._close_ports()
            self._input = midi.open_input(input_name, callback=self._on_midi_message)
            self._output = midi.open_output(output_names[0]) if output_names else None
        return {
            "ok": True,
            "name": input_name,
            "input": input_name,
            "output": output_names[0] if output_names else None,
        }

    def send_midi(self, data: list[int]) -> dict[str, Any]:
        try:
            values = [max(0, min(255, int(value))) for value in data]
            if len(values) != 3:
                raise ValueError("MIDI messages must contain three bytes")
            with self._lock:
                if self._output is None:
                    return {"ok": False, "error": "No MIDI output is connected"}
                self._output.send(self._midi_backend().Message.from_bytes(values))
            return {"ok": True}
        except Exception as error:
            return {"ok": False, "error": str(error)}

    def open_music_folder(self) -> dict[str, Any]:
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(self.music_dir)])
            elif os.name == "nt":
                os.startfile(self.music_dir)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(self.music_dir)])
            return {"ok": True}
        except Exception as error:
            return {"ok": False, "error": str(error)}

    def _on_midi_message(self, message: Any) -> None:
        try:
            values = list(message.bytes())
            if len(values) < 3 or self._window is None:
                return
            payload = json.dumps(values[:3])
            self._window.evaluate_js(
                f"window.dispatchEvent(new CustomEvent('setmix-midi', {{detail: {payload}}}));"
            )
        except Exception:
            # A disconnected controller must never interrupt the audio engine.
            return

    def _close_ports(self) -> None:
        for port in (self._input, self._output):
            if port is not None:
                try:
                    port.close()
                except Exception:
                    pass
        self._input = None
        self._output = None

    def shutdown(self) -> None:
        with self._lock:
            self._close_ports()


def run_desktop(
    music_dir: Path,
    *,
    port: int = 0,
    debug: bool = False,
    webview_module: Any | None = None,
) -> None:
    data_dir = application_data_dir()
    project_dir = Path(__file__).resolve().parents[1]
    project_cache = project_dir / ".setmix-cache"
    running_from_checkout = (project_dir / "ui" / "server.py").is_file()
    cache_dir = project_cache if running_from_checkout else data_dir / "cache"
    work_dir = project_dir if running_from_checkout else data_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["SETMIX_CACHE_DIR"] = str(cache_dir)
    os.chdir(work_dir)

    from ui.server import create_server

    server = create_server(music_dir, port)
    server_thread = threading.Thread(target=server.serve_forever, name="setmix-ui", daemon=True)
    server_thread.start()

    if webview_module is None:
        try:
            import webview as webview_module
        except ImportError as error:
            server.shutdown()
            raise RuntimeError(
                "The desktop runtime is not installed; run pip install -r requirements-desktop.txt"
            ) from error

    bridge = DesktopBridge(music_dir.resolve())
    window = webview_module.create_window(
        "SetMix — AI Native DJ",
        f"http://127.0.0.1:{server.server_port}",
        js_api=bridge,
        width=1500,
        height=960,
        min_size=(1050, 720),
        background_color="#0b0c10",
        text_select=True,
    )
    bridge.set_window(window)
    try:
        webview_module.start(debug=debug, private_mode=False)
    finally:
        bridge.shutdown()
        server.shutdown()
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch the native SetMix desktop application")
    parser.add_argument("--music-dir", type=Path, default=default_desktop_music_dir())
    parser.add_argument("--port", type=int, default=0, help="Private local UI port (0 chooses automatically)")
    parser.add_argument("--debug", action="store_true", help="Enable the embedded web inspector")
    args = parser.parse_args()
    music_dir = args.music_dir.expanduser().resolve()
    if not music_dir.is_dir():
        raise SystemExit(f"Music folder does not exist: {music_dir}")
    try:
        run_desktop(music_dir, port=args.port, debug=args.debug)
    except RuntimeError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
