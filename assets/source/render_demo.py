"""Render the synthetic README storyboard. Requires Pillow; no model or service calls."""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
WIDTH, HEIGHT, SCALE = 1000, 620, 2
INK, BLUE, MUTED = "#182237", "#4b70ff", "#748098"
LINE, PAPER, PALE = "#e5e9f1", "#fbfcff", "#eef2ff"


def font(size, bold=False, mono=False):
    names = (
        ["/System/Library/Fonts/Supplemental/Courier New.ttf", "DejaVuSansMono.ttf"]
        if mono
        else ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "DejaVuSans-Bold.ttf"]
        if bold
        else ["/System/Library/Fonts/Supplemental/Arial.ttf", "DejaVuSans.ttf"]
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size * SCALE)
        except OSError:
            continue
    return ImageFont.load_default(size=size * SCALE)


class Canvas:
    def __init__(self):
        self.image = Image.new("RGB", (WIDTH * SCALE, HEIGHT * SCALE), PAPER)
        self.draw = ImageDraw.Draw(self.image)

    def box(self, xy, fill, radius=0, outline=None, width=1):
        self.draw.rounded_rectangle(
            tuple(round(v * SCALE) for v in xy),
            radius=radius * SCALE,
            fill=fill,
            outline=outline,
            width=width * SCALE,
        )

    def line(self, pts, fill=INK, width=2):
        self.draw.line(
            [(round(x * SCALE), round(y * SCALE)) for x, y in pts],
            fill=fill,
            width=width * SCALE,
            joint="curve",
        )

    def dot(self, x, y, r, fill):
        self.draw.ellipse(
            ((x - r) * SCALE, (y - r) * SCALE, (x + r) * SCALE, (y + r) * SCALE), fill=fill
        )

    def text(self, xy, text, size=16, fill=INK, bold=False, mono=False):
        self.draw.text((xy[0] * SCALE, xy[1] * SCALE), text, font=font(size, bold, mono), fill=fill)

    def mascot(self, x, y, size=1, motion=0):
        # The same silhouette and ribbon as oath-mark.svg.
        def pt(a, b):
            return x + a * size, y + b * size

        self.box(
            (*pt(51, 26), *pt(125, 137)), "#ffffff", int(35 * size), INK, max(1, int(6 * size))
        )
        w = max(1, int(6 * size))
        self.line([pt(51, 89), pt(32, 80), pt(29, 55 + motion)], width=w)
        self.line([pt(29, 55 + motion), pt(22, 47 + motion)], width=w)
        self.line([pt(29, 55 + motion), pt(32, 44 + motion)], width=w)
        self.line([pt(125, 91), pt(142, 100), pt(145, 112)], width=w)
        for a in (73, 103):
            self.line([pt(a, 138), pt(a, 153)], width=w)
        for a in (75, 100):
            self.line([pt(a, 65), pt(a, 70)], width=max(1, int(7 * size)))
        self.line([pt(82, 85), pt(88, 88), pt(94, 85)], width=max(1, int(4 * size)))
        self.line([pt(59, 108), pt(118, 120)], BLUE, max(1, int(9 * size)))


def chat(c, y, lines, user=False):
    x, right = (104, 516) if user else (68, 482)
    h = 32 + 23 * len(lines)
    c.box((x, y, right, y + h), PALE if user else "#ffffff", 15, None if user else LINE)
    for i, value in enumerate(lines):
        c.text((x + 16, y + 15 + 23 * i), value, 15)


def check(c, x, y):
    c.dot(x, y, 10, PALE)
    c.line([(x - 4, y), (x - 1, y + 3), (x + 5, y - 4)], BLUE, 2)


