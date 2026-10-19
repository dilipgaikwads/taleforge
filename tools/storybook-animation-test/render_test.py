"""Storybook puppet animation test: a Roman soldier walks in and speaks with lip sync.

EXPERIMENTAL prototype (not part of the production line yet). See README.md here.
Inputs in this folder: voice.wav (the spoken line) and mouth.json (from Rhubarb).
Output: frames/f0000.png ... then FFmpeg joins them (command in README.md).
"""
import json, math, random
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

S = 2                      # draw at 2x, then shrink = smooth edges
C = 3                      # character scale (1.5x)
W, H = 720 * S, 1280 * S
FPS, DURATION = 30, 10.0
SPEECH_START = 3.4
HERE = Path(__file__).resolve().parent
OUT = HERE / "frames"
OUT.mkdir(exist_ok=True)

INK = (91, 70, 54)
SKIN = (232, 195, 160)
TUNIC = (168, 72, 60)
TUNIC_DARK = (140, 58, 48)
METAL = (176, 172, 152)
CREST = (181, 82, 59)
LEATHER = (107, 79, 58)
BLUSH = (229, 154, 138, 110)

def find_font(names, size):
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


BOLD_FONTS = ["/System/Library/Fonts/Supplemental/Georgia Bold.ttf", "C:/Windows/Fonts/georgiab.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"]
REGULAR_FONTS = ["/System/Library/Fonts/Supplemental/Georgia.ttf", "C:/Windows/Fonts/georgia.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"]
serif_bold = find_font(BOLD_FONTS, 34 * S)
serif = find_font(REGULAR_FONTS, 34 * S)

cues = json.load(open(HERE / "mouth.json"))["mouthCues"]


def mouth_at(t):
    t -= SPEECH_START
    for c in cues:
        if c["start"] <= t < c["end"]:
            return c["value"]
    return "X"


# ---------- painted background (made once) ----------
def make_background():
    rnd = random.Random(7)
    bg = Image.new("RGB", (W, H), (246, 231, 200))
    d = ImageDraw.Draw(bg)
    for y in range(0, int(H * 0.62)):            # warm sky, lighter at the top
        k = y / (H * 0.62)
        d.line([(0, y), (W, y)], fill=(int(250 - 6 * k), int(238 - 10 * k), int(212 - 16 * k)))
    soft = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(soft)
    sd.ellipse([W * 0.66, H * 0.10, W * 0.90, H * 0.10 + W * 0.24], fill=(242, 195, 107, 230))   # sun
    for (cx, cy, rx, ry, col) in [(0.15, 0.17, 0.16, 0.035, (255, 250, 238, 170)),
                                  (0.45, 0.13, 0.20, 0.03, (255, 250, 238, 150))]:              # clouds
        sd.ellipse([W * (cx - rx), H * (cy - ry), W * (cx + rx), H * (cy + ry)], fill=col)
    sd.ellipse([-W * 0.3, H * 0.50, W * 0.8, H * 0.78], fill=(219, 200, 160, 255))               # far hills
    sd.ellipse([W * 0.35, H * 0.52, W * 1.4, H * 0.80], fill=(208, 186, 142, 255))
    tx, ty = W * 0.58, H * 0.505                                                                   # distant temple
    sd.polygon([(tx - 150, ty), (tx + 150, ty), (tx, ty - 70)], fill=(196, 172, 128, 255))
    for i in range(6):
        sd.rectangle([tx - 135 + i * 52, ty, tx - 120 + i * 52, ty + 120], fill=(196, 172, 128, 255))
    sd.rectangle([tx - 150, ty + 115, tx + 150, ty + 135], fill=(196, 172, 128, 255))
    soft = soft.filter(ImageFilter.GaussianBlur(4 * S))
    bg.paste(soft, (0, 0), soft)
    near = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    nd = ImageDraw.Draw(near)
    nd.rectangle([0, H * 0.70, W, H], fill=(201, 174, 122, 255))                                   # ground
    nd.ellipse([-W * 0.2, H * 0.64, W * 1.2, H * 0.80], fill=(201, 174, 122, 255))
    nd.polygon([(W * 0.42, H * 0.70), (W * 0.58, H * 0.70), (W * 0.9, H), (W * 0.1, H)], fill=(222, 202, 160, 255))  # path
    for (x, s) in [(0.08, 1.0), (0.88, 1.2)]:                                                      # trees
        cx, base = W * x, H * 0.72
        nd.rectangle([cx - 10 * S, base - 170 * S * s, cx + 10 * S, base], fill=(120, 92, 66, 255))
        nd.ellipse([cx - 90 * S * s, base - 330 * S * s, cx + 90 * S * s, base - 140 * S * s], fill=(150, 160, 104, 255))
        nd.ellipse([cx - 60 * S * s, base - 360 * S * s, cx + 70 * S * s, base - 220 * S * s], fill=(166, 176, 116, 255))
    near = near.filter(ImageFilter.GaussianBlur(1.5 * S))
    bg.paste(near, (0, 0), near)
    d = ImageDraw.Draw(bg)
    for _ in range(260):                                                                           # grass strokes
        x, y = rnd.uniform(0, W), rnd.uniform(H * 0.71, H)
        d.line([(x, y), (x + rnd.uniform(-6, 6) * S, y - rnd.uniform(8, 18) * S)], fill=(176, 150, 98), width=2 * S)
    noise = np.random.default_rng(3).normal(0, 9, (H // 4, W // 4)).astype(np.int16)              # paper grain
    noise = np.array(Image.fromarray((noise + 128).clip(0, 255).astype(np.uint8)).resize((W, H)))
    arr = np.array(bg).astype(np.int16) + (noise[..., None].astype(np.int16) - 128) // 2
    return Image.fromarray(arr.clip(0, 255).astype(np.uint8))


# ---------- puppet drawing helpers ----------
def limb(d, p1, p2, width, color):
    d.line([p1, p2], fill=INK, width=width + 6 * C)
    for p in (p1, p2):
        r = (width + 6 * C) / 2
        d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=INK)
    d.line([p1, p2], fill=color, width=width)
    for p in (p1, p2):
        r = width / 2
        d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=color)


