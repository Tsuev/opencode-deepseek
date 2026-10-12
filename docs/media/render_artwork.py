"""Render the README's editable SVG artwork and matching PNG previews.

Requires Pillow. No network requests, account data or terminal output are used.
"""

from html import escape
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


OUT = Path(__file__).parent
INK = "#182a35"
MUTED = "#627782"
BLUE = "#4169dc"
GREEN = "#298261"
ORANGE = "#ab673a"


def font(size, bold=False, mono=False):
    options = (
        ["/System/Library/Fonts/Menlo.ttc", "DejaVuSansMono.ttf"]
        if mono else
        ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "DejaVuSans-Bold.ttf"]
        if bold else
        ["/System/Library/Fonts/Supplemental/Arial.ttf", "DejaVuSans.ttf"]
    )
    for candidate in options:
        try:
            return ImageFont.truetype(candidate, size * 2)
        except OSError:
            continue
    raise RuntimeError("Install an Arial or DejaVu font to render previews")


class Canvas:
    def __init__(self, width, height, title, background):
        self.image = Image.new("RGB", (width * 2, height * 2), background)
        self.draw = ImageDraw.Draw(self.image)
        self.svg = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}" role="img">',
            f"<title>{escape(title)}</title>",
        ]
        self.rect(0, 0, width, height, background)

    def rect(self, x, y, w, h, fill, stroke=None, radius=0):
        self.draw.rounded_rectangle(
            (x * 2, y * 2, (x + w) * 2, (y + h) * 2),
            radius=radius * 2, fill=fill, outline=stroke, width=2,
        )
        self.svg.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" '
            f'rx="{radius}" fill="{fill}" stroke="{stroke or "none"}"/>'
        )

    def text(self, x, y, value, size=16, color=INK, bold=False, mono=False):
        self.draw.text((x * 2, y * 2), value, font=font(size, bold, mono), fill=color)
        family = "Menlo, monospace" if mono else "Arial, sans-serif"
        self.svg.append(
            f'<text x="{x}" y="{y + size}" font-size="{size}" '
            f'font-family="{family}" font-weight="{700 if bold else 400}" '
            f'fill="{color}">{escape(value)}</text>'
        )

    def line(self, points, color, width=2, arrow=False):
        self.draw.line([(x * 2, y * 2) for x, y in points], fill=color, width=width * 2)
        encoded = " ".join(f"{x},{y}" for x, y in points)
        self.svg.append(
            f'<polyline points="{encoded}" fill="none" stroke="{color}" '
            f'stroke-width="{width}" stroke-linejoin="round"/>'
        )
        if arrow:
            x, y = points[-1]
            triangle = [(x, y), (x - 7, y - 4), (x - 7, y + 4)]
            self.draw.polygon([(a * 2, b * 2) for a, b in triangle], fill=color)
            self.svg.append(
                f'<polygon points="{x},{y} {x-7},{y-4} {x-7},{y+4}" fill="{color}"/>'
            )

    def pill(self, x, y, width, label, fill, text_color):
        self.rect(x, y, width, 30, fill, radius=15)
        self.text(x + 14, y + 6, label, 13, text_color, bold=True)

    def save(self, name):
        self.svg.append("</svg>")
        (OUT / (name + ".svg")).write_text("\n".join(self.svg) + "\n")
        self.image.save(OUT / (name + ".png"), optimize=True)


def hero():
    c = Canvas(1100, 332, "OpenCode Web Bridge — your accounts, your workspace", "#14232d")
    c.text(48, 38, "OPENCODE WEB BRIDGE", 14, "#8cd8bd", bold=True)
    c.text(46, 84, "Your accounts.", 49, "#f5f3ec", bold=True)
    c.text(46, 141, "Your coding agent.", 49, "#f5f3ec", bold=True)
    c.text(49, 213, "One local API. Tools run in your workspace.", 19, "#b9c9cf")
    for x, width, label in [(48, 116, "DeepSeek"), (176, 91, "Qwen"), (279, 80, "GLM"), (371, 82, "Kimi")]:
        c.pill(x, 270, width, label, "#263c47", "#dcf0e7")
    c.rect(744, 42, 304, 248, "#20343f", "#36535f", 20)
    c.text(770, 68, "127.0.0.1:8000", 23, "#a5e4c9", mono=True)
    c.text(770, 112, "/v1/chat/completions", 16, "#d8e2e5", mono=True)
    c.line([(770, 151), (1022, 151)], "#42606b")
    c.text(770, 175, "Web accounts", 19, "#eff3ed", bold=True)
    c.text(770, 209, "read  ·  edit  ·  tools", 16, "#b9c9cf", mono=True)
    c.text(770, 247, "Account allowances apply", 13, "#99b0bb")
    c.line([(603, 157), (638, 157), (638, 114), (727, 114)], "#8cd8bd", arrow=True)
    c.line([(727, 230), (682, 230), (682, 249), (602, 249)], "#65808f")
    c.save("bridge-hero")


