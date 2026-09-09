from tero.cli import parser
from tero.config import Config


def test_environment_window_is_used_when_cli_does_not_override(monkeypatch):
    monkeypatch.setenv("TERO_CONTEXT_TOKENS", "1000000")
    monkeypatch.setenv("TERO_OUTPUT_TOKENS", "32000")
    args = parser().parse_args([])
    config = Config.from_env(context_tokens=args.context_tokens, output_tokens=args.output_tokens)
    assert config.context_tokens == 1000000
    assert config.output_tokens == 32000


def test_explicit_window_override_wins(monkeypatch):
    monkeypatch.setenv("TERO_CONTEXT_TOKENS", "1000000")
    config = Config.from_env(context_tokens=64000)
    assert config.context_tokens == 64000
