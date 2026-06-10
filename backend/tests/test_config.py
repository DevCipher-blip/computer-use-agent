from pathlib import Path

from config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


def test_env_example_parses_into_settings(monkeypatch):
    values = {}
    for line in ENV_EXAMPLE.read_text().splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key] = value
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    s = Settings(_env_file=None)
    known = {name.upper() for name in Settings.model_fields}
    # Every documented variable except Compose's own is a real setting.
    assert set(values) - known == {"POSTGRES_PASSWORD"}
    assert s.max_concurrent_sessions == 20
    assert s.session_cpus == 1.0
    assert s.cors_origins == []
