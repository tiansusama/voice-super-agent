from dataclasses import dataclass

import comtypes
from pycaw.constants import DEVICE_STATE, EDataFlow, ERole
from pycaw.pycaw import AudioUtilities


@dataclass(frozen=True)
class Endpoint:
    id: str
    name: str


@dataclass(frozen=True)
class AudioDevices:
    microphone: Endpoint
    speaker: Endpoint
    active_ids: frozenset[str]

    @property
    def defaults(self):
        return self.microphone.id, self.speaker.id

    def contains(self, binding: "AudioDevices") -> bool:
        return all(device_id in self.active_ids for device_id in binding.defaults)


def read_devices() -> AudioDevices:
    """读取 Windows 默认多媒体端点及存活状态；COM 对象只在本次调用线程使用。"""
    comtypes.CoInitialize()
    try:
        enumerator = AudioUtilities.GetDeviceEnumerator()
        microphone = AudioUtilities.CreateDevice(enumerator.GetDefaultAudioEndpoint(
            EDataFlow.eCapture.value, ERole.eMultimedia.value))
        speaker = AudioUtilities.CreateDevice(enumerator.GetDefaultAudioEndpoint(
            EDataFlow.eRender.value, ERole.eMultimedia.value))
        collection = enumerator.EnumAudioEndpoints(EDataFlow.eAll.value, DEVICE_STATE.ACTIVE.value)
        active = frozenset(collection.Item(index).GetId() for index in range(collection.GetCount()))
        return AudioDevices(Endpoint(microphone.id, microphone.FriendlyName),
                            Endpoint(speaker.id, speaker.FriendlyName), active)
    except comtypes.COMError as exc:
        raise OSError(f"无法读取 Windows 默认录音或播放设备：{exc}") from exc
    finally:
        comtypes.CoUninitialize()
