"""
VideoFactory - captions and video rendering (FFmpeg).

Captions
  The narration of each scene is split into short chunks (1-3 words) that
  appear one after another, big and high-contrast, above the area where
  YouTube/Instagram put their buttons. Scene start/end times are exact
  (taken from the voice); timing of chunks inside a scene is estimated from
  word length. Both an .ass file (burned into the video) and an .srt file
  (for uploading as subtitles later) are written.

Rendering
  1. Each scene picture becomes a short clip with gentle camera motion
     (zoom in / zoom out / pan), exactly as long as its narration.
  2. The clips are joined in order.
  3. The narration is added and the captions are burned in.
  Output: 1080x1920 (9:16), 30 fps, H.264 video + AAC audio, MP4.
"""
import os
import json
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
FFMPEG = BASE_DIR / "bin" / ("ffmpeg" + (".exe" if os.name == "nt" else ""))
FFPROBE = BASE_DIR / "bin" / ("ffprobe" + (".exe" if os.name == "nt" else ""))
WIDTH, HEIGHT, FPS = 1080, 1920, 30
MAC_FONTS_DIR = "/System/Library/Fonts/Supplemental"
CAPTION_FONT = "Arial Black"
MAX_CHUNK_WORDS, MAX_CHUNK_CHARS = 3, 18


class RenderError(Exception):
    """A plain-language problem while building the video."""


def _run(args, cwd=None, timeout=900):
    return subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=timeout, cwd=cwd)


# ---------- captions ----------

def chunk_words(text):
    chunks, current = [], []
    for word in text.split():
        candidate = current + [word]
        if current and (len(candidate) > MAX_CHUNK_WORDS or len(" ".join(candidate)) > MAX_CHUNK_CHARS):
            chunks.append(" ".join(current))
            current = [word]
        else:
            current = candidate
        if word[-1:] in ".!?," and current:
            chunks.append(" ".join(current))
            current = []
    if current:
        chunks.append(" ".join(current))
    return chunks


def caption_cues(scenes, timings):
    """scenes: [{'narration'}], timings: [{'start','end'}] -> [(start, end, text)]."""
    cues = []
    for scene, timing in zip(scenes, timings):
        chunks = chunk_words(scene["narration"])
        if not chunks:
            continue
        start, end = timing["start"], max(timing["end"] - 0.05, timing["start"] + 0.3)
        weights = [len(c) + 3 for c in chunks]
        total = sum(weights)
        position = start
        for chunk, weight in zip(chunks, weights):
            length = (end - start) * weight / total
            cues.append((round(position, 2), round(position + length, 2), chunk))
            position += length
    return cues


def _ass_time(seconds):
    centis = int(round(seconds * 100))
    h, rest = divmod(centis, 360000)
    m, rest = divmod(rest, 6000)
    s, cs = divmod(rest, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _srt_time(seconds):
    millis = int(round(seconds * 1000))
    h, rest = divmod(millis, 3600000)
    m, rest = divmod(rest, 60000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_captions(cues, ass_path, srt_path):
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {WIDTH}
PlayResY: {HEIGHT}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{CAPTION_FONT},92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,-1,0,0,0,100,100,0,0,1,7,3,2,90,90,560,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for start, end, text in cues:
        safe = text.replace("\\", "").replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{safe}")
    Path(ass_path).write_text(header + "\n".join(lines) + "\n", encoding="utf-8")

    srt = []
    for number, (start, end, text) in enumerate(cues, start=1):
        srt.append(f"{number}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}\n")
    Path(srt_path).write_text("\n".join(srt), encoding="utf-8")


# ---------- video ----------

def _motion_filter(motion, frames):
    d = max(frames - 1, 1)
    center_x, center_y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if motion == "zoom_out":
        z, x, y = f"1.15-0.15*on/{d}", center_x, center_y
    elif motion == "pan_left":
        z, x, y = "1.15", f"(iw-iw/zoom)*(1-on/{d})", center_y
    elif motion == "pan_right":
        z, x, y = "1.15", f"(iw-iw/zoom)*on/{d}", center_y
    else:  # zoom_in
        z, x, y = f"1+0.15*on/{d}", center_x, center_y
    return (f"scale={WIDTH * 2}:{HEIGHT * 2},"
            f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS},"
            f"format=yuv420p")


def render_scene_clip(picture, motion, seconds, destination):
    frames = max(int(round(seconds * FPS)), FPS // 2)
    result = _run([FFMPEG, "-y", "-v", "error", "-i", picture,
                   "-vf", _motion_filter(motion, frames), "-frames:v", frames,
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", FPS, "-an", destination])
    if result.returncode != 0 or not Path(destination).exists():
        raise RenderError(f"FFmpeg could not animate a scene picture: {result.stderr.strip()[-300:]}")


def assemble(folder, clips, voice_path, ass_name, total_seconds, output):
    """Join clips, add narration, burn captions. folder is the working directory."""
    folder = Path(folder)
    list_file = folder / "clips.txt"
    list_file.write_text("".join(f"file '{Path(c).name}'\n" for c in clips), encoding="utf-8")
    joined = folder / "joined.mp4"
    result = _run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", list_file.name,
                   "-c", "copy", joined.name], cwd=folder)
    if result.returncode != 0:
        raise RenderError(f"FFmpeg could not join the scene clips: {result.stderr.strip()[-300:]}")

    subtitles = f"subtitles={ass_name}"
    if Path(MAC_FONTS_DIR).is_dir():
        subtitles += f":fontsdir={MAC_FONTS_DIR}"
    result = _run([FFMPEG, "-y", "-v", "error", "-i", joined.name, "-i", Path(voice_path).resolve(),
                   "-map", "0:v:0", "-map", "1:a:0", "-vf", subtitles,
                   "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-r", FPS,
                   "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
                   "-t", f"{total_seconds:.2f}", "-movflags", "+faststart", Path(output).resolve()],
                  cwd=folder, timeout=1800)
    if result.returncode != 0 or not Path(output).exists():
        raise RenderError(f"FFmpeg could not finish the video: {result.stderr.strip()[-300:]}")


def probe_video(path):
    result = _run([FFPROBE, "-v", "error", "-show_entries",
                   "stream=codec_type,width,height:format=duration", "-of", "json", path], timeout=60)
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    return {"width": video.get("width"), "height": video.get("height"),
            "duration": float((data.get("format") or {}).get("duration") or 0), "has_audio": has_audio}
