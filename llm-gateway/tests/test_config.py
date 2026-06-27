"""Tests for app/config.py — config loading and validation."""
from pathlib import Path

import pytest
import yaml

from app.config import load_config


def _write_config(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.dump(data))
    return p


class TestLoadConfig:
    def test_returns_empty_list_when_file_missing(self, tmp_path):
        sources = load_config(tmp_path / "nonexistent.yaml")
        assert sources == []

    def test_returns_empty_list_for_empty_sources_key(self, tmp_path):
        p = _write_config(tmp_path, {"sources": []})
        assert load_config(p) == []

    def test_loads_basic_source(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_API_KEY", "sk-abc")
        p = _write_config(tmp_path, {"sources": [{
            "name": "test-src",
            "base_url": "https://api.example.com/v1",
            "model": "my-model",
            "api_key_env": "MY_API_KEY",
            "rpm": 10,
            "rpd": 100,
            "priority": 1,
            "enabled": True,
        }]})
        sources = load_config(p)
        assert len(sources) == 1
        s = sources[0]
        assert s.name == "test-src"
        assert s.model == "my-model"
        assert s.api_key == "sk-abc"
        assert s.rpm == 10
        assert s.rpd == 100
        assert s.priority == 1
        assert s.enabled is True

    def test_trailing_slash_stripped_from_base_url(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KEY", "x")
        p = _write_config(tmp_path, {"sources": [{
            "name": "s", "base_url": "https://api.example.com/v1/",
            "model": "m", "api_key_env": "KEY",
            "priority": 1, "enabled": True,
        }]})
        sources = load_config(p)
        assert not sources[0].base_url.endswith("/")

    def test_missing_env_var_disables_source(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MISSING_KEY", raising=False)
        p = _write_config(tmp_path, {"sources": [{
            "name": "s", "base_url": "https://api.example.com/v1",
            "model": "m", "api_key_env": "MISSING_KEY",
            "priority": 1, "enabled": True,
        }]})
        sources = load_config(p)
        assert len(sources) == 1
        assert sources[0].enabled is False

    def test_missing_env_var_does_not_raise(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MISSING_KEY", raising=False)
        p = _write_config(tmp_path, {"sources": [
            {"name": "bad", "base_url": "https://x.com/v1", "model": "m",
             "api_key_env": "MISSING_KEY", "priority": 1, "enabled": True},
            {"name": "good", "base_url": "https://y.com/v1", "model": "m",
             "api_key_env": "", "priority": 2, "enabled": True},
        ]})
        sources = load_config(p)  # must not raise
        assert len(sources) == 2
        assert sources[0].enabled is False
        assert sources[1].enabled is True

    def test_empty_api_key_env_allows_keyless_source(self, tmp_path):
        """Local Ollama has no api_key_env — it must remain enabled."""
        p = _write_config(tmp_path, {"sources": [{
            "name": "ollama", "base_url": "http://localhost:11434/v1",
            "model": "devstral", "api_key_env": "",
            "rpm": None, "rpd": None, "priority": 99, "enabled": True,
        }]})
        sources = load_config(p)
        assert sources[0].enabled is True
        assert sources[0].api_key == ""

    def test_sources_returned_unsorted_priority_is_preserved(self, tmp_path, monkeypatch):
        """load_config returns sources in file order; SourceRegistry sorts them."""
        monkeypatch.setenv("K1", "a")
        monkeypatch.setenv("K2", "b")
        p = _write_config(tmp_path, {"sources": [
            {"name": "high", "base_url": "https://x.com/v1", "model": "m",
             "api_key_env": "K1", "priority": 10, "enabled": True},
            {"name": "low", "base_url": "https://y.com/v1", "model": "m",
             "api_key_env": "K2", "priority": 1, "enabled": True},
        ]})
        sources = load_config(p)
        # load_config preserves file order; sorting is SourceRegistry's job
        assert sources[0].name == "high"
        assert sources[1].name == "low"

    def test_enabled_false_in_config_is_respected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KEY", "x")
        p = _write_config(tmp_path, {"sources": [{
            "name": "s", "base_url": "https://x.com/v1", "model": "m",
            "api_key_env": "KEY", "priority": 1, "enabled": False,
        }]})
        sources = load_config(p)
        assert sources[0].enabled is False
