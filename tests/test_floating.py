import asyncio

import pytest

from test_voice import voice


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_floating_visibility_follows_activation_window_and_manual_hide(voice):
    voice.runtime.update_windows(main_window="hidden")
    assert not voice.runtime.connect("test")["state"]["floating_visible"]
    await voice.wake()
    state = voice.runtime.connect("test")["state"]
    assert state["floating_visible"]
    assert state["floating_reply_id"] is None
    voice.runtime.update_windows(main_window="visible")
    assert not voice.runtime.connect("test")["state"]["floating_visible"]
    voice.runtime.update_windows(main_window="minimized")
    assert voice.runtime.connect("test")["state"]["floating_visible"]
    voice.runtime.command("hide_floating")
    await voice.transcript("隐藏后继续提问。")
    snapshot = await voice.wait(lambda s: s["messages"] and s["state"]["voice_activity"] == "speaking")
    assert snapshot["state"]["floating_hidden"]
    assert not snapshot["state"]["floating_visible"]
    assert snapshot["state"]["floating_available"]
    current = snapshot["state"]["floating_reply_id"]
    voice.runtime.command("preview_floating")
    assert voice.runtime.connect("test")["state"]["floating_visible"]
    assert voice.runtime.connect("test")["state"]["floating_reply_id"] == current
    voice.runtime.command("end_voice")
    await voice.wait(lambda s: s["state"]["voice_status"] == "standby")
    assert not voice.runtime.connect("test")["state"]["floating_visible"]
    await voice.wake()
    state = voice.runtime.connect("test")["state"]
    assert state["floating_visible"]
    assert not state["floating_hidden"]
    assert state["floating_reply_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_candidate_backchannel_and_confirmed_takeover_keep_correct_floating_round(voice):
    voice.runtime.update_windows(main_window="hidden")
    await voice.wake()
    await voice.transcript("先说第一个问题。")
    first = await voice.wait(lambda s: s["state"]["voice_activity"] == "speaking")
    first_reply = first["state"]["floating_reply_id"]
    assert first["state"]["voice_playing"]
    voice.release_classifier.clear()
    await voice.transcript("嗯嗯，你继续。")
    pending = await voice.wait(lambda s: bool(voice.classifications))
    assert pending["state"]["floating_reply_id"] == first_reply
    assert pending["state"]["voice_playing"]
    assert len(pending["messages"]) == 2
    voice.release_classifier.set()
    backchannel = await voice.wait(lambda s: any(m["status"] == "backchannel" for m in s["messages"]))
    assert backchannel["state"]["floating_reply_id"] == first_reply
    voice.decision = "interrupt"
    voice.release_classifier.clear()
    await voice.transcript("等一下，换个问题。")
    candidate = await voice.wait(lambda s: len(voice.classifications) == 2)
    assert candidate["state"]["floating_reply_id"] == first_reply
    voice.release_classifier.set()
    second = await voice.wait(lambda s: s["state"]["floating_reply_id"] != first_reply)
    assert second["messages"][0]["text"] == "先说第一个问题。"
    assert any(message["text"] == "等一下，换个问题。" for message in second["messages"])
    assert any(message["status"] == "backchannel" for message in second["messages"])
    voice.release_audio()
    await voice.wait(lambda s: s["messages"][-1]["status"] == "completed" and not s["state"]["voice_playing"])


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
@pytest.mark.parametrize("visible_before_error", [True, False])
async def test_error_preserves_visible_question_and_recovery_hides_until_next_wake(voice, visible_before_error):
    if visible_before_error:
        voice.runtime.update_windows(main_window="hidden")
    await voice.wake()
    voice.failures["tts"] = 503
    await voice.transcript("保留这个问题和已有回复。")
    failed = await voice.wait(lambda s: s["state"]["voice_status"] == "error")
    assert failed["state"]["floating_visible"] == visible_before_error
    assert failed["messages"][0]["text"] == "保留这个问题和已有回复。"
    assert failed["messages"][1]["text"] == "测试回复。"
    if not visible_before_error:
        voice.runtime.update_windows(main_window="hidden")
        assert not voice.runtime.connect("test")["state"]["floating_visible"]
        voice.runtime.command("preview_floating")
        assert voice.runtime.connect("test")["state"]["floating_visible"]
    voice.runtime.command("hide_floating")
    voice.failures.clear()
    voice.runtime.command("recover_voice")
    await voice.wait(lambda s: s["state"]["voice_status"] == "standby")
    assert not voice.runtime.connect("test")["state"]["floating_visible"]
    await voice.wake()
    state = voice.runtime.connect("test")["state"]
    assert state["floating_visible"]
    assert state["floating_reply_id"] is None
    assert not state["floating_hidden"]


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_preparation_is_visible_and_end_cancels_it(voice, monkeypatch):
    voice.runtime.update_windows(main_window="hidden")
    entered = asyncio.Event()

    async def preparing():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(voice.runtime._conversation, "activate_voice", preparing)
    voice.runtime.command("wake_voice")
    await voice.wait(lambda s: s["state"]["voice_status"] == "starting")
    assert voice.runtime.connect("test")["state"]["floating_visible"]
    voice.runtime.command("end_voice")
    await voice.wait(lambda s: s["state"]["voice_status"] == "standby")
    assert not voice.runtime.connect("test")["state"]["floating_visible"]


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_voice_error_reopens_collapsed_card_and_temporary_model_error_resumes_listening(voice):
    voice.runtime.update_windows(main_window="hidden")
    await voice.wake()
    voice.runtime.command("toggle_preview_card")
    assert not voice.runtime.connect("test")["state"]["preview_expanded"]
    voice.failures["tts"] = 503
    await voice.transcript("请回答。")
    failed = await voice.wait(lambda s: s["state"]["voice_status"] == "error")
    assert failed["state"]["preview_expanded"]
    assert failed["state"]["floating_visible"]
    previous_reply = failed["state"]["floating_reply_id"]
    voice.failures.clear()
    voice.runtime.command("recover_voice")
    await voice.wait(lambda s: s["state"]["voice_status"] == "standby")
    await voice.wake()
    voice.failures["model"] = 503
    await voice.transcript("新的简短问题。")
    failed = await voice.wait(lambda s: s["state"]["floating_reply_id"] not in {None, previous_reply}
                              and s["messages"][-1]["status"] == "failed")
    assert failed["state"]["voice_activity"] == "listening"
    assert failed["state"]["active_reply_id"] is None
    assert failed["state"]["model_available"]
