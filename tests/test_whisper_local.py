"""Local ASR limits and subprocess lifecycle; no downloads or microphone access."""
import asyncio
from array import array
from pathlib import Path
import sys
import wave

import pytest

from travel_agent.adapters.whisper_local import MAX_AUDIO_BYTES, VoiceError, WhisperTranscriber
import travel_agent.adapters.whisper_local as module


def pcm(path, *, seconds=1, amplitude=1000):
    samples = array("h", (amplitude if i % 2 else -amplitude for i in range(int(seconds * 16000))))
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        out.writeframes(samples.tobytes())


@pytest.fixture
def transcriber(tmp_path):
    model = tmp_path / "model.bin"
    model.write_bytes(b"model fixture")
    return WhisperTranscriber(model_path=model, whisper_executable=sys.executable, ffmpeg_executable=sys.executable)


def fake_pipeline(monkeypatch, transcriber, *, seconds=1, amplitude=1000, text="Хочу поехать в Ереван."):
    calls = []
    async def run(command, directory, *, decoder):
        calls.append((command, directory, decoder))
        if decoder:
            pcm(Path(command[-1]), seconds=seconds, amplitude=amplitude)
        else:
            Path(command[command.index("-of") + 1] + ".txt").write_text(text)
    monkeypatch.setattr(transcriber, "_run", run)
    return calls


def test_health_is_safe_and_requires_real_dependencies(tmp_path):
    instance = WhisperTranscriber(model_path=tmp_path / "secret-model-path", whisper_executable="/absent/secret-executable", ffmpeg_executable=sys.executable)
    health = instance.health()
    assert health["available"] is False
    assert health["engine"] == "whisper" and health["language"] == "ru"
    assert health["max_bytes"] == 8388608 and health["max_seconds"] == 60
    assert "secret" not in str(health)


@pytest.mark.parametrize("data,mime,code,status", [
    (b"", "audio/webm", "voice_empty", 422),
    (b"a" * (MAX_AUDIO_BYTES + 1), "audio/webm", "voice_too_large", 413),
    (b"#EXTM3U\nhttps://example.org/private", "application/vnd.apple.mpegurl", "voice_unsupported_format", 415),
])
async def test_invalid_uploads_never_spawn(transcriber, monkeypatch, data, mime, code, status):
    async def forbidden(*args, **kwargs):
        pytest.fail("invalid input reached executable")
    monkeypatch.setattr(transcriber, "_run", forbidden)
    with pytest.raises(VoiceError) as failure:
        await transcriber.transcribe(data, mime)
    assert (failure.value.code, failure.value.http_status) == (code, status)


@pytest.mark.parametrize("mime,demuxer", [("audio/webm;codecs=opus", "matroska"), ("audio/ogg", "ogg"), ("audio/mp4", "mov"), ("audio/wav", "wav")])
async def test_browser_formats_are_forced_local_and_result_is_russian(transcriber, monkeypatch, mime, demuxer):
    calls = fake_pipeline(monkeypatch, transcriber, text="  Хочу в Ереван.\nНа неделю. ")
    result = await transcriber.transcribe(b"opaque upload", mime)
    assert result == {"text": "Хочу в Ереван. На неделю.", "language": "ru", "engine": "whisper"}
    ffmpeg, whisper = calls[0][0], calls[1][0]
    assert ffmpeg[ffmpeg.index("-protocol_whitelist") + 1] == "file"
    assert ffmpeg[ffmpeg.index("-format_whitelist") + 1] == demuxer
    assert ffmpeg[ffmpeg.index("-f") + 1] == demuxer
    assert "-nostdin" in ffmpeg and ffmpeg[ffmpeg.index("-t") + 1] == "60.1"
    if demuxer == "mov":
        assert ffmpeg[ffmpeg.index("-enable_drefs") + 1] == "0"
    assert whisper[whisper.index("-l") + 1] == "ru"
    assert all(not directory.exists() for _, directory, _ in calls)


@pytest.mark.parametrize("seconds,amplitude,code", [(60.1, 1000, "voice_too_long"), (1, 0, "voice_empty"), (1, 30, "voice_empty"), (0.05, 1000, "voice_empty")])
async def test_duration_and_silence_are_rejected_before_whisper(transcriber, monkeypatch, seconds, amplitude, code):
    calls = fake_pipeline(monkeypatch, transcriber, seconds=seconds, amplitude=amplitude)
    with pytest.raises(VoiceError, match=".") as failure:
        await transcriber.transcribe(b"audio", "audio/wav")
    assert failure.value.code == code and len(calls) == 1
    assert not calls[0][1].exists() and not transcriber._lock.locked()


