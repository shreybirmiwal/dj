from pathlib import Path

from setmix.desktop import DesktopBridge


class FakePort:
    def __init__(self, callback=None):
        self.callback = callback
        self.closed = False
        self.sent = []

    def close(self):
        self.closed = True

    def send(self, message):
        self.sent.append(message)


class FakeMessage:
    def __init__(self, values):
        self.values = values

    def bytes(self):
        return self.values

    @classmethod
    def from_bytes(cls, values):
        return cls(values)


class FakeMidi:
    Message = FakeMessage

    def __init__(self):
        self.input = None
        self.output = None

    def get_input_names(self):
        return ["Other MIDI", "DDJ-FLX4 MIDI"]

    def get_output_names(self):
        return ["DDJ-FLX4 MIDI"]

    def open_input(self, _name, callback=None):
        self.input = FakePort(callback)
        return self.input

    def open_output(self, _name):
        self.output = FakePort()
        return self.output


class FakeWindow:
    def __init__(self):
        self.scripts = []

    def evaluate_js(self, script):
        self.scripts.append(script)


class FakeCueEngine:
    def __init__(self):
        self.started = None
        self.level = None
        self.stopped = False
        self.shutdown_called = False

    def status(self):
        return {"ok": True, "available": True, "device": "DDJ-FLX4"}

    def start(self, track_id, **options):
        self.started = (track_id, options)
        return {"ok": True, "active": True, "trackId": track_id}

    def set_level(self, level):
        self.level = level
        return {"ok": True, "level": level}

    def test_route(self, route):
        return {"ok": True, "route": route, "channels": "1/2" if route == "master" else "3/4"}

    def stop(self):
        self.stopped = True
        return {"ok": True, "active": False}

    def shutdown(self):
        self.shutdown_called = True


def test_native_bridge_connects_flx4_and_forwards_messages(tmp_path: Path):
    midi = FakeMidi()
    window = FakeWindow()
    bridge = DesktopBridge(tmp_path, mido_module=midi)
    bridge.set_window(window)

    result = bridge.connect_controller()
    assert result == {
        "ok": True,
        "name": "DDJ-FLX4 MIDI",
        "input": "DDJ-FLX4 MIDI",
        "output": "DDJ-FLX4 MIDI",
    }

    midi.input.callback(FakeMessage([0x90, 0x0B, 0x7F]))
    assert "[144, 11, 127]" in window.scripts[-1]

    assert bridge.send_midi([0x90, 0x0B, 0x7F]) == {"ok": True}
    assert midi.output.sent[0].bytes() == [0x90, 0x0B, 0x7F]

    bridge.shutdown()
    assert midi.input.closed
    assert midi.output.closed


def test_native_bridge_reports_missing_controller(tmp_path: Path):
    midi = FakeMidi()
    midi.get_input_names = lambda: ["Other MIDI"]
    bridge = DesktopBridge(tmp_path, mido_module=midi)

    result = bridge.connect_controller()
    assert result["ok"] is False
    assert "DDJ-FLX4 not detected" in result["error"]


def test_native_bridge_exposes_separate_headphone_cue(tmp_path: Path, monkeypatch):
    cue = FakeCueEngine()
    bridge = DesktopBridge(tmp_path, mido_module=FakeMidi(), cue_engine=cue)

    class FakeRouter:
        def route_to_flx4(self):
            return {"ok": True, "device": "DDJ-FLX4"}

    monkeypatch.setattr("setmix.coreaudio.CoreAudioRouter", FakeRouter)

    assert bridge.list_audio_outputs()["available"] is True
    assert bridge.start_headphone_cue("track-1", 15.0, 0.6)["ok"] is True
    assert cue.started == ("track-1", {"offset_seconds": 15.0, "level": 0.6})
    assert bridge.set_headphone_level(0.25) == {"ok": True, "level": 0.25}
    assert bridge.stop_headphone_cue() == {"ok": True, "active": False}
    assert bridge.test_audio_route("phones")["channels"] == "3/4"
    assert bridge.route_master_to_flx4() == {"ok": True, "device": "DDJ-FLX4"}
    bridge.shutdown()
    assert cue.shutdown_called is True


def test_hardware_diagnostics_report_midi_activity(tmp_path: Path):
    midi = FakeMidi()
    cue = FakeCueEngine()
    bridge = DesktopBridge(tmp_path, mido_module=midi, cue_engine=cue)
    bridge.set_window(FakeWindow())
    bridge.connect_controller()
    midi.input.callback(FakeMessage([0x90, 0x0B, 0x7F]))

    diagnostics = bridge.hardware_diagnostics()

    assert diagnostics["midi"]["connected"] is True
    assert diagnostics["midi"]["messageCount"] == 1
    assert diagnostics["midi"]["lastMessage"] == [0x90, 0x0B, 0x7F]
