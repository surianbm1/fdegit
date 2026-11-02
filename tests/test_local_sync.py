import pytest

from app.local_sync import LocalSync


def test_publish_fanout_cache_conflicts_and_deletion(settings_factory, tmp_path):
    settings = {n: settings_factory(n) for n in ("cloud", "user1", "user2", "user3")}
    sync = LocalSync(settings, tmp_path / "shared")
    with sync.locked():
        sync.initialize()
    settings["user1"].project_path.joinpath("main.py").write_text("value = 2\n")
    result = sync.run("user1")
    assert result["published_files"] == 1
    for name in ("user2", "user3"):
        assert settings[name].project_path.joinpath("main.py").read_text() != "value = 2\n"
        sync.run(name, "pull")
    for config in settings.values():
        assert config.project_path.joinpath("main.py").read_text() == "value = 2\n"
    assert sync.run("user1")["published_files"] == 0
    settings["user2"].project_path.joinpath("main.py").write_text("user2 unsent\n")
    settings["user1"].project_path.joinpath("main.py").write_text("value = 3\n")
    sync.run("user1")
    assert sync.run("user2", "pull")["conflicts"] == ["main.py"]
    assert settings["user2"].project_path.joinpath("main.py").read_text() == "user2 unsent\n"
    with pytest.raises(ValueError, match="Cloud conflicts"):
        sync.run("user2")
    settings["user1"].project_path.joinpath("utils.py").unlink()
    sync.run("user1")
    assert settings["user3"].project_path.joinpath("utils.py").exists()
    sync.run("user3", "pull")
    # The conflicted user2 pull applies none of the incoming changes.
    assert sync.run("user2", "pull")["updated_files"] == 0
    assert settings["user2"].project_path.joinpath("utils.py").exists()
    assert all(
        not settings[name].project_path.joinpath("utils.py").exists()
        for name in ("cloud", "user1", "user3")
    )


def test_selected_demo_does_not_publish_other_edits(settings_factory, tmp_path):
    settings = {n: settings_factory(n) for n in ("cloud", "user1", "user2", "user3")}
    sync = LocalSync(settings, tmp_path / "shared")
    with sync.locked():
        sync.initialize()
    settings["user1"].project_path.joinpath("main.py").write_text("unsent")
    settings["user1"].project_path.joinpath("demo.txt").write_text("demo")
    sync.run("user1", paths=["demo.txt"])
    assert settings["cloud"].project_path.joinpath("main.py").read_text() != "unsent"
    assert sync.run("user1")["published_files"] == 1
