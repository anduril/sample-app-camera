"""StateStore: JSON persistence with atomic writes and tolerant loading."""

import json

from lattice_cam.state import StateStore


def test_roundtrip_persists_across_instances(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(str(path))
    assert store.get("ingress") is None

    store.set("ingress", {"video_id": "abc", "push_url": "srt://x"})
    store.set("created_time", "2026-09-16T00:00:00+00:00")

    reloaded = StateStore(str(path))
    assert reloaded.get("ingress") == {"video_id": "abc", "push_url": "srt://x"}
    assert reloaded.get("created_time") == "2026-09-16T00:00:00+00:00"


def test_delete_removes_key_and_rewrites_file(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(str(path))
    store.set("ingress", {"video_id": "abc"})
    store.delete("ingress")
    store.delete("missing")  # no-op

    assert store.get("ingress") is None
    assert json.loads(path.read_text()) == {}
    assert not (tmp_path / "state.json.tmp").exists()


def test_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "dir" / "state.json"
    StateStore(str(path)).set("k", 1)
    assert json.loads(path.read_text()) == {"k": 1}


def test_corrupt_file_is_treated_as_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    store = StateStore(str(path))
    assert store.get("anything") is None
    store.set("k", "v")
    assert json.loads(path.read_text()) == {"k": "v"}


def test_non_object_file_is_treated_as_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("[1, 2, 3]")
    assert StateStore(str(path)).get("k") is None


def test_empty_path_is_in_memory_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = StateStore("")
    store.set("k", 1)
    assert store.get("k") == 1
    assert list(tmp_path.iterdir()) == []


def test_unwritable_path_keeps_state_in_memory(tmp_path, caplog):
    # The parent is a regular file, so the directory can never be created and
    # every flush fails with an OSError. The daemon must keep running.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    store = StateStore(str(blocker / "state.json"))

    with caplog.at_level("WARNING"):
        store.set("ingress", {"video_id": "abc"})
        store.set("created_time", "t")
        store.delete("ingress")

    assert store.get("created_time") == "t"
    assert store.get("ingress") is None
    warnings = [r for r in caplog.records if "state file could not be written" in r.getMessage()]
    assert len(warnings) == 1  # same error repeated: warned once


def test_flush_error_is_logged_again_when_it_changes(tmp_path, caplog, monkeypatch):
    store = StateStore(str(tmp_path / "state.json"))
    errors = iter([OSError("disk full"), OSError("disk full"), OSError("read-only")])

    def failing_replace(src, dst):
        raise next(errors)

    monkeypatch.setattr("lattice_cam.state.os.replace", failing_replace)
    with caplog.at_level("WARNING"):
        store.set("a", 1)
        store.set("b", 2)
        store.set("c", 3)
    messages = [r.getMessage() for r in caplog.records]
    assert messages.count("state file could not be written; keeping state in memory only") == 2
    assert store.get("c") == 3
