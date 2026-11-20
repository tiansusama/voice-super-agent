import asyncio
from copy import deepcopy
import sqlite3
from types import SimpleNamespace

import pytest

from test_voice import voice
from voice_super_agent import runtime as runtime_module
from voice_super_agent.memory.store import ConversationStore
from voice_super_agent.runtime import Runtime


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("VOICE_WAKE_WORD", raising=False)
    monkeypatch.delenv("VOICE_WAKE_HOTKEY", raising=False)
    monkeypatch.delenv("VOICE_IDLE_TIMEOUT_SECONDS", raising=False)
    runtime = Runtime(database_path=tmp_path / "settings.sqlite3")
    runtime.start()
    try:
        yield runtime
    finally:
        runtime.stop()


def test_saved_hotkey_and_timeout_override_environment_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_WAKE_HOTKEY", "Ctrl+Shift+J")
    monkeypatch.setenv("VOICE_IDLE_TIMEOUT_SECONDS", "81")
    path = tmp_path / "saved.sqlite3"
    runtime = Runtime(database_path=path)
    runtime.start()
    try:
        assert runtime.connect("main")["state"]["wake_hotkey"] == "Ctrl+Shift+J"
        old_hotkey = runtime._hotkey
        runtime.save_setting("hotkey", "Ctrl+Alt+K")
        runtime.save_setting("idle_seconds", 42)
        assert runtime._hotkey is not old_hotkey
    finally:
        runtime.stop()
    monkeypatch.setenv("VOICE_WAKE_HOTKEY", "F12")
    runtime = Runtime(database_path=path)
    runtime.start()
    try:
        state = runtime.connect("main")["state"]
        assert (state["wake_hotkey"], state["voice_idle_seconds"]) == ("Ctrl+Alt+K", 42)
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
    finally:
        runtime.stop()


def test_repair_timeout_restores_other_valid_environment_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_WAKE_WORD", "你好朋友")
    monkeypatch.setenv("VOICE_WAKE_HOTKEY", "F12")
    monkeypatch.setenv("VOICE_IDLE_TIMEOUT_SECONDS", "bad")
    runtime = Runtime(database_path=tmp_path / "repair.sqlite3")
    runtime.start()
    try:
        runtime.save_setting("idle_seconds", 25)
        state = runtime.connect("main")["state"]
        assert (state["wake_word"], state["wake_hotkey"], state["voice_idle_seconds"]) == ("你好朋友", "F12", 25)
        assert runtime._hotkey._key == 0x7B
        assert state["settings_error"] == ""
    finally:
        runtime.stop()


@pytest.mark.parametrize("word,reason", [("hello", "中文短语"), ("你好朋友", "不支持该词条")])
def test_same_unvalidated_word_is_rejected_and_environment_can_be_corrected(tmp_path, monkeypatch, word, reason):
    monkeypatch.setenv("VOICE_WAKE_WORD", word)
    monkeypatch.delenv("VOICE_WAKE_HOTKEY", raising=False)
    monkeypatch.delenv("VOICE_IDLE_TIMEOUT_SECONDS", raising=False)
    model = tmp_path / "model"
    model.mkdir()
    (model / "tokens.txt").write_text("<blk> 0\n", encoding="utf-8")
    path = tmp_path / "invalid-word.sqlite3"
    runtime = Runtime(database_path=path)
    runtime.start()
    try:
        runtime._wake_model_dir = model
        with pytest.raises(ValueError, match=reason):
            runtime.save_setting("word", word)
        assert runtime._settings_values == {}
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT count(*) FROM interaction_settings").fetchone()[0] == 0
    finally:
        runtime.stop()
    monkeypatch.setenv("VOICE_WAKE_WORD", "你好小博")
    runtime = Runtime(database_path=path)
    runtime.start()
    try:
        assert runtime.connect("main")["state"]["wake_word"] == "你好小博"
    finally:
        runtime.stop()