def architecture():
    c = Canvas(1100, 672, "OpenCode Web Bridge — architecture and verified provider paths", "#f5f3ed")
    c.text(38, 28, "A local bridge. Three connection paths.", 29, INK, bold=True)
    c.text(40, 72, "OpenCode owns the tools; providers receive prompts and permitted tool results.", 16, MUTED)

    c.rect(40, 224, 237, 203, "#ffffff", "#d3dcdb", 16)
    c.pill(59, 244, 96, "LOCAL", "#e9eeec", MUTED)
    c.text(61, 292, "OpenCode", 27, INK, bold=True)
    c.text(61, 337, "Agent + permissions", 17, MUTED)
    c.text(61, 370, "read / edit / commands", 14, MUTED, mono=True)

    c.rect(357, 224, 289, 203, "#e9efff", "#bdcbee", 16)
    c.pill(378, 244, 137, "LOOPBACK API", "#d5dfff", BLUE)
    c.text(379, 292, "Local bridge", 27, INK, bold=True)
    c.text(379, 338, "127.0.0.1:8000/v1", 16, BLUE, mono=True)
    c.text(379, 373, "Queues · validation · SSE", 16, MUTED)
    c.line([(277, 326), (345, 326)], BLUE, arrow=True)
    c.text(281, 289, "OpenAI", 12, BLUE)
    c.text(281, 305, "format", 12, BLUE)

    c.rect(757, 124, 304, 129, "#ffffff", "#cfddd5", 14)
    c.text(778, 141, "DeepSeek / Qwen", 22, INK, bold=True)
    c.text(778, 179, "Website HTTP + saved session", 15, MUTED)
    c.pill(778, 211, 90, "QWEN OK", "#e6f3e9", GREEN)
    c.text(884, 216, "DeepSeek: user is muted", 11, MUTED)

    c.rect(757, 281, 304, 150, "#ffffff", "#cfddd5", 14)
    c.text(778, 298, "GLM / Kimi", 22, INK, bold=True)
    c.text(778, 335, "Signed-in Safari + Userscripts", 15, MUTED)
    c.pill(778, 367, 90, "VERIFIED", "#e6f3e9", GREEN)
    c.text(778, 410, "Grok / Mistral: experimental on this path", 11, ORANGE)

    c.rect(757, 469, 304, 125, "#eeeee9", "#d6d9d0", 14)
    c.text(778, 486, "Gemini / Antigravity", 22, INK, bold=True)
    c.text(778, 522, "Official CLI + eligibility check", 15, MUTED)
    c.pill(778, 554, 225, "REGION BLOCKED IN TEST", "#e3e4dd", MUTED)

    c.line([(646, 297), (698, 297), (698, 188), (745, 188)], GREEN, arrow=True)
    c.line([(646, 326), (730, 326), (730, 352), (745, 352)], GREEN, arrow=True)
    c.line([(646, 358), (698, 358), (698, 530), (745, 530)], "#a1aaa3", arrow=True)

    c.rect(40, 491, 606, 104, "#e8ece7", radius=14)
    c.text(59, 506, "Your project files", 20, INK, bold=True)
    c.text(59, 541, "Tools execute here. Read content goes to the selected provider.", 15, MUTED)
    c.line([(159, 427), (159, 479)], MUTED)
    c.text(177, 446, "Tool results", 13, MUTED)

    c.line([(40, 623), (1061, 623)], "#d1d8d2", 1)
    c.text(40, 639, "Text only · opt-in adapters · website account limits apply", 13, MUTED)
    c.text(865, 639, "Validation: 2026-10-02", 13, MUTED)
    c.save("bridge-architecture")


if __name__ == "__main__":
    hero()
    architecture()
    print("Rendered hero and architecture SVGs with PNG previews")
