import pytest
from pipecat.frames.frames import InputAudioRawFrame, TranscriptionFrame
from pipecat.tests.utils import run_test

from voice_super_agent.desktop.hotkey import HoldHotkey
from voice_super_agent.voice.lifecycle import VoiceLifecycle, WakeSettings
from voice_super_agent.voice.wake import WakeAudioGate, RecognitionGate


def test_hold_requires_one_second_and_release_before_another_wake():
    hotkey = HoldHotkey("Ctrl+\\")
    assert not hotkey.sample(True, 10)
    assert not hotkey.sample(True, 10.99)
    assert hotkey.sample(True, 11)
    assert not hotkey.sample(True, 14)
    assert not hotkey.sample(False, 14.1)
    assert not hotkey.sample(True, 15)
    assert hotkey.sample(True, 16)
    assert not hotkey.sample(False, 17)
    assert not hotkey.sample(True, 18)
    assert not hotkey.sample(False, 18.5)
    assert not hotkey.sample(True, 19)
    assert not hotkey.sample(True, 19.6)


def test_custom_hotkey_matches_modifiers_and_rejects_invalid_combinations(monkeypatch):
    from voice_super_agent.desktop import hotkey as module

    pressed = {0x11, 0x10, ord("K")}
    monkeypatch.setattr(module.user32, "GetAsyncKeyState", lambda key: 0x8000 if key in pressed else 0)
    custom = HoldHotkey("Ctrl+Shift+K")
    assert custom.pressed()
    pressed.add(0x12)
    assert not custom.pressed()
    for combination in ("Ctrl", "Ctrl+Ctrl+K", "Ctrl+K+L", "Ctrl+Unknown"):
        with pytest.raises(ValueError, match="组合键格式"):
            HoldHotkey(combination)


def test_only_waiting_time_counts_and_overlapping_work_restarts_the_full_period():
    state = VoiceLifecycle(60)
    state.activate(10)
    assert not state.should_end(69.99)
    state.activity("capture", True, 20)
    state.activity("recognition", True, 21)
    state.activity("capture", False, 22)
    assert not state.should_end(500)
    state.activity("reply", True, 501)
    state.activity("recognition", False, 502)
    state.activity("model", True, 503)
    state.activity("model", False, 504)
    state.activity("playback", True, 505)
    state.activity("reply", False, 506)
    assert not state.should_end(600)
    state.activity("playback", False, 601)
    state.activity("playback", False, 650)
    assert not state.should_end(660.99)
    assert state.should_end(661)
    state.deactivate()
    assert not state.should_end(10000)
    state.activate(10000)
    assert state.deadline == 10060


def test_ending_waits_for_its_reply_and_playback_and_new_input_cancels_it():
    state = VoiceLifecycle()
    state.activate(0)
    state.ending_reply = "old"
    state.finished_reply = "old"
    state.activity("playback", True, 1)
    assert not state.should_end(2)
    state.new_input()
    state.activity("playback", False, 3)
    state.finished_reply = "old"
    assert not state.should_end(4)
    state.ending_reply = "new"
    assert not state.should_end(5)
    state.finished_reply = "new"
    assert state.should_end(5)


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "abc"])
def test_timeout_configuration_requires_positive_integer(monkeypatch, value):
    monkeypatch.setenv("VOICE_IDLE_TIMEOUT_SECONDS", value)
    with pytest.raises(ValueError):
        WakeSettings.from_environment()


@pytest.mark.asyncio
async def test_standby_pcm_only_reaches_kws_and_wake_fires_once():
    events, detected_audio = [], []

    class Detector:
        def feed(self, audio, rate):
            detected_audio.append((audio, rate))
            return True

    gate = WakeAudioGate(events.append)
    gate.mode = "standby"
    gate.detector = Detector()
    pcm = b"\x01\x00" * 320
    down, _ = await run_test(gate, frames_to_send=[InputAudioRawFrame(pcm, 16000, 1), InputAudioRawFrame(pcm, 16000, 1)])
    assert events == [{"type": "wake"}]
    assert detected_audio == [(pcm, 16000)]
    assert [frame.audio for frame in down if isinstance(frame, InputAudioRawFrame)] == [bytes(640), bytes(640)]


@pytest.mark.asyncio
async def test_active_pcm_passes_and_preparing_transcripts_are_discarded():
    gate = WakeAudioGate(lambda event: None)
    gate.mode = "active"
    pcm = b"\x01\x00" * 320
    down, _ = await run_test(gate, frames_to_send=[InputAudioRawFrame(pcm, 16000, 1)])
    assert [frame.audio for frame in down if isinstance(frame, InputAudioRawFrame)] == [pcm]
    gate.mode = "paused"
    down, _ = await run_test(RecognitionGate(gate), frames_to_send=[TranscriptionFrame("旧识别", "u", "")])
    assert not any(isinstance(frame, TranscriptionFrame) for frame in down)