def point(origin, angle_deg, length):
    a = math.radians(angle_deg)
    return (origin[0] + math.sin(a) * length, origin[1] + math.cos(a) * length)


def leg(d, hip, swing, bend):
    knee = point(hip, swing, 88 * C)
    foot = point(knee, swing - bend, 84 * C)
    limb(d, hip, knee, 26 * C, SKIN)
    limb(d, knee, foot, 24 * C, SKIN)
    d.ellipse([foot[0] - 22 * C, foot[1] - 9 * C, foot[0] + 26 * C, foot[1] + 11 * C], fill=LEATHER, outline=INK, width=3 * C)


def arm(d, shoulder, upper, lower):
    elbow = point(shoulder, upper, 74 * C)
    hand = point(elbow, lower, 68 * C)
    limb(d, shoulder, elbow, 22 * C, SKIN)
    limb(d, elbow, hand, 20 * C, SKIN)
    d.ellipse([hand[0] - 14 * C, hand[1] - 14 * C, hand[0] + 14 * C, hand[1] + 14 * C], fill=SKIN, outline=INK, width=3 * C)
    return hand


def draw_mouth(d, cx, cy, shape):
    k = C
    if shape in ("X", "A"):
        d.arc([cx - 16 * k, cy - 8 * k, cx + 16 * k, cy + 8 * k], 20, 160, fill=INK, width=3 * k)
    elif shape == "B":
        d.ellipse([cx - 16 * k, cy - 5 * k, cx + 16 * k, cy + 7 * k], fill=(120, 50, 45), outline=INK, width=3 * k)
        d.rectangle([cx - 11 * k, cy - 3 * k, cx + 11 * k, cy], fill=(250, 245, 235))
    elif shape == "C":
        d.ellipse([cx - 17 * k, cy - 9 * k, cx + 17 * k, cy + 11 * k], fill=(120, 50, 45), outline=INK, width=3 * k)
    elif shape == "D":
        d.ellipse([cx - 19 * k, cy - 12 * k, cx + 19 * k, cy + 18 * k], fill=(120, 50, 45), outline=INK, width=3 * k)
        d.ellipse([cx - 10 * k, cy + 4 * k, cx + 10 * k, cy + 15 * k], fill=(205, 110, 105))
    elif shape == "E":
        d.ellipse([cx - 11 * k, cy - 9 * k, cx + 11 * k, cy + 11 * k], fill=(120, 50, 45), outline=INK, width=3 * k)
    elif shape == "F":
        d.ellipse([cx - 7 * k, cy - 6 * k, cx + 7 * k, cy + 7 * k], fill=(120, 50, 45), outline=INK, width=3 * k)
    elif shape == "G":
        d.line([(cx - 14 * k, cy), (cx + 14 * k, cy)], fill=INK, width=3 * k)
        d.rectangle([cx - 9 * k, cy - 4 * k, cx + 9 * k, cy], fill=(250, 245, 235), outline=INK, width=2 * k)
    else:  # H
        d.ellipse([cx - 15 * k, cy - 9 * k, cx + 15 * k, cy + 11 * k], fill=(120, 50, 45), outline=INK, width=3 * k)
        d.ellipse([cx - 7 * k, cy - 6 * k, cx + 7 * k, cy + 2 * k], fill=(205, 110, 105))