@pytest.mark.parametrize("order", [("hotkey", "idle_seconds"), ("idle_seconds", "hotkey")])
@pytest.mark.parametrize("prepare_voice", [False, True])
def test_invalid_environment_fields_can_be_repaired_in_either_order(tmp_path, monkeypatch, order, prepare_voice):
    monkeypatch.setenv("VOICE_WAKE_HOTKEY", "Ctrl")
    monkeypatch.setenv("VOICE_IDLE_TIMEOUT_SECONDS", "0")
    for name in ("DEEPSEEK_API_KEY", "VOLCENGINE_API_KEY", "ARK_API_KEY"):
        monkeypatch.setenv(name, "configuration-check-only")
    values = {"hotkey": "F8", "idle_seconds": 30}
    errors = {"hotkey": "组合键", "idle_seconds": "超时"}
    path = tmp_path / "two-errors.sqlite3"
    runtime = Runtime(database_path=path, wake_model_dir=tmp_path if prepare_voice else None)
    runtime.start()
    try:
        if prepare_voice:
            asyncio.run_coroutine_threadsafe(asyncio.wait({runtime._voice_task}), runtime._loop).result()
            assert "语音启动失败：交互配置无效：" in runtime.connect("main")["state"]["voice_error"]
        runtime.save_setting(order[0], values[order[0]])
        state = runtime.connect("main")["state"]
        assert errors[order[1]] in state["settings_error"]
        assert errors[order[1]] in state["voice_error"]
        assert errors[order[0]] not in state["voice_error"]
        with pytest.raises(ValueError, match=errors[order[1]]):
            runtime._require_voice_configuration()
        runtime.save_setting(order[1], values[order[1]])
        state = runtime.connect("main")["state"]
        assert (state["wake_hotkey"], state["voice_idle_seconds"]) == ("F8", 30)
        assert state["settings_error"] == ""
        assert state["voice_error"] == "交互配置已修正，请恢复语音。"
        runtime._require_voice_configuration()
    finally:
        runtime.stop()
    runtime = Runtime(database_path=path)
    runtime.start()
    try:
        state = runtime.connect("main")["state"]
        assert (state["wake_hotkey"], state["voice_idle_seconds"]) == ("F8", 30)
        runtime._require_voice_configuration()
    finally:
        runtime.stop()


def test_same_word_reuses_model_validation_only_after_detector_is_ready(app, tmp_path, monkeypatch):
    candidate = SimpleNamespace(reset=lambda: None, feed=lambda audio, rate: False)
    prepared = []
    def detector(directory, word):
        prepared.append(word)
        return candidate
    monkeypatch.setattr(runtime_module, "KeywordDetector", detector)
    app._wake_model_dir = tmp_path
    app.update_windows(voice_status="standby")
    app.save_setting("word", "你好小博")
    assert prepared == ["你好小博"]
    assert app._detector is candidate
    app.save_setting("word", "你好小博")
    assert prepared == ["你好小博"]
    assert app._detector is candidate
    assert app.connect("main")["state"]["pending_wake_word"] is None


def test_failed_repair_keeps_configuration_error_until_setting_is_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_IDLE_TIMEOUT_SECONDS", "bad")
    runtime = Runtime(database_path=tmp_path / "failed-repair.sqlite3")
    runtime.start()
    try:
        saved = runtime._store._save_settings
        def fail(values):
            raise sqlite3.OperationalError("测试保存失败")
        monkeypatch.setattr(runtime._store, "_save_settings", fail)
        with pytest.raises(ValueError, match="测试保存失败"):
            runtime.save_setting("idle_seconds", 25)
        with pytest.raises(ValueError, match="超时"):
            runtime._require_voice_configuration()
        assert runtime.connect("main")["state"]["voice_idle_seconds"] == 60
        assert runtime._settings_values == {}
        monkeypatch.setattr(runtime._store, "_save_settings", saved)
        runtime.save_setting("idle_seconds", 25)
        assert runtime.connect("main")["state"]["settings_error"] == ""
    finally:
        runtime.stop()


