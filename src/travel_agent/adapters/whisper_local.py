"""Local Russian speech recognition with bounded decoding and no audio uploads."""
from __future__ import annotations

import asyncio
import math
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import tempfile
import wave
from array import array

MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_AUDIO_SECONDS = 60
_SAMPLE_RATE = 16000
_FORMATS = {
    "audio/webm": "matroska", "video/webm": "matroska",
    "audio/ogg": "ogg", "application/ogg": "ogg",
    "audio/mp4": "mov", "video/mp4": "mov", "audio/x-m4a": "mov",
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/aac": "aac",
    "audio/wav": "wav", "audio/wave": "wav", "audio/x-wav": "wav",
    "audio/aiff": "aiff", "audio/x-aiff": "aiff",
}

SUPPORTED_AUDIO_TYPES = frozenset(_FORMATS)


class VoiceError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 422):
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)


def _executable(value: str | Path | None, name: str) -> str | None:
    resolved = shutil.which(str(value) if value is not None else name)
    return str(Path(resolved).resolve()) if resolved else None


def _check_pcm(path: Path) -> None:
    try:
        with wave.open(str(path), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, _SAMPLE_RATE):
                raise ValueError("Unexpected decoded format")
            frames = audio.getnframes()
            if frames > MAX_AUDIO_SECONDS * _SAMPLE_RATE:
                raise VoiceError("voice_too_long", "Запись длиннее 60 секунд. Запишите сообщение короче.", 413)
            if frames < _SAMPLE_RATE // 10:
                raise VoiceError("voice_empty", "В записи не слышно речи. Попробуйте записать сообщение ещё раз.")
            pcm = array("h", audio.readframes(frames))
            if sys.byteorder != "little":
                pcm.byteswap()
    except VoiceError:
        raise
    except (OSError, EOFError, wave.Error, ValueError):
        raise VoiceError("voice_invalid_audio", "Не удалось прочитать запись. Попробуйте записать её ещё раз.") from None
    rms = math.sqrt(sum(sample * sample for sample in pcm) / max(1, len(pcm)))
    if not pcm or max(abs(sample) for sample in pcm) < 200 or rms < 50:
        raise VoiceError("voice_empty", "В записи не слышно речи. Говорите ближе к микрофону и попробуйте ещё раз.")


async def _stop_process(process) -> None:
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await asyncio.shield(process.wait())


