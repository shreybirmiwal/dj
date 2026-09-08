from setmix.coreaudio import CoreAudioDevice, CoreAudioRouter, fourcc


class FakeRouter(CoreAudioRouter):
    def __init__(self, devices, default):
        self._devices = devices
        self._default = default
        self.updated = []

    def devices(self):
        return self._devices

    def default_output(self):
        return self._default

    def _set_default(self, selector, object_id):
        self.updated.append((selector, object_id))
        self._default = next(device for device in self._devices if device.object_id == object_id)


def test_fourcc_matches_coreaudio_integer_encoding():
    assert fourcc("dOut") == 0x644F7574


def test_router_sets_media_and_system_output_to_flx4():
    speakers = CoreAudioDevice(10, "MacBook Pro Speakers")
    flx4 = CoreAudioDevice(20, "DDJ-FLX4")
    router = FakeRouter([speakers, flx4], speakers)

    result = router.route_to_flx4()

    assert result["ok"] is True
    assert result["device"] == "DDJ-FLX4"
    assert result["previousDevice"] == "MacBook Pro Speakers"
    assert router.updated == [
        (router.PROPERTY_DEFAULT_OUTPUT, 20),
        (router.PROPERTY_DEFAULT_SYSTEM_OUTPUT, 20),
    ]


def test_router_reports_missing_flx4_without_changing_output():
    speakers = CoreAudioDevice(10, "MacBook Pro Speakers")
    router = FakeRouter([speakers], speakers)

    assert router.route_to_flx4()["ok"] is False
    assert router.updated == []