@pytest.mark.parametrize("name,value", [("hotkey", "Ctrl+Ctrl+J"), ("hotkey", "Ctrl+K+J"),
                                       ("idle_seconds", 0), ("idle_seconds", -1), ("idle_seconds", 1.2),
                                       ("idle_seconds", True), ("idle_seconds", "30")])
def test_invalid_settings_preserve_effective_and_saved_values(app, name, value):
    app.save_setting("hotkey", "Ctrl+Shift+K")
    app.save_setting("idle_seconds", 35)
    original = dict(app._settings_values)
    with pytest.raises(ValueError):
        app.save_setting(name, value)
    state = app.connect("main")["state"]
    assert (state["wake_hotkey"], state["voice_idle_seconds"]) == ("Ctrl+Shift+K", 35)
    assert app._settings_values == original
    assert state["settings_error"] and not state["settings_saving"]


def test_settings_transaction_failure_keeps_old_hotkey_and_timeout(app, monkeypatch):
    original = app._hotkey
    def fail(values):
        raise sqlite3.OperationalError("测试磁盘已满")
    monkeypatch.setattr(app._store, "_save_settings", fail)
    with pytest.raises(ValueError, match="磁盘已满"):
        app.save_setting("hotkey", "F8")
    assert app._hotkey is original
    assert app.connect("main")["state"]["wake_hotkey"] == "Ctrl+\\"
    assert app._settings_values == {}


def test_timeout_change_resets_waiting_and_waits_for_busy_work(app):
    async def activate():
        app._lifecycle.activate(app._loop.time() - 40)
    asyncio.run_coroutine_threadsafe(activate(), app._loop).result()
    app.save_setting("idle_seconds", 17)
    assert 16 < app._lifecycle.deadline - app._loop.time() <= 17
    async def busy():
        app._lifecycle.activity("tool", True, app._loop.time())
    asyncio.run_coroutine_threadsafe(busy(), app._loop).result()
    app.save_setting("idle_seconds", 28)
    assert app._lifecycle.deadline is None
    async def done():
        app._lifecycle.activity("tool", False, app._loop.time())
    asyncio.run_coroutine_threadsafe(done(), app._loop).result()
    assert 27 < app._lifecycle.deadline - app._loop.time() <= 28


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_word_waits_for_standby_and_replaces_only_detector(voice, monkeypatch):
    runtime = voice.runtime
    await voice.wake()
    old_detector = runtime._detector
    candidate = SimpleNamespace(reset=lambda: None, feed=lambda audio, rate: False)
    monkeypatch.setattr(runtime_module, "KeywordDetector", lambda directory, word: candidate)
    conversation = runtime._conversation
    runtime.save_setting("word", "你好朋友")
    state = runtime.connect("main")["state"]
    assert state["wake_word"] == "你好小博" and state["pending_wake_word"] == "你好朋友"
    assert conversation._voice.gate.detector is old_detector
    runtime.command("end_voice")
    await voice.wait(lambda snapshot: snapshot["state"]["voice_status"] == "standby")
    assert runtime._conversation is conversation
    assert conversation._voice.gate.detector is candidate
    assert runtime.connect("main")["state"]["pending_wake_word"] is None
    assert runtime.connect("main")["state"]["wake_word"] == "你好朋友"
    next_detector = SimpleNamespace(reset=lambda: None, feed=lambda audio, rate: False)
    monkeypatch.setattr(runtime_module, "KeywordDetector", lambda directory, word: next_detector)
    runtime.save_setting("word", "你好小友")
    assert conversation._voice.gate.detector is next_detector
    assert runtime.connect("main")["state"]["wake_word"] == "你好小友"


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_invalid_word_and_apply_failure_preserve_old_running_detector(voice, monkeypatch):
    runtime = voice.runtime
    original = runtime._detector
    def unsupported(directory, word):
        raise ValueError("唤醒模型不支持该词条的发音")
    monkeypatch.setattr(runtime_module, "KeywordDetector", unsupported)
    with pytest.raises(ValueError, match="不支持"):
        runtime.save_setting("word", "无法支持的词条")
    assert runtime._settings_values == {}
    candidate = SimpleNamespace(reset=lambda: None, feed=lambda audio, rate: False)
    monkeypatch.setattr(runtime_module, "KeywordDetector", lambda directory, word: candidate)
    save = runtime._store._save_settings
    def fail_on_apply(values):
        if "word" in values and "pending_word" not in values:
            raise sqlite3.OperationalError("测试应用事务失败")
        save(values)
    monkeypatch.setattr(runtime._store, "_save_settings", fail_on_apply)
    with pytest.raises(ValueError, match="应用事务失败"):
        runtime.save_setting("word", "你好朋友")
    assert runtime._detector is original
    assert runtime._conversation._voice.gate.detector is original
    state = runtime.connect("main")["state"]
    assert state["voice_status"] == "standby" and state["wake_word"] == "你好小博"
    assert state["pending_wake_word"] == "你好朋友"
    monkeypatch.setattr(runtime._store, "_save_settings", save)
    runtime.save_setting("word", "你好朋友")
    assert runtime.connect("main")["state"]["wake_word"] == "你好朋友"