class WhisperTranscriber:
    def __init__(self, model_path: str | Path | None = None, *,
                 whisper_executable: str | Path | None = None,
                 ffmpeg_executable: str | Path | None = None,
                 timeout_seconds: float = 60):
        self.model_path = Path(model_path or os.getenv("WHISPER_MODEL_PATH") or Path.cwd() / "data/models/whisper/ggml-small.bin").expanduser().resolve()
        self.whisper_executable = _executable(whisper_executable, "whisper-cli")
        self.ffmpeg_executable = _executable(ffmpeg_executable, "ffmpeg")
        self.timeout_seconds = max(0.01, min(float(timeout_seconds), 60.0))
        self._lock = asyncio.Lock()

    def health(self) -> dict:
        available = bool(self.whisper_executable and self.ffmpeg_executable and self.model_path.is_file() and os.access(self.model_path, os.R_OK))
        result = {"available": available, "engine": "whisper", "language": "ru",
                  "max_seconds": MAX_AUDIO_SECONDS, "max_bytes": MAX_AUDIO_BYTES}
        if not available:
            result["message"] = "Голосовой ввод пока недоступен: нужна локальная модель Whisper и программы для обработки звука."
        return result

    async def _run(self, command: list[str], directory: Path, *, decoder: bool) -> None:
        # An allowlist prevents inherited API credentials, proxies, and plugin
        # configuration from reaching either local executable.
        environment = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                       "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TMPDIR": str(directory)}
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                *command, cwd=directory, env=environment, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            ))
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            # Cancellation during spawn must still obtain and reap the child.
            try:
                process = await spawn
            except OSError:
                pass
            else:
                await _stop_process(process)
            raise
        except OSError:
            raise VoiceError("voice_unavailable", "Не удалось запустить локальное распознавание речи.", 503) from None
        try:
            return_code = await process.wait()
        except BaseException:
            await _stop_process(process)
            raise
        if return_code:
            if decoder:
                raise VoiceError("voice_invalid_audio", "Не удалось прочитать запись. Поддерживаются WebM, Ogg, MP4, MP3 и WAV.")
            raise VoiceError("voice_failed", "Не удалось распознать речь. Попробуйте ещё раз.", 500)

    async def transcribe(self, audio: bytes, content_type: str) -> dict:
        if not isinstance(audio, bytes) or not audio:
            raise VoiceError("voice_empty", "Запись пустая. Запишите голосовое сообщение.")
        if len(audio) > MAX_AUDIO_BYTES:
            raise VoiceError("voice_too_large", "Запись слишком большая. Максимальный размер — 8 МБ.", 413)
        mime = content_type.split(";", 1)[0].strip().lower() if isinstance(content_type, str) else ""
        input_format = _FORMATS.get(mime)
        if input_format is None:
            raise VoiceError("voice_unsupported_format", "Этот формат записи не поддерживается. Используйте WebM, Ogg, MP4, MP3 или WAV.", 415)
        if not self.health()["available"]:
            raise VoiceError("voice_unavailable", self.health()["message"], 503)
        if self._lock.locked():
            raise VoiceError("voice_busy", "Предыдущая запись ещё распознаётся. Подождите немного.", 409)
        async with self._lock:
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    with tempfile.TemporaryDirectory(prefix="travel-voice-") as temp:
                        directory = Path(temp)
                        source, decoded = directory / "recording", directory / "audio.wav"
                        transcript = directory / "transcript"
                        source.write_bytes(audio)
                        # Force a single demuxer: playlists and network inputs
                        # cannot be selected from untrusted uploaded bytes.
                        command = [self.ffmpeg_executable, "-nostdin", "-hide_banner", "-loglevel", "error",
                                   "-protocol_whitelist", "file", "-format_whitelist", input_format,
                                   "-f", input_format]
                        if input_format == "mov":
                            command += ["-enable_drefs", "0"]
                        command += ["-i", str(source), "-map", "0:a:0", "-vn", "-sn", "-dn",
                                    "-t", "60.1", "-ar", str(_SAMPLE_RATE), "-ac", "1",
                                    "-c:a", "pcm_s16le", "-f", "wav", "-y", str(decoded)]
                        await self._run(command, directory, decoder=True)
                        _check_pcm(decoded)
                        await self._run([self.whisper_executable, "-m", str(self.model_path), "-f", str(decoded),
                                         "-l", "ru", "-t", "4", "-otxt", "-of", str(transcript),
                                         "-nt", "-np", "-sns"], directory, decoder=False)
                        output = transcript.with_suffix(".txt")
                        if not output.is_file() or output.stat().st_size > 64 * 1024:
                            raise VoiceError("voice_failed", "Распознавание не вернуло текст. Попробуйте ещё раз.", 500)
                        text = re.sub(r"\s+", " ", output.read_text(encoding="utf-8")).strip()
                        if not text or not re.search(r"[\w]", text) or text.casefold() in {
                            "[blank_audio]", "[silence]", "[music]", "[музыка]", "(музыка)", "(тишина)", "[тишина]",
                        }:
                            raise VoiceError("voice_empty", "В записи не удалось разобрать речь. Попробуйте ещё раз.")
                        return {"text": text, "language": "ru", "engine": "whisper"}
            except TimeoutError:
                raise VoiceError("voice_timeout", "Распознавание заняло слишком много времени. Попробуйте запись короче.", 504) from None
            except (OSError, UnicodeError):
                raise VoiceError("voice_failed", "Не удалось обработать запись. Попробуйте ещё раз.", 500) from None
