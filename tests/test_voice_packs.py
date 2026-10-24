import json
import os
import time
import zipfile

from vision_backend.voice_packs import VoicePackManager

KEYS = ["approaching", "lingering", "unattended_motion"]


def make_zip(path, files: dict[str, bytes]):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)


def test_zip_with_wrapper_folder_is_indexed(tmp_path):
    make_zip(tmp_path / "pack.zip", {
        "my_pack/approaching/a.wav": b"RIFF",
        "my_pack/approaching/b.mp3": b"ID3",
        "my_pack/lingering/c.wav": b"RIFF",
        "my_pack/lingering/notes.txt": b"ignored",
    })
    mgr = VoicePackManager(tmp_path, KEYS)
    assert mgr.rescan()
    assert [c.path.name for c in mgr.index["approaching"]] == ["a.wav", "b.mp3"]
    assert [c.path.name for c in mgr.index["lingering"]] == ["c.wav"]
    assert "unattended_motion" not in mgr.index
    assert mgr.pick("unattended_motion") is None


def test_round_robin_selection(tmp_path):
    make_zip(tmp_path / "pack.zip", {"approaching/a.wav": b"1", "approaching/b.wav": b"2"})
    mgr = VoicePackManager(tmp_path, KEYS, selection="round_robin")
    mgr.rescan()
    assert [mgr.pick("approaching").path.name for _ in range(3)] == ["a.wav", "b.wav", "a.wav"]


def test_manifest_overrides_folders(tmp_path):
    make_zip(tmp_path / "pack.zip", {
        "manifest.json": json.dumps({"scenarios": {"lingering": ["clips/x.wav"]}}).encode(),
        "clips/x.wav": b"RIFF",
    })
    mgr = VoicePackManager(tmp_path, KEYS)
    mgr.rescan()
    assert mgr.pick("lingering").path.name == "x.wav"


def test_newest_pack_wins_and_rescan_detects_changes(tmp_path):
    make_zip(tmp_path / "old.zip", {"approaching/old.wav": b"1", "lingering/old.wav": b"1"})
    past = time.time() - 100
    os.utime(tmp_path / "old.zip", (past, past))
    mgr = VoicePackManager(tmp_path, KEYS)
    mgr.rescan()
    assert mgr.pick("approaching").pack == "old.zip"
    assert not mgr.rescan()  # nothing changed

    make_zip(tmp_path / "new.zip", {"approaching/new.wav": b"2"})
    assert mgr.rescan()
    assert mgr.pick("approaching").pack == "new.zip"
    assert mgr.pick("lingering").pack == "old.zip"  # falls back to older pack


def test_plain_folder_pack(tmp_path):
    (tmp_path / "folder_pack" / "unattended_motion").mkdir(parents=True)
    (tmp_path / "folder_pack" / "unattended_motion" / "hey.ogg").write_bytes(b"OggS")
    mgr = VoicePackManager(tmp_path, KEYS)
    mgr.rescan()
    assert mgr.pick("unattended_motion").path.name == "hey.ogg"


def test_zip_slip_and_corrupt_zips_are_skipped(tmp_path):
    make_zip(tmp_path / "evil.zip", {"../escape.wav": b"x"})
    (tmp_path / "partial.zip").write_bytes(b"PK\x03\x04 not really a zip")
    make_zip(tmp_path / "good.zip", {"approaching/a.wav": b"1"})
    mgr = VoicePackManager(tmp_path, KEYS)
    mgr.rescan()
    assert not (tmp_path / "escape.wav").exists()
    assert not (tmp_path.parent / "escape.wav").exists()
    assert mgr.pick("approaching").pack == "good.zip"