@pytest.mark.asyncio
async def test_pending_word_survives_restart_and_applies_when_host_prepares(tmp_path, monkeypatch):
    path = tmp_path / "pending.sqlite3"
    store = ConversationStore(path)
    await store.open()
    await store.save_settings({"word": "你好小博", "pending_word": "你好朋友"})
    await store.close()
    monkeypatch.setenv("VOICE_WAKE_WORD", "你好小友")
    candidate = SimpleNamespace(reset=lambda: None, feed=lambda audio, rate: False)
    monkeypatch.setattr(runtime_module, "KeywordDetector", lambda directory, word: candidate)
    runtime = Runtime(database_path=path)
    runtime.start()
    try:
        assert runtime.connect("main")["state"]["pending_wake_word"] == "你好朋友"
        assert runtime.connect("main")["state"]["wake_word"] == "你好小博"
        assert await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(runtime._apply_pending_word(), runtime._loop))
        assert runtime.connect("main")["state"]["wake_word"] == "你好朋友"
        assert runtime._settings_values == {"word": "你好朋友"}
    finally:
        runtime.stop()


def test_history_browse_and_full_text_do_not_change_working_context(app):
    messages = [{"id": str(index), "reply_id": str(index // 2), "role": "user" if index % 2 == 0 else "assistant",
                 "source": "voice" if index % 3 == 0 else "text", "status": "completed", "error": "",
                 "created_at": "2026-11-16T01:00:00+00:00", "text": ("🙂正文\n" * 3000) if index == 0 else f"消息 {index}"}
                for index in range(45)]
    records = [{"display_message_id": "0", "message": {"role": "user", "content": "模型原文"}}]
    async def seed():
        app._messages = {message["id"]: message for message in messages}
        app._model_records = deepcopy(records)
        app._window = {"display_start_position": 40, "model_start_position": 1}
        await app._store.save(messages, records, app._window)
    asyncio.run_coroutine_threadsafe(seed(), app._loop).result()
    before = app.connect("main")
    page = app.list_history()
    assert [row["id"] for row in page["messages"]] == [str(index) for index in range(44, 24, -1)]
    page2 = app.list_history(page["next_before_position"])
    page3 = app.list_history(page2["next_before_position"])
    assert page3["next_before_position"] is None
    assert len({row["id"] for row in page["messages"] + page2["messages"] + page3["messages"]}) == 45
    first = app.read_history("0")
    rest = app.read_history("0", first["next_offset"])
    assert first["message"]["text"] + rest["message"]["text"] == messages[0]["text"]
    assert rest["next_offset"] is None
    assert app.connect("main")["messages"] == before["messages"]
    assert app._model_records == records
    assert app._window == {"display_start_position": 40, "model_start_position": 1}
    assert app.connect("main")["state"]["history_status"] == before["state"]["history_status"]
