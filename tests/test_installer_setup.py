from dotenv import dotenv_values
import pytest

from voice_super_agent.desktop.setup import save_service_keys


def test_first_run_keys_can_be_saved_and_updated_without_losing_advanced_settings(tmp_path):
    config = tmp_path / ".env"
    keys = {"DEEPSEEK_API_KEY": " text-key ", "VOLCENGINE_API_KEY": "", "ARK_API_KEY": ""}
    save_service_keys(config, keys)
    assert dotenv_values(config, encoding="utf-8-sig") == {
        "DEEPSEEK_API_KEY": "text-key", "VOLCENGINE_API_KEY": "", "ARK_API_KEY": "",
    }
    with config.open("a", encoding="utf-8") as original:
        original.write("VOICE_WAKE_WORD=你好小博\n")
    keys.update(DEEPSEEK_API_KEY="replacement-key", VOLCENGINE_API_KEY="voice-key")
    save_service_keys(config, keys)
    assert dotenv_values(config, encoding="utf-8-sig") == {
        **keys, "VOICE_WAKE_WORD": "你好小博",
    }

    with pytest.raises(ValueError, match="DeepSeek"):
        save_service_keys(config, {**keys, "DEEPSEEK_API_KEY": ""})
    assert dotenv_values(config, encoding="utf-8-sig")["DEEPSEEK_API_KEY"] == "replacement-key"
