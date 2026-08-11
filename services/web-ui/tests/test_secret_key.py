"""The session key must be unique per installation and stable across
restarts — a shipped constant would let anyone forge a login cookie."""

from pathlib import Path

from webui_service.secret import resolve_secret_key


def test_generates_and_persists_a_key(tmp_path, monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    first = resolve_secret_key(str(tmp_path))
    assert len(first) >= 32
    assert (tmp_path / "session_secret").read_text().strip() == first


def test_same_key_on_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    first = resolve_secret_key(str(tmp_path))
    second = resolve_secret_key(str(tmp_path))
    assert first == second  # sessions survive a container restart


def test_different_installations_get_different_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    one = resolve_secret_key(str(tmp_path / "a"))
    two = resolve_secret_key(str(tmp_path / "b"))
    assert one != two


def test_environment_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "operator-managed-key")
    assert resolve_secret_key(str(tmp_path)) == "operator-managed-key"
    assert not (tmp_path / "session_secret").exists()


def test_blank_environment_value_is_ignored(tmp_path, monkeypatch):
    """compose passes SECRET_KEY through as an empty string by default."""
    monkeypatch.setenv("SECRET_KEY", "")
    key = resolve_secret_key(str(tmp_path))
    assert key and key != ""
    assert (tmp_path / "session_secret").exists()


def test_unwritable_dir_falls_back_to_ephemeral_key(tmp_path, monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("this is a file, not a directory")
    key = resolve_secret_key(str(blocker / "webui"))
    assert len(key) >= 32  # degraded but never insecure


def test_key_file_is_not_world_readable(tmp_path, monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    resolve_secret_key(str(tmp_path))
    mode = Path(tmp_path / "session_secret").stat().st_mode & 0o777
    assert mode == 0o600
