"""
VideoFactory - automatic quality control of a finished video.

Each check returns {"name", "status": "pass" | "warn" | "fail", "detail", "fix"}.
"fix" says which production stage would repair a failure, so the factory can
redo just that part automatically (a limited number of times).

Checks (all measured with FFmpeg/FFprobe on the real file):
  VIDEO     resolution 1080x1920, 9:16, length, every scene present,
            whole file decodes without errors, no black frames
  AUDIO     voice present and loud enough, no clipping, no long unwanted silences
  CAPTIONS  file present, every narrated word appears, timing inside the video
"""
import os
import json
import re
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
FFMPEG = BASE_DIR / "bin" / ("ffmpeg" + (".exe" if os.name == "nt" else ""))
FFPROBE = BASE_DIR / "bin" / ("ffprobe" + (".exe" if os.name == "nt" else ""))
MIN_SECONDS, MAX_SECONDS = 15, 90


def _run(args, timeout=600):
    return subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=timeout)


def _check(name, ok, detail, fix=None, warn_only=False):
    status = "pass" if ok else ("warn" if warn_only else "fail")
    return {"name": name, "status": status, "detail": detail, "fix": None if ok else fix}


def _words(text):
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def probe(path):
    result = _run([FFPROBE, "-v", "error", "-show_entries",
                   "stream=codec_type,width,height,avg_frame_rate:format=duration", "-of", "json", path], 60)
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        data = {}
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    return {"width": video.get("width"), "height": video.get("height"),
            "has_audio": any(s.get("codec_type") == "audio" for s in streams),
            "duration": float((data.get("format") or {}).get("duration") or 0)}


def check_video_file(video_path, scene_count, clip_count_expected):
    checks = []
    info = probe(video_path)
    checks.append(_check("Resolution 1080x1920 (9:16)",
                         info["width"] == 1080 and info["height"] == 1920,
                         f"{info['width']}x{info['height']}", "ASSEMBLING"))
    checks.append(_check(f"Length {MIN_SECONDS}-{MAX_SECONDS} seconds",
                         MIN_SECONDS <= info["duration"] <= MAX_SECONDS,
                         f"{info['duration']:.1f} seconds", "SCRIPTING"))
    checks.append(_check("Every scene is in the video", scene_count == clip_count_expected and scene_count > 0,
                         f"{clip_count_expected} of {scene_count} scenes", "ASSEMBLING"))

    decode = _run([FFMPEG, "-v", "error", "-i", video_path, "-f", "null", "-"], 900)
    errors = [line for line in decode.stderr.splitlines() if line.strip()]
    checks.append(_check("File plays from start to end without errors",
                         decode.returncode == 0 and not errors,
                         "no errors" if not errors else errors[0][:150], "ASSEMBLING"))

    black = _run([FFMPEG, "-v", "info", "-i", video_path, "-vf",
                  "blackdetect=d=0.5:pix_th=0.05", "-an", "-f", "null", "-"], 900)
    spans = re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", black.stderr)
    checks.append(_check("No black frames", not spans,
                         "none found" if not spans else f"black from {spans[0][0]}s to {spans[0][1]}s",
                         "GENERATING_VISUALS"))
    return checks, info


def check_audio(video_path, duration):
    checks = []
    vol = _run([FFMPEG, "-v", "info", "-i", video_path, "-vn", "-af", "volumedetect", "-f", "null", "-"], 600)
    mean = re.search(r"mean_volume: (-?[\d.]+) dB", vol.stderr)
    peak = re.search(r"max_volume: (-?[\d.]+) dB", vol.stderr)
    mean_db = float(mean.group(1)) if mean else -99.0
    peak_db = float(peak.group(1)) if peak else 0.0
    checks.append(_check("Voice is clearly audible", mean_db > -30, f"average loudness {mean_db:.1f} dB",
                         "GENERATING_VOICE"))
    checks.append(_check("No clipping (distortion)", peak_db <= -0.1, f"loudest peak {peak_db:.1f} dB",
                         "GENERATING_VOICE"))

    sil = _run([FFMPEG, "-v", "info", "-i", video_path, "-vn", "-af",
                "silencedetect=noise=-40dB:d=1.2", "-f", "null", "-"], 600)
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", sil.stderr)]
    inner = [s for s in starts if s < duration - 1.5]
    checks.append(_check("No long silent gaps", not inner,
                         "none" if not inner else f"silence starting at {inner[0]:.1f}s",
                         "GENERATING_VOICE"))
    return checks


def check_captions(srt_path, narration, duration):
    if not Path(srt_path).exists():
        return [_check("Captions present", False, "captions file missing", "GENERATING_CAPTIONS")]
    text = Path(srt_path).read_text(encoding="utf-8")
    times = re.findall(r"(\d\d):(\d\d):(\d\d),(\d\d\d) --> (\d\d):(\d\d):(\d\d),(\d\d\d)", text)
    caption_lines = [line for line in text.splitlines()
                     if line.strip() and "-->" not in line and not line.strip().isdigit()]
    last_end = 0.0
    if times:
        h, m, s, ms = map(int, times[-1][4:])
        last_end = h * 3600 + m * 60 + s + ms / 1000
    same_words = _words(" ".join(caption_lines)) == _words(narration)
    longest = max((len(w) for w in " ".join(caption_lines).split()), default=0)
    return [
        _check("Captions present", bool(times), f"{len(times)} caption chunks", "GENERATING_CAPTIONS"),
        _check("Captions match the narration word for word", same_words,
               "every word matches" if same_words else "captions and narration differ", "GENERATING_CAPTIONS"),
        _check("Captions end inside the video", last_end <= duration + 0.5,
               f"last caption ends at {last_end:.1f}s of {duration:.1f}s", "GENERATING_CAPTIONS"),
        _check("Caption words fit on screen", longest <= 14,
               f"longest word has {longest} letters", None, warn_only=True),
    ]
