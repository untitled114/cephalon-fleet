import asyncio
import logging
import os
import random
import shutil
import sys
from pathlib import Path

import pytest

from amir.voice.greetings import AUDIO_EXTENSIONS, GreetingLibrary, validate_clip

AUDIO_README = Path(__file__).parent.parent / "fixtures" / "audio" / "README.md"


def add_copies(directory: Path, clip: Path, names: list[str]) -> list[Path]:
    paths = []
    for name in names:
        target = directory / name
        shutil.copyfile(clip, target)
        paths.append(target)
    return paths


def test_extensions_include_owner_format():
    assert ".qt" in AUDIO_EXTENSIONS
    assert ".md" not in AUDIO_EXTENSIONS


async def test_real_clip_valid(ffmpeg_exe, voice_clip):
    assert await validate_clip(ffmpeg_exe, voice_clip) is True


async def test_truncated_copy_invalid(ffmpeg_exe, voice_clip, tmp_path):
    broken = tmp_path / "broken.qt"
    broken.write_bytes(voice_clip.read_bytes()[:4000])
    assert await validate_clip(ffmpeg_exe, broken) is False


async def test_non_audio_invalid(ffmpeg_exe, tmp_path):
    fake = tmp_path / "fake.mp3"
    shutil.copyfile(AUDIO_README, fake)
    assert await validate_clip(ffmpeg_exe, fake) is False


async def test_missing_ffmpeg_binary_is_invalid(voice_clip, tmp_path):
    assert await validate_clip(str(tmp_path / "no-such-ffmpeg"), voice_clip) is False


async def test_timeout_returns_false(ffmpeg_exe, voice_clip):
    assert await validate_clip(ffmpeg_exe, voice_clip, timeout=0.0001) is False


async def test_failure_logs_name_only(ffmpeg_exe, tmp_path, caplog):
    fake = tmp_path / "fake.mp3"
    shutil.copyfile(AUDIO_README, fake)
    with caplog.at_level(logging.DEBUG):
        await validate_clip(ffmpeg_exe, fake)
    assert "fake.mp3" in caplog.text
    assert str(tmp_path) not in caplog.text


async def test_empty_directory_warns(ffmpeg_exe, tmp_path, caplog):
    library = GreetingLibrary(tmp_path, ffmpeg_exe)
    with caplog.at_level(logging.WARNING):
        report = await library.scan()
    assert report.valid == [] and report.invalid == []
    assert library.valid_count == 0
    assert library.pick() is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)


async def test_missing_directory_warns(ffmpeg_exe, tmp_path, caplog):
    library = GreetingLibrary(tmp_path / "absent", ffmpeg_exe)
    with caplog.at_level(logging.WARNING):
        await library.scan()
    assert library.pick() is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)


async def test_scan_classifies_and_ignores_non_audio(ffmpeg_exe, voice_clip, tmp_path):
    (good,) = add_copies(tmp_path, voice_clip, ["amir-greeting-01.qt"])
    broken = tmp_path / "amir-greeting-02.qt"
    broken.write_bytes(voice_clip.read_bytes()[:4000])
    shutil.copyfile(AUDIO_README, tmp_path / "README.md")
    library = GreetingLibrary(tmp_path, ffmpeg_exe)
    report = await library.scan()
    assert report.valid == [good.resolve()]
    assert report.invalid == [broken.resolve()]
    assert library.valid_count == 1


async def test_single_clip_repeats(ffmpeg_exe, voice_clip, tmp_path):
    add_copies(tmp_path, voice_clip, ["amir-greeting-01.qt"])
    library = GreetingLibrary(tmp_path, ffmpeg_exe, rng=random.Random(1))
    await library.scan()
    picks = {library.pick() for _ in range(5)}
    assert len(picks) == 1
    assert None not in picks


async def test_no_back_to_back_repeat(ffmpeg_exe, voice_clip, tmp_path):
    add_copies(tmp_path, voice_clip, ["a-01.qt", "a-02.qt", "a-03.qt"])
    library = GreetingLibrary(tmp_path, ffmpeg_exe, rng=random.Random(7))
    await library.scan()
    assert library.valid_count == 3
    picks = [library.pick() for _ in range(50)]
    assert all(p is not None for p in picks)
    assert all(a != b for a, b in zip(picks, picks[1:], strict=False))
    assert len(set(picks)) == 3


async def test_mark_bad_excludes(ffmpeg_exe, voice_clip, tmp_path):
    first, second = add_copies(tmp_path, voice_clip, ["a-01.qt", "a-02.qt"])
    library = GreetingLibrary(tmp_path, ffmpeg_exe, rng=random.Random(3))
    await library.scan()
    library.mark_bad(first.resolve())
    assert library.valid_count == 1
    assert {library.pick() for _ in range(10)} == {second.resolve()}
    library.mark_bad(second.resolve())
    assert library.valid_count == 0
    assert library.pick() is None


async def test_unicode_filename(ffmpeg_exe, voice_clip, tmp_path):
    (clip,) = add_copies(tmp_path, voice_clip, ["Grüße \U0001f44b.qt"])
    library = GreetingLibrary(tmp_path, ffmpeg_exe)
    await library.scan()
    assert library.valid_count == 1
    assert library.pick() == clip.resolve()


