"""
VideoFactory - voice (narration) provider and audio tools.

VoiceProvider is the general shape every narration service must follow, so
other voices (including local open-source ones) can be added later.
GeminiVoice uses Google's Gemini text-to-speech models on the free tier.

The whole script is narrated in ONE request (saves free quota). FFmpeg then:
  1. trims silence at the start and end,
  2. evens out the loudness (about -16 LUFS, the usual level for phone video),
  3. finds the natural pauses between sentences, so each scene's picture
     changes exactly when its sentences are spoken.
"""
import os
import base64
import json
import re
import socket
import subprocess
import urllib.error
import urllib.request
import wave
from pathlib import Path

from .config import GEMINI_API_KEY, GEMINI_TTS_MODELS
from .db import get_db

BASE_DIR = Path(__file__).resolve().parent.parent
FFMPEG = BASE_DIR / "bin" / ("ffmpeg" + (".exe" if os.name == "nt" else ""))
FFPROBE = BASE_DIR / "bin" / ("ffprobe" + (".exe" if os.name == "nt" else ""))
TTS_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Gemini's built-in narrator voices (name: character). Suited to documentary narration.
VOICES = {
    "Charon": "informative",
    "Sadaltager": "knowledgeable",
    "Gacrux": "mature",
    "Algenib": "gravelly",
    "Orus": "firm",
    "Kore": "firm",
    "Sulafat": "warm",
    "Iapetus": "clear",
}
DEFAULT_VOICE = "Charon"


class VoiceError(Exception):
    """A plain-language problem while creating narration."""


class VoiceProvider:
    name = "base"

    def is_configured(self):
        raise NotImplementedError

    def synthesize(self, text, voice, destination, job_id=None):
        """Write narration audio (WAV) to destination. Return details dict."""
        raise NotImplementedError


def _pcm_to_wav(pcm, destination, rate=24000):
    with wave.open(str(destination), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)


class GeminiVoice(VoiceProvider):
    name = "gemini-tts"

    def __init__(self, api_key=GEMINI_API_KEY, models=GEMINI_TTS_MODELS, timeout=240):
        self.api_key = api_key
        self.models = list(models)
        self.timeout = timeout

    def is_configured(self):
        return bool(self.api_key)

    def _call(self, model, text, voice):
        config = {"responseModalities": ["AUDIO"]}
        if voice:
            config["speechConfig"] = {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}
        body = json.dumps({"contents": [{"parts": [{"text": text}]}],
                           "generationConfig": config}).encode("utf-8")
        request = urllib.request.Request(
            TTS_URL.format(model=model), data=body, method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _audio_part(data):
        for candidate in data.get("candidates", []) or []:
            for part in (candidate.get("content") or {}).get("parts", []) or []:
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    return inline.get("mimeType") or inline.get("mime_type") or "", inline["data"]
        return None, None

    def synthesize(self, text, voice, destination, job_id=None):
        if not self.is_configured():
            raise VoiceError("No Gemini key found. Redo Step 9 to save your key.")
        problems = []
        for model in self.models:
            used_voice = voice
            try:
                try:
                    data = self._call(model, text, voice)
                except urllib.error.HTTPError as first:
                    if first.code == 400 and voice:
                        used_voice = ""  # this model did not accept the voice name: use its default
                        data = self._call(model, text, "")
                    else:
                        raise
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
                if exc.code == 400 and ("API_KEY_INVALID" in detail or "API key not valid" in detail):
                    raise VoiceError("Google rejected the key. Create a new key in AI Studio and redo Step 9.")
                label = {404: "not available", 429: "free limit reached or busy"}.get(exc.code, f"error {exc.code}")
                problems.append(f"{model}: {label}")
                continue
            except (TimeoutError, socket.timeout):
                problems.append(f"{model}: took too long")
                continue
            except urllib.error.URLError:
                raise VoiceError("Could not reach Google. Check the Mac's internet connection.")

            mime, encoded = self._audio_part(data)
            if not encoded:
                problems.append(f"{model}: no audio in the answer")
                continue
            audio = base64.b64decode(encoded)
            if audio[:4] == b"RIFF" or "wav" in mime.lower():
                Path(destination).write_bytes(audio)
            else:  # raw 16-bit PCM, e.g. "audio/L16;codec=pcm;rate=24000"
                match = re.search(r"rate=(\d+)", mime)
                _pcm_to_wav(audio, destination, int(match.group(1)) if match else 24000)

            seconds = probe_duration(destination)
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO provider_usage (job_id, provider, category, units, unit_name, estimated_cost_usd) "
                    "VALUES (?, ?, 'tts', ?, 'seconds', 0)", (job_id, f"gemini:{model}", seconds))
            return {"model": model, "voice": used_voice or "model default", "seconds": seconds}
        raise VoiceError("No free Gemini voice model answered. Details: " + " | ".join(problems))


# ---------- audio tools (FFmpeg) ----------

def _run(args, timeout=180):
    return subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=timeout)


def probe_duration(path):
    result = _run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path], 60)
    try:
        return round(float(result.stdout.strip()), 2)
    except ValueError:
        return 0.0


def finish_audio(raw, master, preview):
    """Trim silence at both ends, even out loudness, make a small preview for the phone."""
    trim = ("silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05,areverse,"
            "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05,areverse,"
            "loudnorm=I=-16:TP=-1.5:LRA=11,apad=pad_dur=0.4")
    result = _run([FFMPEG, "-y", "-v", "error", "-i", raw, "-af", trim, "-ar", "48000", "-ac", "1", master])
    if result.returncode != 0 or probe_duration(master) <= 0:
        raise VoiceError("FFmpeg could not process the narration audio.")
    _run([FFMPEG, "-y", "-v", "error", "-i", master, "-c:a", "aac", "-b:a", "96k", preview])


def find_pauses(path):
    """Return the middle time (seconds) of every short silence in the narration."""
    result = _run([FFMPEG, "-v", "info", "-i", path, "-af", "silencedetect=noise=-35dB:d=0.18",
                   "-f", "null", "-"])
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", result.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", result.stderr)]
    return [round((s + e) / 2, 2) for s, e in zip(starts, ends)]


def scene_timings(word_counts, total_seconds, pauses):
    """Decide when each scene starts and ends: aim for where its words should end,
    then snap to the nearest natural pause in the voice."""
    total_words = max(1, sum(word_counts))
    ends, previous, spoken = [], 0.0, 0
    window = max(1.5, 0.12 * total_seconds)
    for count in word_counts[:-1]:
        spoken += count
        expected = total_seconds * spoken / total_words
        nearby = [p for p in pauses if p > previous + 0.8 and abs(p - expected) <= window]
        chosen = min(nearby, key=lambda p: abs(p - expected)) if nearby else expected
        chosen = max(chosen, previous + 0.8)
        ends.append(round(chosen, 2))
        previous = chosen
    ends.append(round(total_seconds, 2))
    starts = [0.0] + ends[:-1]
    return [{"start": s, "end": e, "duration": round(e - s, 2)} for s, e in zip(starts, ends)]