@pytest.mark.parametrize("text", ["", "  ", "[BLANK_AUDIO]", "[Музыка]", "..."])
async def test_empty_transcripts_are_not_sent_as_user_text(transcriber, monkeypatch, text):
    calls = fake_pipeline(monkeypatch, transcriber, text=text)
    with pytest.raises(VoiceError) as failure:
        await transcriber.transcribe(b"audio", "audio/wav")
    assert failure.value.code == "voice_empty"
    assert not calls[0][1].exists()


async def test_second_recording_gets_busy_error(transcriber, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    async def run(command, directory, *, decoder):
        if decoder:
            entered.set()
            await release.wait()
            pcm(Path(command[-1]))
        else:
            Path(command[command.index("-of") + 1] + ".txt").write_text("Хочу в Ереван")
    monkeypatch.setattr(transcriber, "_run", run)
    first = asyncio.create_task(transcriber.transcribe(b"audio", "audio/wav"))
    await entered.wait()
    with pytest.raises(VoiceError) as failure:
        await transcriber.transcribe(b"audio", "audio/wav")
    assert failure.value.code == "voice_busy" and failure.value.http_status == 409
    release.set()
    await first
    assert not transcriber._lock.locked()


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_kill_wait_and_cleanup(transcriber, monkeypatch, cancel):
    started, killed = asyncio.Event(), asyncio.Event()
    seen = {}
    class Process:
        pid = 987654321
        returncode = None
        async def wait(self):
            started.set()
            await killed.wait()
            self.returncode = -9
            seen["waited"] = True
            return -9
    process = Process()
    async def create(*args, **kwargs):
        seen["directory"] = kwargs["cwd"]
        return process
    def kill(pid, sig):
        assert pid == process.pid
        seen["killed"] = True
        killed.set()
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(module.os, "killpg", kill)
    transcriber.timeout_seconds = 1 if cancel else 0.01
    task = asyncio.create_task(transcriber.transcribe(b"audio", "audio/wav"))
    await started.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(VoiceError) as failure:
            await task
        assert failure.value.code == "voice_timeout" and failure.value.http_status == 504
    assert seen["killed"] and seen["waited"]
    assert not seen["directory"].exists() and not transcriber._lock.locked()


async def test_subprocess_environment_has_no_inherited_secrets(transcriber, monkeypatch, tmp_path):
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", "secret-key")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-key")
    monkeypatch.setenv("HTTPS_PROXY", "https://secret-proxy.invalid")
    async def create(*args, **kwargs):
        assert "secret" not in str(kwargs)
        assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
        assert kwargs["stdout"] == kwargs["stderr"] == asyncio.subprocess.DEVNULL
        assert kwargs["start_new_session"] is True
        class Process:
            async def wait(self):
                return 0
        return Process()
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", create)
    await transcriber._run([sys.executable], tmp_path, decoder=True)


@pytest.mark.parametrize("decoder,code", [(True, "voice_invalid_audio"), (False, "voice_failed")])
async def test_executable_failure_does_not_expose_logs(transcriber, monkeypatch, tmp_path, decoder, code):
    async def create(*args, **kwargs):
        class Process:
            async def wait(self):
                return 1
        return Process()
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", create)
    with pytest.raises(VoiceError) as failure:
        await transcriber._run([sys.executable], tmp_path, decoder=decoder)
    assert failure.value.code == code


async def test_spawn_failure_is_safe(transcriber, monkeypatch, tmp_path):
    async def create(*args, **kwargs):
        raise OSError("secret executable path")
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", create)
    with pytest.raises(VoiceError) as failure:
        await transcriber._run([sys.executable], tmp_path, decoder=True)
    assert failure.value.code == "voice_unavailable" and "secret" not in str(failure.value)


async def test_cancellation_during_spawn_still_reaps_child(transcriber, monkeypatch, tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    seen = {}
    class Process:
        pid = 987654321
        returncode = None
        async def wait(self):
            self.returncode = -9
            seen["waited"] = True
            return -9
    async def create(*args, **kwargs):
        started.set()
        await release.wait()
        return Process()
    def kill(pid, sig):
        assert pid == Process.pid
        seen["killed"] = True
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(module.os, "killpg", kill)
    task = asyncio.create_task(transcriber._run([sys.executable], tmp_path, decoder=True))
    await started.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen == {"killed": True, "waited": True}
