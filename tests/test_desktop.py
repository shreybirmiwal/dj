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
