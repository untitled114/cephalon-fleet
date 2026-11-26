def test_version():
    import amir

    assert amir.__version__ == "0.1.0"


def test_real_fixtures_present(captured, voice_clip):
    # The real feed (about 1.2 MB), not a stand-in: larger than the fetch limit tests/net exercises.
    assert len(captured("forum_pc_update_notes.rss.xml")) > 1_000_000
    assert voice_clip.stat().st_size == 98_702


def test_ffmpeg_available(ffmpeg_exe):
    import subprocess

    out = subprocess.run([ffmpeg_exe, "-hide_banner", "-version"], capture_output=True, text=True)
    assert out.returncode == 0 and "ffmpeg version" in out.stdout
