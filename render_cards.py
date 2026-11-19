#!/usr/bin/env python3
"""Render the animated SVG cards for the GitHub profile README.

    python render_cards.py            # animated cards -> cards/, README.md updated
    STATIC=1 python render_cards.py   # final frame only -> preview/ (+ preview/index.html)

Data comes from data/contributions.json, the raw response of data/query.graphql.
The GitHub Action refreshes it daily. To get it locally:

    gh api graphql -f login=GustavoResendeDev -F query=@data/query.graphql > data/contributions.json

Needs: pip install fonttools pillow   (pyftsubset ships with fonttools)

The cards are shown through <img>, so they only use what works there: one inline
<style> with CSS keyframes, SMIL <animate> elements, and fonts subset with
pyftsubset and embedded as base64 WOFF data URIs. No scripts, no external files.
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parent
STATIC = os.environ.get("STATIC", "") not in ("", "0", "false", "no")
OUT = ROOT / ("preview" if STATIC else "cards")
DATA = ROOT / "data" / "contributions.json"
README = ROOT / "README.md"

# ─── Profile content ─────────────────────────────────────────────────────────
PROFILE = {
    "name": ("Gustavo Rodrigues", "Resende"),          # line 1 white, line 2 cyan
    "badge": "Aberto a colaborações",
    "tagline": "Estudante de Engenharia de Software construindo a própria fábrica de software.",
    "code": '> gustavo.build({ foco: "fábrica de software" })',
    "link": "https://gustavoresendedev.github.io/",
    # Optional portrait: drop assets/portrait.jpg (or .png/.webp) into the repo.
    "portrait": ["assets/portrait.jpg", "assets/portrait.png", "assets/portrait.webp"],
    "monogram": "GR",
}

TILES = [  # exactly three, shown in a row
    {"slug": "site", "icon": "globe", "label": "Site", "title": "NEXORA",
     "sub": "gustavoresendedev.github.io", "url": "https://gustavoresendedev.github.io/"},
    {"slug": "instagram", "icon": "camera", "label": "Instagram", "title": "@gustavoresende.dev",
     "sub": "Bastidores e estudos", "url": "https://instagram.com/gustavoresende.dev"},
    {"slug": "whatsapp", "icon": "chat", "label": "WhatsApp", "title": "Fale comigo",
     "sub": "Projetos e parcerias", "url": "https://wa.me/message/OCB7LETXQXXVC1"},
]

# Repos to feature, in order. Leave empty to use your pinned repos instead.
FEATURED = ["NexoraCodeBR.com", "Site-Distribuidora-Lopes-V-1.1.0", "V2.AtividadeEscolar"]
# Optional description overrides (used when a repo has no description on GitHub).
DESCRIPTIONS = {
    "NexoraCodeBR.com": "Site da NEXORA: websites, cardápios QR Code, automação e suporte 24/7.",
}

# ─── Brand (from the NEXORA site: input.css) ─────────────────────────────────
C = {
    "o950": "#030508", "o900": "#080B10", "o800": "#0D1117", "o700": "#161B22", "o600": "#21262D",
    "glow": "#00E5FF", "electric": "#00D2FF", "deep": "#0088A8",
    "white": "#FFFFFF", "s300": "#CBD5E1", "s400": "#94A3B8", "s500": "#64748B",
}
LEVELS = {  # contribution graph, in the brand cyan
    "NONE": C["o700"], "FIRST_QUARTILE": "#063A47", "SECOND_QUARTILE": C["deep"],
    "THIRD_QUARTILE": "#00B8D9", "FOURTH_QUARTILE": C["glow"],
}
W = 840  # width of full-width cards (README shows them at 100%)

# Faces: family, file, variation axes
FACES = {
    "display": ("Syne", "Syne-VF.ttf", {"wght": 800}),
    "label": ("Space Grotesk", "SpaceGrotesk-VF.ttf", {"wght": 500}),
    "num": ("Space Grotesk Bold", "SpaceGrotesk-VF.ttf", {"wght": 700}),
    "body": ("Inter", "Inter-VF.ttf", {"wght": 400, "opsz": 14}),
}
FALLBACK = {"display": "sans-serif", "label": "monospace", "num": "monospace", "body": "sans-serif"}

MONTHS = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]

# Page timeline: each card's animations start at its base delay, so the README
# plays top to bottom.
T_HERO, T_TILES, T_ACTIVITY, T_REPOS, T_REPO_STEP = 0.0, 0.45, 0.95, 1.55, 0.22


# ─── Fonts ───────────────────────────────────────────────────────────────────
class Fonts:
    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cards-fonts-"))
        self.static: dict[str, Path] = {}
        self.metrics: dict[str, tuple] = {}
        self.cache: dict[tuple, str] = {}
        self.pyftsubset = shutil.which("pyftsubset")

    def _load(self, face: str) -> None:
        if face in self.static:
            return
        _, file, axes = FACES[face]
        vf = TTFont(ROOT / "fonts" / file)
        font = instancer.instantiateVariableFont(vf, axes)
        path = self.tmp / f"{face}.ttf"
        font.save(path)
        self.static[face] = path
        self.metrics[face] = (font.getBestCmap(), font["hmtx"].metrics, font["head"].unitsPerEm)

    def width(self, face: str, size: float, text: str, ls: float = 0) -> float:
        self._load(face)
        cmap, hmtx, upm = self.metrics[face]
        units = sum(hmtx.get(cmap.get(ord(ch), ".notdef"), (upm // 2, 0))[0] for ch in text)
        return units * size / upm + ls * len(text)

    def woff_b64(self, face: str, chars: set[str]) -> str:
        self._load(face)
        text = "".join(sorted(chars | {" "}))
        key = (face, text)
        if key not in self.cache:
            txt = self.tmp / "chars.txt"
            txt.write_text(text, encoding="utf-8")
            out = self.tmp / f"{face}.woff"
            cmd = [self.pyftsubset] if self.pyftsubset else [sys.executable, "-m", "fontTools.subset"]
            subprocess.run(cmd + [
                str(self.static[face]), f"--text-file={txt}", f"--output-file={out}",
                "--flavor=woff", "--layout-features=kern,liga,calt", "--no-hinting",
                "--desubroutinize", "--name-IDs=1,2", "--notdef-outline",
            ], check=True)
            self.cache[key] = base64.b64encode(out.read_bytes()).decode()
        return self.cache[key]


FONTS = Fonts()


def fit(text: str, face: str, size: float, maxw: float, ls: float = 0, min_size: float = 9) -> float:
    while size > min_size and FONTS.width(face, size, text, ls) > maxw:
        size -= 0.5
    return size


def wrap(text: str, face: str, size: float, maxw: float, max_lines: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for wd in words:
        trial = f"{cur} {wd}".strip()
        if FONTS.width(face, size, trial) <= maxw:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        last = " ".join(lines[max_lines - 1:])
        while last and FONTS.width(face, size, last + "…") > maxw:
            last = last[:-1].rstrip()
        lines = lines[: max_lines - 1] + [last + "…"]
    return lines


# ─── Card builder ────────────────────────────────────────────────────────────
CSS_ANIM = """
.a{animation-duration:.9s;animation-timing-function:cubic-bezier(.16,1,.3,1);animation-fill-mode:both}
.rise{animation-name:rise}.fade{animation-name:fade}
.grow{animation-name:grow;transform-box:fill-box;transform-origin:50% 100%}
.pop{animation-name:pop;transform-box:fill-box;transform-origin:50% 50%}
.wipe{animation-name:wipe;transform-box:fill-box;transform-origin:0 50%}
.drop{animation-name:drop;transform-box:fill-box;transform-origin:50% 0}
.nudge{animation:nudge 2.6s ease-in-out infinite}
@keyframes rise{from{opacity:0;transform:translateY(14px)}}
@keyframes fade{from{opacity:0}}
@keyframes grow{from{transform:scaleY(0)}}
@keyframes pop{from{opacity:0;transform:scale(.2)}}
@keyframes wipe{from{transform:scaleX(0)}}
@keyframes drop{from{transform:scaleY(0)}}
@keyframes nudge{0%,70%,100%{transform:translate(0,0)}80%{transform:translate(2px,-2px)}}
.s{display:none}
@media (prefers-reduced-motion:reduce){*{animation:none!important}.m{display:none}.s{display:inline}}
"""
# STATIC=1: the final frame. Same end state as prefers-reduced-motion.
CSS_STATIC = "*{animation:none!important}.m{display:none}"


class Card:
    def __init__(self, name: str, w: int, h: int, t0: float, title: str) -> None:
        self.name, self.w, self.h, self.t0, self.title = name, w, h, t0, title
        self.defs: list[str] = []
        self.body: list[str] = []
        self.used: dict[str, set[str]] = defaultdict(set)

    # animation attributes: class + delay relative to this card's start
    def anim(self, kind: str, d: float = 0, dur: float | None = None) -> str:
        if STATIC:
            return ""
        style = f"animation-delay:{self.t0 + d:.2f}s" + (f";animation-duration:{dur}s" if dur else "")
        return f' class="a {kind}" style="{style}"'

    def at(self, d: float) -> str:
        """SMIL begin time for a loop that starts once the intro is done."""
        return f"{self.t0 + d:.2f}s"

    def motion(self, moving: str, still: str = "") -> None:
        """SMIL-animated content, with a still version for reduced motion / STATIC."""
        if STATIC:
            self.body.append(still)
        else:
            self.body.append(f'<g class="m">{moving}</g>' + (f'<g class="s">{still}</g>' if still else ""))

    def text(self, x: float, y: float, s: str, face: str, size: float, fill: str,
             anchor: str = "start", ls: float = 0, extra: str = "") -> str:
        self.used[face].update(s)
        family = FACES[face][0]
        lsp = f' letter-spacing="{ls}"' if ls else ""
        return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="\'{family}\',{FALLBACK[face]}" '
                f'font-size="{size}" fill="{fill}" text-anchor="{anchor}"{lsp}{extra}>{escape(s)}</text>')

    def add(self, s: str) -> None:
        self.body.append(s)

    def frame(self, rx: int = 18) -> None:
        """Panel background shared by every card."""
        self.defs.append(
            f'<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
            f'<stop offset="0" stop-color="{C["o700"]}"/><stop offset="1" stop-color="{C["o900"]}"/></linearGradient>'
            f'<clipPath id="clip"><rect width="{self.w}" height="{self.h}" rx="{rx}"/></clipPath>')
        self.add(f'<rect width="{self.w}" height="{self.h}" rx="{rx}" fill="url(#bg)"/>')
        self.border_rx = rx

    def eyebrow(self, x: float, y: float, s: str, d: float = 0) -> None:
        self.add(f'<g{self.anim("rise", d)}>'
                 + self.text(x, y, "// " + s.upper(), "label", 11, C["glow"], ls=2.2) + "</g>")

    def render(self) -> str:
        faces = []
        for face, chars in self.used.items():
            fam = FACES[face][0]
            faces.append(f"@font-face{{font-family:'{fam}';src:url(data:font/woff;base64,"
                         f"{FONTS.woff_b64(face, chars)}) format('woff')}}")
        css = "".join(faces) + (CSS_STATIC if STATIC else CSS_ANIM)
        border = (f'<rect x=".5" y=".5" width="{self.w - 1}" height="{self.h - 1}" rx="{self.border_rx - .5}" '
                  f'fill="none" stroke="{C["glow"]}" stroke-opacity=".16"/>')
        return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
                f'width="{self.w}" height="{self.h}" viewBox="0 0 {self.w} {self.h}" role="img" '
                f'aria-label="{escape(self.title)}"><title>{escape(self.title)}</title>'
                f'<style>{css}</style><defs>{"".join(self.defs)}</defs>'
                f'<g clip-path="url(#clip)">{"".join(self.body)}</g>{border}</svg>\n')

    def save(self) -> str:
        (OUT / f"{self.name}.svg").write_text(self.render(), encoding="utf-8")
        return f"{self.name}.svg"


# ─── Icons (24-unit stroke paths, drawn here rather than brand logos) ────────
ICONS = {
    "globe": '<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18M5 7.5h14M5 16.5h14"/>',
    "camera": '<path d="M3 8.5h4l2-3h6l2 3h4V19H3z"/><circle cx="12" cy="13.2" r="3.6"/>',
    "chat": '<path d="M4 5h16v11H10l-5 4v-4H4z"/><path d="M8 9.5h8M8 12.5h5"/>',
    "arrow": '<path d="M7 17L17 7M9 7h8v8"/>',
    "fork": '<circle cx="6" cy="5" r="2"/><circle cx="18" cy="5" r="2"/><circle cx="12" cy="19" r="2"/><path d="M6 7v2a3 3 0 003 3h6a3 3 0 003-3V7M12 12v5"/>',
}


def icon(name: str, x: float, y: float, size: float, color: str, width: float = 1.7) -> str:
    k = size / 24
    return (f'<g transform="translate({x:.1f} {y:.1f}) scale({k:.3f})" fill="none" stroke="{color}" '
            f'stroke-width="{width / k:.2f}" stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</g>')


def star(cx: float, cy: float, r: float, color: str) -> str:
    pts = []
    for i in range(10):
        rad = r if i % 2 == 0 else r * 0.45
        a = -math.pi / 2 + i * math.pi / 5
        pts.append(f"{cx + rad * math.cos(a):.2f},{cy + rad * math.sin(a):.2f}")
    return f'<polygon points="{" ".join(pts)}" fill="{color}"/>'


def glow_filter(id_: str = "glow", std: float = 6) -> str:
    return (f'<filter id="{id_}" x="-50%" y="-50%" width="200%" height="200%">'
            f'<feGaussianBlur stdDeviation="{std}" result="b"/>'
            f'<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>')


def grid_pattern(card: Card, step: int = 28) -> None:
    card.defs.append(
        f'<pattern id="grid" width="{step}" height="{step}" patternUnits="userSpaceOnUse">'
        f'<path d="M{step} 0H0V{step}" fill="none" stroke="{C["glow"]}" stroke-opacity=".06"/></pattern>'
        f'<radialGradient id="fadeG" cx=".8" cy=".2" r=".9"><stop offset="0" stop-color="#fff"/>'
        f'<stop offset="1" stop-color="#fff" stop-opacity="0"/></radialGradient>'
        f'<mask id="gridMask"><rect width="{card.w}" height="{card.h}" fill="url(#fadeG)"/></mask>'
        f'<radialGradient id="halo" cx=".5" cy=".5" r=".5"><stop offset="0" stop-color="{C["glow"]}" stop-opacity=".22"/>'
        f'<stop offset="1" stop-color="{C["glow"]}" stop-opacity="0"/></radialGradient>')
    card.add(f'<rect width="{card.w}" height="{card.h}" fill="url(#grid)" mask="url(#gridMask)"/>')


# ─── Data ────────────────────────────────────────────────────────────────────
def load_data() -> dict:
    if not DATA.exists():
        sys.exit(f"Missing {DATA.relative_to(ROOT)}. Fetch it with:\n"
                 "  gh api graphql -f login=<you> -F query=@data/query.graphql > data/contributions.json")
    raw = json.loads(DATA.read_text(encoding="utf-8"))
    if raw.get("errors"):
        sys.exit(f"GraphQL errors in {DATA.name}: {raw['errors']}")
    user = raw["data"]["user"]
    cal = user["contributionsCollection"]["contributionCalendar"]
    weeks = [w["contributionDays"] for w in cal["weeks"]]
    days = {date.fromisoformat(d["date"]): d["contributionCount"] for w in weeks for d in w}
    today = max(days)

    # streaks: today with zero contributions doesn't break the current streak yet
    cur, d = 0, today if days[today] > 0 else today - timedelta(days=1)
    while days.get(d, 0) > 0:
        cur, d = cur + 1, d - timedelta(days=1)
    longest = run = 0
    for d in sorted(days):
        run = run + 1 if days[d] > 0 else 0
        longest = max(longest, run)
    busiest_day = max(days, key=lambda k: (days[k], k))

    months = []
    for i in range(11, -1, -1):
        y, m = divmod(today.year * 12 + today.month - 1 - i, 12)
        m += 1
        months.append(((y, m), sum(c for dd, c in days.items() if (dd.year, dd.month) == (y, m))))

    repos = {r["name"]: r for r in user["repositories"]["nodes"]}
    pinned = [r for r in user["pinnedItems"]["nodes"] if r]
    if FEATURED:
        featured = [repos[n] for n in FEATURED if n in repos]
        missing = [n for n in FEATURED if n not in repos]
        if missing:
            print(f"warning: featured repos not found (private/renamed?): {missing}", file=sys.stderr)
    else:
        featured = pinned or [r for r in repos.values() if not r["isFork"]][:3]

    return {
        "login": user["login"], "weeks": weeks, "days": days, "today": today,
        "total_year": cal["totalContributions"], "current": cur, "longest": longest,
        "active": sum(1 for c in days.values() if c > 0), "n_days": len(days),
        "busiest": (busiest_day, days[busiest_day]), "months": months, "featured": featured,
    }


def fmt_date(d: date, year: bool = False) -> str:
    return f"{d.day} {MONTHS[d.month - 1]}" + (f" {d.year}" if year else "")


def plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def ago(iso: str, today: date) -> str:
    n = (today - datetime.fromisoformat(iso.replace("Z", "+00:00")).date()).days
    if n <= 0:
        return "atualizado hoje"
    if n == 1:
        return "atualizado ontem"
    if n < 31:
        return f"atualizado há {n} dias"
    if n < 365:
        return f"atualizado há {plural(n // 30, 'mês', 'meses')}"
    return f"atualizado há {plural(n // 365, 'ano', 'anos')}"


# ─── Cards ───────────────────────────────────────────────────────────────────
def portrait_data_uri() -> str | None:
    for rel in PROFILE["portrait"]:
        p = ROOT / rel
        if p.exists():
            from PIL import Image, ImageOps
            im = ImageOps.exif_transpose(Image.open(p)).convert("RGB")
            side = min(im.size)
            im = im.crop(((im.width - side) // 2, (im.height - side) // 2,
                          (im.width + side) // 2, (im.height + side) // 2)).resize((360, 360), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=84, optimize=True, progressive=True)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    return None


def hero() -> str:
    H = 290
    l1, l2 = PROFILE["name"]
    card = Card("hero", W, H, T_HERO, f"{l1} {l2}: {PROFILE['tagline']}")
    card.frame()
    grid_pattern(card)
    card.defs.append(glow_filter("glow", 10))
    card.defs.append(f'<linearGradient id="scan" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{C["glow"]}" stop-opacity="0"/>'
                     f'<stop offset="1" stop-color="{C["glow"]}" stop-opacity=".09"/></linearGradient>')
    card.add(f'<circle cx="700" cy="40" r="260" fill="url(#halo)"/>')

    # scanline loop (like the site's animate-scanline)
    card.motion(f'<rect x="0" y="-60" width="{W}" height="60" fill="url(#scan)">'
                f'<animateTransform attributeName="transform" type="translate" from="0 0" to="0 {H + 60}" '
                f'dur="7s" begin="{card.at(1.6)}" repeatCount="indefinite"/></rect>')

    x, maxw = 44, 520
    # availability badge with pulsing dot
    badge = PROFILE["badge"].upper()
    bw = FONTS.width("label", 11, badge, 2) + 44
    card.add(f'<g{card.anim("rise", .05)}><rect x="{x}" y="34" width="{bw:.0f}" height="28" rx="14" '
             f'fill="{C["glow"]}" fill-opacity=".07" stroke="{C["glow"]}" stroke-opacity=".3"/>'
             + card.text(x + 30, 52, badge, "label", 11, C["glow"], ls=2) + "</g>")
    dot = f'<circle cx="{x + 16}" cy="48" r="3.5" fill="{C["glow"]}"/>'
    card.add(f'<g{card.anim("pop", .25)}>{dot}</g>')
    card.motion(f'<circle cx="{x + 16}" cy="48" r="3.5" fill="none" stroke="{C["glow"]}" stroke-opacity="0">'
                f'<animate attributeName="r" values="3.5;10" dur="2s" begin="{card.at(1)}" repeatCount="indefinite"/>'
                f'<animate attributeName="stroke-opacity" values=".8;0" dur="2s" begin="{card.at(1)}" repeatCount="indefinite"/></circle>')

    s1 = fit(l1.upper(), "display", 44, maxw)
    s2 = fit(l2.upper(), "display", 44, maxw)
    size = min(s1, s2)
    card.add(f'<g{card.anim("rise", .15)}>' + card.text(x, 118, l1.upper(), "display", size, C["white"]) + "</g>")
    card.add(f'<g{card.anim("rise", .28)}>'
             + card.text(x, 118 + size * 1.08, l2.upper(), "display", size, C["glow"], extra=' filter="url(#glow)"')
             + "</g>")

    y = 118 + size * 1.08 + 40
    for i, line in enumerate(wrap(PROFILE["tagline"], "body", 16, maxw, 2)):
        card.add(f'<g{card.anim("rise", .42 + i * .08)}>' + card.text(x, y + i * 24, line, "body", 16, C["s300"]) + "</g>")

    code = PROFILE["code"]
    cy = H - 30
    cw = FONTS.width("label", 13, code)
    card.add(f'<g{card.anim("fade", .7)}>' + card.text(x, cy, code, "label", 13, C["deep"]))
    cursor = f'<rect x="{x + cw + 4:.1f}" y="{cy - 12}" width="8" height="15" fill="{C["glow"]}"'
    card.motion(cursor + f'><animate attributeName="opacity" values="1;1;0;0" keyTimes="0;.5;.5;1" dur="1.1s" '
                f'begin="{card.at(.9)}" repeatCount="indefinite"/></rect>', cursor + "/>")
    card.add("</g>")

    # portrait or monogram, with a slowly spinning dashed ring (site: animate-spin-slow)
    pcx, pcy, r = 700, H / 2, 86
    card.defs.append(f'<clipPath id="face"><circle cx="{pcx}" cy="{pcy}" r="{r}"/></clipPath>')
    uri = portrait_data_uri()
    inner = (f'<circle cx="{pcx}" cy="{pcy}" r="{r}" fill="{C["o800"]}"/>'
             + (f'<image href="{uri}" xlink:href="{uri}" x="{pcx - r}" y="{pcy - r}" width="{2 * r}" height="{2 * r}" '
                f'clip-path="url(#face)" preserveAspectRatio="xMidYMid slice"/>' if uri else
                card.text(pcx, pcy + 20, PROFILE["monogram"], "display", 58, C["glow"], anchor="middle",
                          extra=' filter="url(#glow)"'))
             + f'<circle cx="{pcx}" cy="{pcy}" r="{r}" fill="none" stroke="{C["glow"]}" stroke-opacity=".55" stroke-width="1.5"/>')
    card.add(f'<g{card.anim("pop", .3, 1.1)}>{inner}</g>')
    ring = (f'<circle cx="{pcx}" cy="{pcy}" r="{r + 16}" fill="none" stroke="{C["glow"]}" stroke-opacity=".45" '
            f'stroke-width="1.2" stroke-dasharray="3 9" stroke-linecap="round"')
    card.add(f'<g{card.anim("fade", .6)}>')
    card.motion(ring + f'><animateTransform attributeName="transform" type="rotate" from="0 {pcx} {pcy}" '
                f'to="360 {pcx} {pcy}" dur="24s" begin="{card.at(.6)}" repeatCount="indefinite"/></circle>', ring + "/>")
    card.add("</g>")
    return card.save()


def tiles() -> list[tuple[str, dict]]:
    out = []
    TW, TH = 270, 150
    for i, t in enumerate(TILES):
        card = Card(f"tile-{t['slug']}", TW, TH, T_TILES + i * .15, f"{t['label']}: {t['title']}")
        card.frame(14)
        card.defs.append(f'<radialGradient id="halo" cx=".5" cy=".5" r=".5"><stop offset="0" stop-color="{C["glow"]}" stop-opacity=".16"/>'
                         f'<stop offset="1" stop-color="{C["glow"]}" stop-opacity="0"/></radialGradient>'
                         f'<linearGradient id="line" x1="0" x2="1"><stop offset="0" stop-color="{C["glow"]}"/>'
                         f'<stop offset="1" stop-color="{C["glow"]}" stop-opacity="0"/></linearGradient>')
        card.add(f'<circle cx="30" cy="30" r="110" fill="url(#halo)"/>')
        card.add(f'<g{card.anim("pop", 0)}><rect x="20" y="20" width="44" height="44" rx="11" fill="{C["glow"]}" '
                 f'fill-opacity=".08" stroke="{C["glow"]}" stroke-opacity=".28"/>'
                 + icon(t["icon"], 30, 30, 24, C["glow"]) + "</g>")
        card.add(f'<g{card.anim("fade", .2)}><g class="{"" if STATIC else "nudge"}" style="animation-delay:{card.t0 + 1.2:.2f}s">'
                 + icon("arrow", TW - 42, 22, 20, C["s400"], 1.6) + "</g></g>")
        card.add(f'<g{card.anim("rise", .12)}>' + card.text(20, 94, t["label"].upper(), "label", 11, C["glow"], ls=2.2) + "</g>")
        ts = fit(t["title"], "display", 18, TW - 40)
        card.add(f'<g{card.anim("rise", .2)}>' + card.text(20, 118, t["title"], "display", ts, C["white"]) + "</g>")
        ss = fit(t["sub"], "body", 12.5, TW - 40)
        card.add(f'<g{card.anim("rise", .28)}>' + card.text(20, 137, t["sub"], "body", ss, C["s400"]) + "</g>")
        card.add(f'<rect x="0" y="{TH - 2}" width="{TW}" height="2" fill="url(#line)"{card.anim("wipe", .35, 1.2)}/>')
        out.append((card.save(), t))
    return out


def stat_box(card: Card, x: float, y: float, w: float, label: str, value: str, unit: str, d: float) -> None:
    card.add(f'<g{card.anim("rise", d)}>'
             f'<rect x="{x}" y="{y}" width="{w:.1f}" height="76" rx="12" fill="{C["o950"]}" fill-opacity=".55" '
             f'stroke="{C["glow"]}" stroke-opacity=".12"/>'
             + card.text(x + 16, y + 26, label.upper(), "label", 10.5, C["s400"], ls=1.6)
             + card.text(x + 16, y + 60, value, "display", 26, C["white"])
             + card.text(x + 22 + FONTS.width("display", 26, value), y + 60, unit, "body", 12.5, C["s400"])
             + "</g>")


def activity(D: dict) -> str:
    H = 380
    months = D["months"]
    total = sum(v for _, v in months)
    bday, bcount = D["busiest"]
    title = (f"{total} contribuições nos últimos 12 meses. Sequência atual: {D['current']} dias; "
             f"maior sequência: {D['longest']} dias; {D['active']} dias ativos; " +
             (f"dia mais intenso: {fmt_date(bday, True)} com {bcount}." if bcount else "nenhum dia com contribuições."))
    card = Card("activity", W, H, T_ACTIVITY, title)
    card.frame()
    card.defs.append(f'<linearGradient id="bar" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="{C["deep"]}" stop-opacity=".55"/>'
                     f'<stop offset="1" stop-color="{C["glow"]}"/></linearGradient>'
                     f'<linearGradient id="barDim" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="{C["deep"]}" stop-opacity=".25"/>'
                     f'<stop offset="1" stop-color="{C["deep"]}"/></linearGradient>' + glow_filter("glow", 5))
    P = 32
    card.eyebrow(P, 44, "Atividade no GitHub")
    card.add(f'<g{card.anim("rise", .08)}>' + card.text(P, 80, f"{total} contribuições", "display", 26, C["white"]) + "</g>")
    card.add(f'<g{card.anim("rise", .12)}>' + card.text(W - P, 80, "por mês · últimos 12 meses", "label", 12, C["s400"], anchor="end") + "</g>")

    gap, bw = 12, (W - 2 * P - 3 * 12) / 4
    stats = [
        ("Sequência atual", str(D["current"]), "dias" if D["current"] != 1 else "dia"),
        ("Maior sequência", str(D["longest"]), "dias" if D["longest"] != 1 else "dia"),
        ("Dias ativos", str(D["active"]), "no ano"),
        ("Dia mais intenso", str(bcount), f"em {fmt_date(bday)}") if bcount else ("Dia mais intenso", "–", ""),
    ]
    for i, (lab, val, unit) in enumerate(stats):
        stat_box(card, P + i * (bw + gap), 104, bw, lab, val, unit, .18 + i * .07)

    # bar chart
    top, base = 222, 336
    hmax = base - top - 18
    vmax = max(1, max(v for _, v in months))
    slot = (W - 2 * P) / 12
    barw = 30
    for k in (1, 2, 3):  # faint gridlines
        gy = base - hmax * k / 3
        card.add(f'<line x1="{P}" x2="{W - P}" y1="{gy:.1f}" y2="{gy:.1f}" stroke="{C["s500"]}" stroke-opacity=".18" '
                 f'stroke-dasharray="2 6"{card.anim("fade", .4)}/>')
    card.add(f'<line x1="{P}" x2="{W - P}" y1="{base}" y2="{base}" stroke="{C["glow"]}" stroke-opacity=".25"{card.anim("wipe", .4, 1.2)}/>')
    imax = max(range(12), key=lambda i: (months[i][1], i))
    for i, ((y, m), v) in enumerate(months):
        cx = P + slot * i + slot / 2
        h = max(3, hmax * v / vmax) if v else 3
        d = .55 + i * .06
        best = i == imax and v > 0
        fill = "url(#bar)" if v else C["o600"]
        if v and not best:
            fill = "url(#barDim)" if v < vmax * .35 else "url(#bar)"
        card.add(f'<rect x="{cx - barw / 2:.1f}" y="{base - h:.1f}" width="{barw}" height="{h:.1f}" rx="5" fill="{fill}"'
                 + (' filter="url(#glow)"' if best else "") + f'{card.anim("grow", d, 1.1)}/>')
        card.add(f'<g{card.anim("rise", d + .45)}>' + card.text(cx, base - h - 8, str(v), "num", 11,
                                                             C["glow"] if best else C["s300"], anchor="middle") + "</g>")
        lab = MONTHS[m - 1] + (f" {str(y)[2:]}" if m == 1 or i == 0 else "")
        card.add(f'<g{card.anim("fade", d)}>' + card.text(cx, base + 22, lab, "label", 11,
                                                       C["white"] if best else C["s400"], anchor="middle") + "</g>")
        if best:  # pulse above the busiest month
            py = base - h - 26
            dot = f'<circle cx="{cx:.1f}" cy="{py:.1f}" r="3" fill="{C["glow"]}"/>'
            card.add(f'<g{card.anim("pop", d + .7)}>{dot}</g>')
            card.motion(f'<circle cx="{cx:.1f}" cy="{py:.1f}" r="3" fill="none" stroke="{C["glow"]}" stroke-opacity="0">'
                        f'<animate attributeName="r" values="3;11" dur="1.8s" begin="{card.at(d + 1.2)}" repeatCount="indefinite"/>'
                        f'<animate attributeName="stroke-opacity" values=".9;0" dur="1.8s" begin="{card.at(d + 1.2)}" repeatCount="indefinite"/>'
                        f'</circle>')
    return card.save()


def repo_card(r: dict, i: int, D: dict) -> str:
    H = 172
    name = r["name"]
    desc = r.get("description") or DESCRIPTIONS.get(name) or "Sem descrição."
    card = Card(f"repo-{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')}", W, H,
                T_REPOS + i * T_REPO_STEP, f"Repositório {name}: {desc}")
    card.frame()
    card.defs.append(f'<linearGradient id="accent" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{C["glow"]}"/>'
                     f'<stop offset="1" stop-color="{C["deep"]}" stop-opacity=".2"/></linearGradient>'
                     f'<linearGradient id="sweep" x1="0" x2="1"><stop offset="0" stop-color="{C["glow"]}" stop-opacity="0"/>'
                     f'<stop offset=".5" stop-color="{C["glow"]}" stop-opacity=".07"/><stop offset="1" stop-color="{C["glow"]}" stop-opacity="0"/></linearGradient>')
    card.add(f'<rect x="0" y="0" width="4" height="{H}" fill="url(#accent)"{card.anim("drop", 0, 1.2)}/>')
    num = f"{i + 1:02d}"
    card.add(f'<g{card.anim("fade", .3, 1.4)}>' + card.text(W - 40, 128, num, "display", 104, "none", anchor="end",
                                                          extra=f' stroke="{C["glow"]}" stroke-opacity=".12" stroke-width="1.2"') + "</g>")
    # one slow light sweep after the intro, then every 9s
    card.motion(f'<rect x="-300" y="0" width="300" height="{H}" fill="url(#sweep)">'
                f'<animate attributeName="x" values="-300;{W};{W}" keyTimes="0;.35;1" dur="9s" begin="{card.at(1.2)}" repeatCount="indefinite"/></rect>')
    P = 36
    card.eyebrow(P, 40, f"Projeto {num}", .05)
    ns = fit(name, "display", 24, W - 2 * P - 160)
    card.add(f'<g{card.anim("rise", .12)}>' + card.text(P, 74, name, "display", ns, C["white"]) + "</g>")
    card.add(f'<g{card.anim("fade", .3)}><g class="{"" if STATIC else "nudge"}" style="animation-delay:{card.t0 + 1.4:.2f}s">'
             + icon("arrow", W - 58, 22, 22, C["s400"], 1.6) + "</g></g>")
    for j, line in enumerate(wrap(desc, "body", 14.5, W - 2 * P - 150, 2)):
        card.add(f'<g{card.anim("rise", .2 + j * .06)}>' + card.text(P, 102 + j * 21, line, "body", 14.5, C["s300"]) + "</g>")

    # meta row
    y, x = 150, P
    parts = []
    lang = r.get("primaryLanguage")
    if lang:
        parts.append(f'<circle cx="{x + 5}" cy="{y - 4}" r="5" fill="{lang.get("color") or C["glow"]}"/>'
                     + card.text(x + 16, y, lang["name"], "label", 12, C["s300"]))
        x += 16 + FONTS.width("label", 12, lang["name"]) + 22
    parts.append(star(x + 6, y - 4.5, 6.5, C["glow"]) + card.text(x + 18, y, str(r["stargazerCount"]), "num", 12, C["s300"]))
    x += 18 + FONTS.width("num", 12, str(r["stargazerCount"])) + 22
    parts.append(icon("fork", x, y - 11, 13, C["glow"], 1.5) + card.text(x + 19, y, str(r["forkCount"]), "num", 12, C["s300"]))
    for j, p in enumerate(parts):
        card.add(f'<g{card.anim("rise", .34 + j * .06)}>{p}</g>')
    card.add(f'<g{card.anim("fade", .5)}>' + card.text(W - P, y, ago(r["pushedAt"], D["today"]), "label", 12, C["s400"], anchor="end") + "</g>")
    return card.save(), r


def contributions(D: dict, t0: float) -> str:
    H = 262
    weeks = D["weeks"]
    title = f"Gráfico de contribuições: {D['total_year']} contribuições no último ano."
    card = Card("contributions", W, H, t0, title)
    card.frame()
    card.defs.append(f'<linearGradient id="sweep" x1="0" x2="1"><stop offset="0" stop-color="{C["glow"]}" stop-opacity="0"/>'
                     f'<stop offset=".5" stop-color="{C["glow"]}" stop-opacity=".18"/><stop offset="1" stop-color="{C["glow"]}" stop-opacity="0"/></linearGradient>')
    P = 32
    card.eyebrow(P, 44, "Contribuições")
    card.add(f'<g{card.anim("rise", .08)}>' + card.text(P, 78, f"{D['total_year']} no último ano", "display", 22, C["white"]) + "</g>")

    gx, gy = 64, 112
    pitch = min(14.0, (W - gx - P) / len(weeks))
    cell = pitch - 3
    # month labels at the first column of each month
    last_m = None
    for ci, wk in enumerate(weeks):
        first = date.fromisoformat(wk[0]["date"])
        m = next((date.fromisoformat(d["date"]).month for d in wk if date.fromisoformat(d["date"]).day <= 7), None)
        if m is not None and m != last_m and ci < len(weeks) - 1:
            if ci > 0 or first.day <= 7:
                card.add(f'<g{card.anim("fade", .2 + ci * .012)}>'
                         + card.text(gx + ci * pitch, gy - 10, MONTHS[m - 1], "label", 10.5, C["s400"]) + "</g>")
            last_m = m
    for row, lab in ((1, "seg"), (3, "qua"), (5, "sex")):
        card.add(f'<g{card.anim("fade", .2)}>' + card.text(gx - 10, gy + row * pitch + cell - 1.5, lab, "label", 10, C["s500"], anchor="end") + "</g>")
    for ci, wk in enumerate(weeks):
        for d in wk:
            row = d["weekday"]
            x, y = gx + ci * pitch, gy + row * pitch
            fill = LEVELS.get(d["contributionLevel"], C["o700"])
            card.add(f'<rect x="{x:.1f}" y="{y:.1f}" width="{cell:.1f}" height="{cell:.1f}" rx="2.5" fill="{fill}"'
                     f'{card.anim("pop", .25 + ci * .016 + row * .03, .6)}/>')
    gw = len(weeks) * pitch
    card.motion(f'<rect x="-200" y="{gy - 4}" width="120" height="{7 * pitch + 5:.1f}" fill="url(#sweep)">'
                f'<animate attributeName="x" values="-200;{gx + gw:.0f};{gx + gw:.0f}" keyTimes="0;.4;1" dur="10s" '
                f'begin="{card.at(2.2)}" repeatCount="indefinite"/></rect>')

    ly = H - 28
    card.add(f'<g{card.anim("fade", 1)}>' + card.text(P, ly, f"{D['active']} dias ativos · sequência atual de {plural(D['current'], 'dia', 'dias')}",
                                                   "label", 11.5, C["s400"]) + "</g>")
    lx = W - P - 5 * 15 - FONTS.width("label", 11, "Mais") - 8
    legend = card.text(lx - 8, ly, "Menos", "label", 11, C["s400"], anchor="end")
    for k, lvl in enumerate(LEVELS.values()):
        legend += f'<rect x="{lx + k * 15:.1f}" y="{ly - 10}" width="11" height="11" rx="2.5" fill="{lvl}"/>'
    legend += card.text(lx + 5 * 15 + 4, ly, "Mais", "label", 11, C["s400"])
    card.add(f'<g{card.anim("fade", 1.1)}>{legend}</g>')
    return card.save()


# ─── README / preview ────────────────────────────────────────────────────────
def markup(prefix: str, hero_f, tile_fs, act_f, repo_fs, contrib_f, D) -> str:
    l1, l2 = PROFILE["name"]
    rows = [f'<a href="{PROFILE["link"]}"><img src="{prefix}{hero_f}" width="100%" alt="{escape(l1 + " " + l2)}: {escape(PROFILE["tagline"])}"></a>',
            "", "<p>"]
    for f, t in tile_fs:
        rows.append(f'  <a href="{t["url"]}"><img src="{prefix}{f}" width="32.5%" alt="{escape(t["label"] + ": " + t["title"])}"></a>')
    rows += ["</p>", "",
             f'<img src="{prefix}{act_f}" width="100%" alt="{escape(_activity_alt(D))}">', ""]
    for f, r in repo_fs:
        desc = r.get("description") or DESCRIPTIONS.get(r["name"]) or ""
        rows.append(f'<a href="{r["url"]}"><img src="{prefix}{f}" width="100%" alt="{escape(r["name"])}: {escape(desc)}"></a>')
    rows += ["", f'<img src="{prefix}{contrib_f}" width="100%" alt="Gráfico de contribuições: {D["total_year"]} no último ano">']
    return "\n".join(rows)


def _activity_alt(D: dict) -> str:
    bday, bcount = D["busiest"]
    return (f"Contribuições por mês. Sequência atual: {D['current']} dias; maior: {D['longest']}; "
            f"{D['active']} dias ativos; dia mais intenso: {fmt_date(bday, True)} ({bcount}).")


def update_readme(block: str) -> None:
    start, end = "<!-- cards:start -->", "<!-- cards:end -->"
    new = f"{start}\n{block}\n{end}"
    text = README.read_text(encoding="utf-8") if README.exists() else f"{start}\n{end}\n"
    if start in text and end in text:
        text = re.sub(re.escape(start) + r".*?" + re.escape(end), lambda _: new, text, flags=re.S)
    else:
        text = new + "\n\n" + text
    README.write_text(text, encoding="utf-8")


def write_preview(block: str) -> None:
    (OUT / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Prévia do perfil</title>'
        '<style>body{margin:0;background:#0d1117;display:flex;justify-content:center}'
        'main{width:830px;padding:32px 0}img{display:block;margin:0 0 8px}p{margin:0 0 8px;display:flex;gap:1.25%}'
        'p img{margin:0}a{display:contents}</style><main>\n' + block + "\n</main>\n", encoding="utf-8")


def main() -> None:
    D = load_data()
    if OUT.exists():
        for f in OUT.glob("*.svg"):
            f.unlink()  # drop cards for repos that are no longer featured
    OUT.mkdir(exist_ok=True)
    hero_f = hero()
    tile_fs = tiles()
    act_f = activity(D)
    repo_fs = [repo_card(r, i, D) for i, r in enumerate(D["featured"])]
    contrib_f = contributions(D, T_REPOS + len(repo_fs) * T_REPO_STEP + .2)
    if STATIC:
        write_preview(markup("", hero_f, tile_fs, act_f, repo_fs, contrib_f, D))
        print(f"Static final frame written to {OUT.relative_to(ROOT)}/ - open preview/index.html to screenshot.")
    else:
        update_readme(markup("cards/", hero_f, tile_fs, act_f, repo_fs, contrib_f, D))
        sizes = ", ".join(f"{p.name} {p.stat().st_size // 1024}KB" for p in sorted(OUT.glob("*.svg")))
        print(f"Rendered {sizes}")


if __name__ == "__main__":
    main()