def soldier(layer, x, ground, t, walking, phase, gesture, shrug, blink, mouth, brow):
    d = ImageDraw.Draw(layer, "RGBA")
    bob = -abs(math.sin(phase)) * 14 * C if walking else math.sin(t * 2.2) * 3 * C
    hip = (x, ground - 172 * C + bob)
    d.ellipse([x - 95 * C, ground - 14 * C, x + 95 * C, ground + 14 * C], fill=(120, 95, 60, 70))   # shadow
    if walking:
        s1, s2 = 28 * math.sin(phase), -28 * math.sin(phase)
        b1, b2 = max(0, 40 * math.sin(phase + math.pi / 2)), max(0, 40 * math.sin(phase - math.pi / 2))
    else:
        s1, s2, b1, b2 = 6, -6, 0, 0
    leg(d, (hip[0] - 18 * C, hip[1]), s2, b2)                                 # back leg
    shoulder_b = (hip[0] - 34 * C, hip[1] - 150 * C)
    shoulder_f = (hip[0] + 34 * C, hip[1] - 150 * C)
    swing = 30 * math.sin(phase) if walking else 0
    lerp = lambda a, b, k: a + (b - a) * k
    back_up = lerp(-swing + 5, -55, shrug)
    back_low = lerp(back_up + 15, -150, shrug)
    hand_b = arm(d, shoulder_b, back_up, back_low)                            # back arm
    rest_hand = point(point(shoulder_b, 5, 74 * C), 20, 68 * C)
    anchor = hand_b if walking else rest_hand       # spear stays planted when he stops to talk
    sx = anchor[0] - (4 if walking else 46) * C
    d.line([(sx - 6 * C, anchor[1] - 360 * C), (sx + 3 * C, anchor[1] + 118 * C)], fill=LEATHER, width=7 * C)   # spear
    d.polygon([(sx - 18 * C, anchor[1] - 355 * C), (sx - 6 * C, anchor[1] - 415 * C), (sx + 6 * C, anchor[1] - 355 * C)], fill=METAL, outline=INK)
    leg(d, (hip[0] + 18 * C, hip[1]), s1, b1)                                 # front leg
    body = [(hip[0] - 62 * C, hip[1] + 22 * C), (hip[0] + 62 * C, hip[1] + 22 * C), (hip[0] + 48 * C, hip[1] - 165 * C), (hip[0] - 48 * C, hip[1] - 165 * C)]
    d.polygon(body, fill=TUNIC, outline=INK, width=4 * C)
    for i in range(5):                                                        # tunic folds
        fx = hip[0] - 44 * C + i * 22 * C
        d.line([(fx, hip[1] - 10 * C), (fx + 4 * C, hip[1] + 18 * C)], fill=TUNIC_DARK, width=3 * C)
    d.rectangle([hip[0] - 54 * C, hip[1] - 52 * C, hip[0] + 54 * C, hip[1] - 34 * C], fill=LEATHER, outline=INK, width=3 * C)
    d.ellipse([hip[0] - 66 * C, hip[1] - 175 * C, hip[0] - 14 * C, hip[1] - 125 * C], fill=METAL, outline=INK, width=3 * C)
    d.ellipse([hip[0] + 14 * C, hip[1] - 175 * C, hip[0] + 66 * C, hip[1] - 125 * C], fill=METAL, outline=INK, width=3 * C)
    front_up = lerp(lerp(swing - 5, 65, gesture), 55, shrug)
    front_low = lerp(lerp(front_up + 15, 125, gesture), 150, shrug)
    arm(d, shoulder_f, front_up, front_low)                                   # front arm
    hx, hy = hip[0] + 6 * C, hip[1] - 255 * C                                 # head
    d.rectangle([hx - 16 * C, hy + 50 * C, hx + 16 * C, hy + 92 * C], fill=SKIN, outline=INK, width=3 * C)
    d.ellipse([hx - 66 * C, hy - 72 * C, hx + 66 * C, hy + 80 * C], fill=SKIN, outline=INK, width=4 * C)
    d.ellipse([hx - 52 * C, hy + 10 * C, hx - 28 * C, hy + 30 * C], fill=BLUSH)
    d.ellipse([hx + 28 * C, hy + 10 * C, hx + 52 * C, hy + 30 * C], fill=BLUSH)
    by = hy - 22 * C - brow * 12 * C
    d.line([(hx - 40 * C, by + 4 * C), (hx - 16 * C, by)], fill=INK, width=4 * C)
    d.line([(hx + 16 * C, by), (hx + 40 * C, by + 4 * C)], fill=INK, width=4 * C)
    for ex in (hx - 27 * C, hx + 27 * C):
        if blink:
            d.line([(ex - 9 * C, hy - 2 * C), (ex + 9 * C, hy - 2 * C)], fill=INK, width=4 * C)
        else:
            d.ellipse([ex - 7 * C, hy - 10 * C, ex + 7 * C, hy + 6 * C], fill=INK)
            d.ellipse([ex - 2 * C, hy - 7 * C, ex + 3 * C, hy - 2 * C], fill=(255, 255, 255))
    draw_mouth(d, hx, hy + 40 * C, mouth)
    d.chord([hx - 74 * C, hy - 110 * C, hx + 74 * C, hy + 30 * C], 180, 360, fill=METAL, outline=INK, width=4 * C)   # helmet
    d.rectangle([hx - 76 * C, hy - 44 * C, hx + 76 * C, hy - 30 * C], fill=(150, 146, 126), outline=INK, width=3 * C)
    d.pieslice([hx - 60 * C, hy - 165 * C, hx + 60 * C, hy - 55 * C], 180, 360, fill=CREST, outline=INK, width=3 * C)


