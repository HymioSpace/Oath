from pathlib import Path

import pytest

from bootstrap.command import _docker_environment, _metrics_target


def test_metrics_target_is_absolute_and_persisted_in_child_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "targets.json"
    target.write_text('[{"targets":["backend:8080"]}]\n')
    monkeypatch.chdir(tmp_path)

    resolved = _metrics_target(Path("targets.json"))
    environment = _docker_environment({"hymio_metrics_targets": resolved})

    assert resolved == str(target)
    assert environment["OATH_HYMIO_METRICS_TARGETS"] == resolved


def test_missing_metrics_target_fails_before_containers_start(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        _metrics_target(tmp_path / "missing.json")