async def test_symlink_outside_directory_skipped(ffmpeg_exe, voice_clip, tmp_path, caplog):
    library_dir = tmp_path / "greetings"
    library_dir.mkdir()
    (library_dir / "escape.qt").symlink_to(voice_clip)
    library = GreetingLibrary(library_dir, ffmpeg_exe)
    with caplog.at_level(logging.WARNING):
        report = await library.scan()
    assert report.valid == [] and report.invalid == []
    assert library.pick() is None
    assert "escape.qt" in caplog.text


async def test_symlink_inside_directory_allowed(ffmpeg_exe, voice_clip, tmp_path):
    (real,) = add_copies(tmp_path, voice_clip, ["real.qt"])
    (tmp_path / "alias.qt").symlink_to(real)
    library = GreetingLibrary(tmp_path, ffmpeg_exe)
    report = await library.scan()
    assert real.resolve() in report.valid


async def test_rescan_clears_bad_marks(ffmpeg_exe, voice_clip, tmp_path):
    (clip,) = add_copies(tmp_path, voice_clip, ["a-01.qt"])
    library = GreetingLibrary(tmp_path, ffmpeg_exe)
    await library.scan()
    library.mark_bad(clip.resolve())
    assert library.pick() is None
    await library.scan()
    assert library.pick() == clip.resolve()


@pytest.mark.parametrize("ext", sorted(AUDIO_EXTENSIONS))
def test_extensions_are_lowercase_dotted(ext):
    assert ext.startswith(".") and ext == ext.lower()


def fake_ffmpeg(directory: Path, body: str) -> str:
    script = directory / "fake-ffmpeg"
    script.write_text(f"#!{sys.executable}\nimport sys, time, os\n{body}\n")
    script.chmod(0o755)
    return str(script)


async def test_failure_log_redacts_full_path(voice_clip, tmp_path, caplog):
    clip_dir = tmp_path / "secret-dir"
    clip_dir.mkdir()
    clip = clip_dir / "leaky.mp3"
    shutil.copyfile(voice_clip, clip)
    ffmpeg = fake_ffmpeg(
        tmp_path,
        "path = sys.argv[sys.argv.index('-i') + 1]\n"
        "print(path + ': Invalid data found when processing input', file=sys.stderr)\n"
        "sys.exit(1)",
    )
    with caplog.at_level(logging.DEBUG):
        assert await validate_clip(ffmpeg, clip) is False
    assert "leaky.mp3" in caplog.text
    assert str(tmp_path) not in caplog.text
    assert "secret-dir" not in caplog.text
    assert "Invalid data found" in caplog.text


async def test_cancel_kills_child(voice_clip, tmp_path):
    pidfile = tmp_path / "pid"
    ffmpeg = fake_ffmpeg(
        tmp_path, f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)"
    )
    task = asyncio.create_task(validate_clip(ffmpeg, voice_clip))
    for _ in range(200):
        if pidfile.exists() and pidfile.read_text():
            break
        await asyncio.sleep(0.01)
    pid = int(pidfile.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_timeout_kills_child(voice_clip, tmp_path):
    pidfile = tmp_path / "pid"
    ffmpeg = fake_ffmpeg(
        tmp_path, f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)"
    )
    assert await validate_clip(ffmpeg, voice_clip, timeout=0.5) is False
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


async def test_symlink_loop_skipped(ffmpeg_exe, voice_clip, tmp_path, caplog):
    (tmp_path / "loop-a.qt").symlink_to(tmp_path / "loop-b.qt")
    (tmp_path / "loop-b.qt").symlink_to(tmp_path / "loop-a.qt")
    (good,) = add_copies(tmp_path, voice_clip, ["good.qt"])
    library = GreetingLibrary(tmp_path, ffmpeg_exe)
    with caplog.at_level(logging.WARNING):
        report = await library.scan()
    assert report.valid == [good.resolve()]
    assert "loop-a.qt" in caplog.text
    assert str(tmp_path) not in caplog.text


async def test_iterdir_oserror_gives_empty_result(ffmpeg_exe, tmp_path, monkeypatch, caplog):
    def boom(self):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "iterdir", boom)
    library = GreetingLibrary(tmp_path, ffmpeg_exe)
    with caplog.at_level(logging.WARNING):
        report = await library.scan()
    assert report.valid == [] and report.invalid == []
    assert library.pick() is None
    assert str(tmp_path) not in caplog.text


async def test_mark_bad_with_three_clips_no_repeat(ffmpeg_exe, voice_clip, tmp_path):
    paths = add_copies(tmp_path, voice_clip, ["a-01.qt", "a-02.qt", "a-03.qt"])
    library = GreetingLibrary(tmp_path, ffmpeg_exe, rng=random.Random(11))
    await library.scan()
    bad = paths[1].resolve()
    library.mark_bad(bad)
    picks = [library.pick() for _ in range(50)]
    assert bad not in picks
    assert None not in picks
    assert all(a != b for a, b in zip(picks, picks[1:], strict=False))