def ease(v):
    v = max(0.0, min(1.0, v))
    return v * v * (3 - 2 * v)


bg = make_background()
title_font_y = 46 * S
frames = int(FPS * DURATION)
speech_end = SPEECH_START + cues[-1]["end"]
for f in range(frames):
    t = f / FPS
    walking = t < 3.0
    x = (-160 + (W / S / 2 + 20 + 160) * ease(t / 3.0)) * S if walking else (W / 2 + 20 * S)
    phase = t * 2 * math.pi * 1.1
    talking = SPEECH_START <= t <= speech_end
    gesture = ease((t - SPEECH_START) / 0.5) * (1 - ease((t - (speech_end - 1.6)) / 0.4)) if talking else 0
    shrug = ease((t - (speech_end - 1.3)) / 0.4) * (1 - ease((t - 9.2) / 0.5)) if t > speech_end - 1.3 else 0
    brow = ease((t - (speech_end - 1.4)) / 0.3) * (1 - ease((t - 9.3) / 0.4)) if t > speech_end - 1.4 else 0
    blink = (t % 2.7) > 2.6 or 4.9 < t < 5.0
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    soldier(layer, x, int(H * 0.93), t, walking, phase, gesture, shrug, blink, mouth_at(t) if talking else "X", brow)
    frame = bg.copy()
    frame.paste(layer, (0, 0), layer)
    d = ImageDraw.Draw(frame, "RGBA")
    a = int(255 * ease(t / 1.2))
    for i, txt in enumerate(("Rome,", "two thousand years ago...")):
        tw = d.textlength(txt, font=serif_bold)
        d.text(((W - tw) / 2, title_font_y + i * 46 * S), txt, font=serif_bold, fill=(91, 70, 54, a))
    if talking:
        line = "Another month of marching..." if t < SPEECH_START + 2.3 else "...paid in salt? Again?!"
        lw = d.textlength(line, font=serif)
        bx, by = (W - lw) / 2, H * 0.165
        d.rounded_rectangle([bx - 30 * S, by - 22 * S, bx + lw + 30 * S, by + 62 * S], radius=26 * S, fill=(255, 250, 238, 235), outline=INK, width=3 * S)
        d.text((bx, by - 2 * S), line, font=serif, fill=INK)
    frame.resize((W // S, H // S), Image.LANCZOS).save(OUT / f"f{f:04d}.png")
print("frames:", frames)