def render(t):
    c = Canvas()
    offline, returned = 4 <= t < 14, t >= 14
    done = t >= 11
    c.text((38, 25), "A connection can end. The work can continue.", 24, bold=True)
    c.text((39, 62), "One task. One persistent agent. Two independent lifecycles.", 15, MUTED)
    c.box((38, 101, 542, 552), "#ffffff", 20, LINE)
    c.box((560, 101, 962, 552), "#ffffff", 20, LINE)
    c.mascot(49, 108, 0.25)
    c.text((101, 122), "Your conversation", 17, bold=True)
    c.dot(438, 133, 4, MUTED if offline else BLUE)
    c.text((450, 124), "Offline" if offline else "Online", 13, MUTED)
    c.line([(58, 163), (522, 163)], LINE, 1)
    c.text((585, 122), "Oath", 17, bold=True)
    c.text((830, 125), "SERVER SIDE", 11, MUTED, bold=True)
    c.line([(580, 163), (942, 163)], LINE, 1)
    if not offline:
        if t >= 18.5:
            c.text((69, 185), "offline.start  /  replay missed events", 11, BLUE, mono=True)
            chat(c, 210, ["Done while you were away.", "Saved: workspace/action-plan.md"])
            c.text((69, 302), "offline.end  /  live", 11, MUTED, mono=True)
            chat(c, 330, ["What should I tackle first?"], user=True)
            if t >= 19.7:
                chat(c, 403, ["Start with the deployment checklist."])
        else:
            chat(c, 183, ["Summarize the project notes.", "Save a short action plan."], user=True)
            if t >= 1.2:
                chat(c, 278, ["I'll read the notes and save the plan."])
        if returned and t < 18.5:
            c.text((69, 348), "offline.start  /  replay missed events", 11, BLUE, mono=True)
            if t >= 14.8:
                chat(c, 371, ["Done while you were away.", "Saved: workspace/action-plan.md"])
            if t >= 15.8:
                c.text((69, 463), "offline.end  /  live", 11, MUTED, mono=True)
        c.box((60, 499, 520, 532), PAPER, 11, LINE)
        c.text(
            (74, 507),
            "What should I tackle first?" if 17.5 <= t < 18.5 else "Ask Oath anything...",
            13,
            MUTED,
        )
        c.dot(499, 515, 10, BLUE)
        c.line([(495, 515), (503, 515), (500, 512)], "#ffffff", 1)
    else:
        c.box((61, 184, 519, 529), PAPER, 16)
        c.mascot(234, 226, 0.65, 3 * math.sin(t * 3))
        c.text((163, 366), "You closed the client.", 20, bold=True)
        c.text(
            (127, 402),
            "Your result is saved. Come back anytime."
            if done
            else "Oath is still handling your accepted task.",
            14,
            MUTED,
        )
        c.box((156, 451, 424, 487), "#ffffff", 12, LINE)
        c.text((176, 462), "Results will be here when you return.", 13, MUTED)
    # Execution follows its own clock through the disconnect.
    tasks = [
        (1.0, 4.5, "Read workspace notes", "Bash  /  notes/*.md"),
        (4.5, 8.0, "Build a short action plan", "Model  /  next step"),
        (8.0, 11.0, "Save action-plan.md", "Bash  /  write workspace file"),
    ]
    c.text((585, 185), "TASK PROGRESS", 11, MUTED, bold=True)
    for i, (start, end, title, detail) in enumerate(tasks):
        y = 229 + i * 69
        complete, active = t >= end, start <= t < end
        if complete:
            check(c, 596, y + 9)
        elif active:
            c.dot(596, y + 9, 10, PALE)
            c.dot(596, y + 9, 3 + 1.4 * math.sin(t * 5), BLUE)
        else:
            c.dot(596, y + 9, 6, LINE)
        c.text((617, y), title, 16, INK if t >= start else MUTED, bold=active)
        c.text((617, y + 24), detail, 12, MUTED, mono=True)
        if i < 2:
            c.line([(596, y + 26), (596, y + 55)], LINE, 1)
    c.box((583, 444, 939, 531), PAPER, 13)
    if t < 1:
        status, detail = "Input accepted", "Ready for a worker"
    elif not done:
        status, detail = (
            ("Client offline. Agent working.", "New events are persisted.")
            if offline
            else ("Agent working", "Tool results wake the next step.")
        )
    elif offline:
        status, detail = "Completed while offline", "Reply saved. Waiting for your return."
    elif t < 16:
        status, detail = "Replaying saved events", "Ordered delivery from the device cursor."
    elif 18.5 <= t < 19.7:
        status, detail = "New message. Next step.", "Your conversation continues."
    else:
        status, detail = "Caught up. Ready when you are.", "Idle checks make no model call."
    c.dot(599, 463, 3, BLUE)
    c.text((612, 454), status, 14, bold=True)
    c.text((598, 487), detail, 12, MUTED)
    c.text((39, 576), "EVENT-DRIVEN  /  DURABLE  /  SUPERVISED", 11, MUTED, mono=True)
    c.text((791, 575), "ILLUSTRATED WORKFLOW", 10, MUTED, bold=True)
    return c.image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)


def main():
    frames = [render(i / 6) for i in range(138)]
    # A shared palette keeps static surfaces stable across frames.
    palette = frames[0].quantize(colors=128)
    frames = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
    frames[0].save(
        ROOT / "oath-offline-demo.gif",
        save_all=True,
        append_images=frames[1:],
        duration=170,
        loop=0,
        optimize=True,
        disposal=1,
    )
    print(ROOT / "oath-offline-demo.gif")


if __name__ == "__main__":
    main()
