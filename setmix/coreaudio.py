"""Small macOS CoreAudio bridge for selecting the FLX4 master output."""

from __future__ import annotations

import ctypes
import ctypes.util
import re
import sys
from dataclasses import dataclass
from typing import Any


FLX4_PATTERN = re.compile(r"DDJ[- ]?FLX4|Pioneer DJ", re.IGNORECASE)


def fourcc(value: str) -> int:
    if len(value) != 4:
        raise ValueError("CoreAudio property codes must contain four characters")
    return int.from_bytes(value.encode("latin-1"), "big")


class AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("selector", ctypes.c_uint32),
        ("scope", ctypes.c_uint32),
        ("element", ctypes.c_uint32),
    ]


@dataclass(frozen=True)
class CoreAudioDevice:
    object_id: int
    name: str


class CoreAudioRouter:
    """Read and update the system's CoreAudio output-device properties."""

    SYSTEM_OBJECT = 1
    SCOPE_GLOBAL = fourcc("glob")
    ELEMENT_MAIN = 0
    PROPERTY_DEVICES = fourcc("dev#")
    PROPERTY_NAME = fourcc("lnam")
    PROPERTY_DEFAULT_OUTPUT = fourcc("dOut")
    PROPERTY_DEFAULT_SYSTEM_OUTPUT = fourcc("sOut")
    UTF8_ENCODING = 0x08000100

    def __init__(self, *, coreaudio: Any | None = None, corefoundation: Any | None = None) -> None:
        if sys.platform != "darwin" and coreaudio is None:
            raise RuntimeError("Direct output routing is only available on macOS")
        self._ca = coreaudio or self._load_framework("CoreAudio")
        self._cf = corefoundation or self._load_framework("CoreFoundation")
        self._configure_signatures()

    @staticmethod
    def _load_framework(name: str) -> Any:
        path = ctypes.util.find_library(name)
        if not path:
            raise RuntimeError(f"macOS {name} framework is unavailable")
        return ctypes.cdll.LoadLibrary(path)

    def _configure_signatures(self) -> None:
        pointer = ctypes.POINTER(AudioObjectPropertyAddress)
        self._ca.AudioObjectGetPropertyDataSize.argtypes = [
            ctypes.c_uint32, pointer, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)
        ]
        self._ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
        self._ca.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32,
            pointer,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        self._ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
        self._ca.AudioObjectSetPropertyData.argtypes = [
            ctypes.c_uint32,
            pointer,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        self._ca.AudioObjectSetPropertyData.restype = ctypes.c_int32
        self._cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
        self._cf.CFStringGetLength.restype = ctypes.c_long
        self._cf.CFStringGetMaximumSizeForEncoding.argtypes = [ctypes.c_long, ctypes.c_uint32]
        self._cf.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
        self._cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        self._cf.CFStringGetCString.restype = ctypes.c_bool

    @classmethod
    def _address(cls, selector: int) -> AudioObjectPropertyAddress:
        return AudioObjectPropertyAddress(selector, cls.SCOPE_GLOBAL, cls.ELEMENT_MAIN)

    @staticmethod
    def _check(status: int, operation: str) -> None:
        if status == 0:
            return
        unsigned = int(status) & 0xFFFFFFFF
        raw = unsigned.to_bytes(4, "big")
        label = raw.decode("latin-1") if all(32 <= byte < 127 for byte in raw) else str(status)
        raise RuntimeError(f"CoreAudio {operation} failed ({label})")

    def _property_size(self, object_id: int, selector: int) -> int:
        address = self._address(selector)
        size = ctypes.c_uint32()
        status = self._ca.AudioObjectGetPropertyDataSize(
            object_id, ctypes.byref(address), 0, None, ctypes.byref(size)
        )
        self._check(status, "property-size query")
        return int(size.value)

    def _read_object_id(self, object_id: int, selector: int) -> int:
        address = self._address(selector)
        value = ctypes.c_uint32()
        size = ctypes.c_uint32(ctypes.sizeof(value))
        status = self._ca.AudioObjectGetPropertyData(
            object_id, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(value)
        )
        self._check(status, "property read")
        return int(value.value)

    def _device_name(self, object_id: int) -> str:
        address = self._address(self.PROPERTY_NAME)
        value = ctypes.c_void_p()
        size = ctypes.c_uint32(ctypes.sizeof(value))
        status = self._ca.AudioObjectGetPropertyData(
            object_id, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(value)
        )
        self._check(status, "device-name read")
        if not value.value:
            return f"CoreAudio device {object_id}"
        length = self._cf.CFStringGetLength(value)
        capacity = self._cf.CFStringGetMaximumSizeForEncoding(length, self.UTF8_ENCODING) + 1
        buffer = ctypes.create_string_buffer(max(1, capacity))
        if not self._cf.CFStringGetCString(value, buffer, capacity, self.UTF8_ENCODING):
            return f"CoreAudio device {object_id}"
        return buffer.value.decode("utf-8", errors="replace")

    def devices(self) -> list[CoreAudioDevice]:
        address = self._address(self.PROPERTY_DEVICES)
        size = self._property_size(self.SYSTEM_OBJECT, self.PROPERTY_DEVICES)
        count = size // ctypes.sizeof(ctypes.c_uint32)
        if count <= 0:
            return []
        values = (ctypes.c_uint32 * count)()
        data_size = ctypes.c_uint32(size)
        status = self._ca.AudioObjectGetPropertyData(
            self.SYSTEM_OBJECT, ctypes.byref(address), 0, None, ctypes.byref(data_size), values
        )
        self._check(status, "device-list read")
        return [CoreAudioDevice(int(value), self._device_name(int(value))) for value in values]

    def default_output(self) -> CoreAudioDevice | None:
        object_id = self._read_object_id(self.SYSTEM_OBJECT, self.PROPERTY_DEFAULT_OUTPUT)
        if object_id == 0:
            return None
        return CoreAudioDevice(object_id, self._device_name(object_id))

    def _set_default(self, selector: int, object_id: int) -> None:
        address = self._address(selector)
        value = ctypes.c_uint32(object_id)
        status = self._ca.AudioObjectSetPropertyData(
            self.SYSTEM_OBJECT,
            ctypes.byref(address),
            0,
            None,
            ctypes.sizeof(value),
            ctypes.byref(value),
        )
        self._check(status, "default-output update")

    def route_to_flx4(self) -> dict[str, Any]:
        previous = self.default_output()
        target = next((device for device in self.devices() if FLX4_PATTERN.search(device.name)), None)
        if target is None:
            return {"ok": False, "error": "DDJ-FLX4 CoreAudio output was not found"}
        try:
            self._set_default(self.PROPERTY_DEFAULT_OUTPUT, target.object_id)
            self._set_default(self.PROPERTY_DEFAULT_SYSTEM_OUTPUT, target.object_id)
        except Exception as error:
            return {"ok": False, "error": str(error)}
        current = self.default_output()
        if current is None or current.object_id != target.object_id:
            return {"ok": False, "error": "macOS did not accept the FLX4 output route"}
        return {
            "ok": True,
            "device": target.name,
            "previousDevice": previous.name if previous else None,
            "channels": "USB 1/2 → RCA MASTER",
        }

