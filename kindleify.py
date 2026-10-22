#!/usr/bin/env python3
"""kindleify: re-layout a single-column PDF (with optional margin notes) for an e-reader.

Most "PDF for Kindle" tools either crop the margins (losing sidenotes) or
rasterize and re-flow the text (losing vector quality). This tool keeps the
original vectors and instead cuts each page's text column into horizontal bands
at the gaps between lines, then stacks those bands onto narrow, screen-shaped
pages. Anything living in a margin column (numbered sidenotes, author asides,
figure captions) is lifted out and inserted inline right after the paragraph or
figure it is attached to, scaled up to body size and marked with a rule.

Page geometry is auto-calibrated from the document: running headers and
footers, the text column edges, the margin column, and the line spacing. Every
detected value can be overridden from the command line.

Requirements: pymupdf, pillow.
"""
from __future__ import annotations

import argparse
import collections
import os
import re
import statistics
import sys
from dataclasses import dataclass, fields

import pymupdf
from PIL import Image

__version__ = "0.2.2"

# Screen aspect ratios (height / width) for common e-readers.
DEVICES = {
    "kindle": 1448 / 1072,       # 6" Kindle, Kindle Basic
    "paperwhite": 1648 / 1236,   # Paperwhite 6.8"
    "oasis": 1680 / 1264,        # Oasis 7"
    "scribe": 2480 / 1860,       # Scribe 10.2"
    "kobo": 1680 / 1264,         # Kobo Clara / Libra (close enough)
    "remarkable": 1872 / 1404,   # reMarkable 2
}


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
@dataclass
class Geometry:
    """Source page geometry, in PDF points, measured from the top-left corner."""
    header_y: float = 0.0        # content above this is a running header
    footer_y: float = 1e9        # content below this is a running footer
    text_l: float = 72.0         # left edge of body text
    text_r: float = 540.0        # right edge of justified body text (ink)
    col_l: float = 66.0          # clip left edge for the main column
    col_r: float = 546.0         # clip right edge for the main column
    split: float = 1e9           # x boundary between main column and margin column
    margin_r: float = 1e9        # right edge of margin-column content
    body_size: float = 10.0      # body font size
    body_font: str = ""          # body font family (e.g. "LMSans10")
    line_gap: float = 4.0        # typical gap between consecutive body-line boxes
    line_pitch: float = 14.0     # baseline-to-baseline distance of body text
    space_w: float = 3.3         # typical inter-word space in body text
    has_margin: bool = False

    @property
    def col_w(self) -> float:
        return self.col_r - self.col_l

    def describe(self) -> str:
        return (
            f"header_y={self.header_y:.0f} footer_y={self.footer_y:.0f} "
            f"text=[{self.text_l:.0f}..{self.text_r:.0f}] clip=[{self.col_l:.0f}..{self.col_r:.0f}] "
            f"split={self.split:.0f} margin_r={self.margin_r:.0f} body={self.body_size:.1f}pt "
            f"font={self.body_font} gap={self.line_gap:.1f} pitch={self.line_pitch:.1f} "
            f"margin_notes={'yes' if self.has_margin else 'no'}"
        )


def _mode(values, ndigits=0):
    if not values:
        return None
    return collections.Counter(round(v, ndigits) for v in values).most_common(1)[0][0]


def _font_family(name: str) -> str:
    """'ABCDEF+LMSans10-Regular' -> 'LMSans10'."""
    name = name.split("+", 1)[-1]
    return re.split(r"[-,]", name, 1)[0]


def _text_lines(page):
    """Yield (bbox, text, size, font) for every non-empty text line on a page."""
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for l in b["lines"]:
            text = "".join(s["text"] for s in l["spans"])
            if text.strip():
                big = max(l["spans"], key=lambda sp: len(sp["text"]))
                yield l["bbox"], text, big["size"], _font_family(big["font"])


def calibrate(doc, sample=40) -> Geometry:
    """Detect page geometry from a sample of pages."""
    n = len(doc)
    idx = sorted(set(round(i * (n - 1) / max(1, sample - 1)) for i in range(min(sample, n))))
    pages = [doc[i] for i in idx]
    W, H = doc[0].rect.width, doc[0].rect.height
    g = Geometry(footer_y=H, split=W, margin_r=W)

    # ---- running headers / footers: same (digit-stripped) text at the same y
    # on a large share of pages, or bare page numbers.
    zone = collections.defaultdict(list)   # y-bucket -> list of (page, normalized text, y1, y0)
    all_lines = []
    for pi, p in enumerate(pages):
        for bbox, text, size, font in _text_lines(p):
            all_lines.append((pi, bbox, text, size, font))
            y0, y1 = bbox[1], bbox[3]
            if y0 < 0.15 * H or y1 > 0.85 * H:
                norm = re.sub(r"\d+", "#", text.strip().lower())
                zone[round(y0 / 4)].append((pi, norm, y0, y1))
    header_y, footer_y = 0.0, H
    for bucket, entries in zone.items():
        by_page = {}
        for pi, norm, y0, y1 in entries:
            by_page.setdefault(pi, norm)
        if len(by_page) < 0.6 * len(pages):
            continue
        norms = collections.Counter(by_page.values())
        top_norm, top_n = norms.most_common(1)[0]
        if top_n < 0.5 * len(pages) and not all(re.fullmatch(r"[#\s.\-–—|]*", v) for v in by_page.values()):
            continue
        ys0 = [e[2] for e in entries]
        ys1 = [e[3] for e in entries]
        if statistics.median(ys0) < 0.15 * H:
            header_y = max(header_y, max(ys1) + 2)
        else:
            footer_y = min(footer_y, min(ys0) - 2)
    g.header_y, g.footer_y = header_y, footer_y

    body = [(pi, bbox, text, size, font) for pi, bbox, text, size, font in all_lines
            if header_y < bbox[1] and bbox[3] < footer_y]
    if not body:
        raise SystemExit("error: no extractable text found (scanned PDF?). kindleify needs a text layer.")

    # ---- body font size: most common size weighted by characters
    size_w = collections.Counter()
    font_w = collections.Counter()
    for _, _, text, size, font in body:
        size_w[round(size, 1)] += len(text)
    g.body_size = size_w.most_common(1)[0][0]
    for _, _, text, size, font in body:
        if abs(size - g.body_size) < 0.6:
            font_w[font] += len(text)
    g.body_font = font_w.most_common(1)[0][0] if font_w else ""
    bodyl = [(pi, bbox) for pi, bbox, _, size, _ in body if abs(size - g.body_size) < 0.6]

    # ---- text column: left edge = mode of x0, right edge = mode of x1 among
    # lines that reach past the median (justified full lines)
    x0s = [b[0] for _, b in bodyl]
    g.text_l = _mode(x0s)
    x1s = [b[2] for _, b in bodyl]
    med = statistics.median(x1s)
    g.text_r = _mode([x for x in x1s if x >= med]) or max(x1s)

    # ---- margin column: lines that start clearly right of the text column
    margin = [(pi, bbox) for pi, bbox, _, _, _ in body if bbox[0] > g.text_r + 12]
    if len(margin) >= 0.02 * len(body) and len(margin) >= 10:
        margin_l = _mode([b[0] for _, b in margin])
        g.split = (g.text_r + margin_l) / 2
        g.margin_r = min(W, max(b[2] for _, b in margin) + 2)
        g.has_margin = True
    g.col_l = max(0.0, g.text_l - 6)
    g.col_r = g.split - 1 if g.has_margin else min(W, g.text_r + 10)

    # ---- line spacing: mode of the gap between consecutive body lines
    gaps = []
    per_page = collections.defaultdict(list)
    for pi, b in bodyl:
        if b[0] < g.split:
            per_page[pi].append(b)
    for lines in per_page.values():
        lines.sort(key=lambda b: b[1])
        for a, b in zip(lines, lines[1:]):
            gp = b[1] - a[3]
            if -2 < gp < 12:
                gaps.append(gp)
    g.line_gap = _mode(gaps, 1) if gaps else 4.0
    heights = [b[3] - b[1] for _, b in bodyl]
    g.line_pitch = (statistics.median(heights) if heights else g.body_size) + g.line_gap

    # ---- inter-word space: median gap between words on body lines
    spaces = []
    for p in pages[:8]:
        for w0, w1 in zip(*(lambda ws: (ws, ws[1:]))(p.get_text("words"))):
            if w0[5:7] == w1[5:7] and w0[0] < g.split and 0 < w1[0] - w0[2] < g.body_size:
                spaces.append(w1[0] - w0[2])
    g.space_w = statistics.median(spaces) if spaces else 0.33 * g.body_size
    return g


# --------------------------------------------------------------------------- #
# Page analysis
# --------------------------------------------------------------------------- #
class Item:
    __slots__ = ("x0", "y0", "x1", "y1", "is_text")

    def __init__(self, bbox, is_text=False):
        self.x0, self.y0, self.x1, self.y1 = bbox
        self.is_text = is_text


def collect_items(page, g: Geometry):
    """All visible things on the page, split into main-column and margin lists."""
    main, margin = [], []

    def add(bbox, is_text):
        if bbox[3] <= g.header_y or bbox[1] >= g.footer_y:
            return
        (main if bbox[0] < g.split else margin).append(Item(bbox, is_text))

    d = page.get_text("dict")
    for b in d["blocks"]:
        if b["type"] == 1:
            add(b["bbox"], False)
            continue
        for l in b["lines"]:
            if "".join(s["text"] for s in l["spans"]).strip():
                add(l["bbox"], True)
    W, H = page.rect.width, page.rect.height
    for dr in page.get_drawings():
        r = dr["rect"]
        if r.is_empty or (r.width < 0.5 and r.height < 0.5):
            continue
        if r.width > 0.8 * W and r.height > 0.8 * H:      # page background
            continue
        add((r.x0, r.y0, r.x1, r.y1), False)
    return main, margin


def cluster(items, gap_tol, max_h=None, graphic_tol=0.0, text_overlap=0.0):
    """Group items into atomic vertical clusters by y-overlap (plus gap_tol).
    Items taller than max_h (big boxes) are dropped so their contents can still
    be split at line gaps. graphic_tol lets drawings and images bridge small gaps
    (arrow ends, box borders) so a diagram made of parts stays one cluster."""
    items = sorted(items, key=lambda i: (i.y0, i.x0))
    clusters = []
    cur = None
    for it in items:
        if max_h is not None and (it.y1 - it.y0) > max_h:
            continue
        tol = gap_tol
        if cur is not None and graphic_tol and (not it.is_text or not cur["last_text"]):
            tol = max(tol, graphic_tol)
        elif cur is not None and it.is_text and cur["last_text"]:
            tol = tol - text_overlap  # a superscript can make neighbouring text lines overlap slightly
        if cur is not None and it.y0 <= cur["y1"] + tol:
            cur["y1"] = max(cur["y1"], it.y1)
            cur["x0"] = min(cur["x0"], it.x0)
            cur["x1"] = max(cur["x1"], it.x1)
            cur["items"].append(it)
            cur["last_text"] = cur["last_text"] and it.is_text
        else:
            cur = {"y0": it.y0, "y1": it.y1, "x0": it.x0, "x1": it.x1, "items": [it], "last_text": it.is_text}
            clusters.append(cur)
    return clusters


def _bands(clusters, top_pad, bot_pad, floor):
    """Cut bands around clusters: midpoints of the gaps between neighbours."""
    out = []
    for i, c in enumerate(clusters):
        top = c["y0"] - top_pad if i == 0 else (clusters[i - 1]["y1"] + c["y0"]) / 2
        bot = c["y1"] + bot_pad if i == len(clusters) - 1 else (c["y1"] + clusters[i + 1]["y0"]) / 2
        out.append((max(top, floor), bot))
    return out


def _line_words(line, g: Geometry):
    """Split a rawdict line into words: tight x-extents, text, baseline, fonts."""
    chars = []
    base = re.sub(r"\d+", "", g.body_font)             # LMSans10 -> LMSans, so LMSans9 counts as body
    for sp in line["spans"]:
        fam = re.sub(r"\d+", "", _font_family(sp["font"]))
        for c in sp["chars"]:
            chars.append((c["c"], c["bbox"], c["origin"][1], sp["size"], fam))
    chars.sort(key=lambda c: c[1][0])
    words, cur = [], []

    def flush():
        if cur:
            words.append({
                "x0": min(c[1][0] for c in cur), "x1": max(c[1][2] for c in cur),
                "text": "".join(c[0] for c in cur),
                "body_chars": sum(1 for c in cur if c[4] == base),
                "max_size": max(c[3] for c in cur),
            })
        cur.clear()

    for i, c in enumerate(chars):
        if c[0].isspace():
            flush()
            continue
        if cur and c[1][0] - cur[-1][1][2] > 0.25 * g.body_size:
            flush()
        cur.append(c)
    flush()
    # drop a trailing hyphen from the last word so it can be glued to the next line
    if words and words[-1]["text"].endswith("-") and len(words[-1]["text"]) > 1:
        last_char = [c for c in chars if not c[0].isspace()][-1]
        words[-1]["hyphen"] = True
        words[-1]["x1"] = last_char[1][0]
    sizes = collections.Counter(round(c[2], 1) for c in chars if not c[0].isspace())
    baseline = sizes.most_common(1)[0][0] if sizes else line["bbox"][3]
    return words, baseline


def build_units(page, g: Geometry, usable_h: float, note_scale: float, keep_notes: bool, max_scale: float = 1.0):
    """Turn one source page into main-column units and margin-note units."""
    main, margin = collect_items(page, g)
    pno = page.number
    line_fonts = {}
    for b in page.get_text("dict")["blocks"]:
        if b["type"] == 0:
            for l in b["lines"]:
                big = max(l["spans"], key=lambda sp: len(sp["text"]))
                first = next((sp for sp in l["spans"] if sp["text"].strip()), big)
                line_fonts[(round(l["bbox"][0], 1), round(l["bbox"][1], 1))] = (max(big["size"], first["size"]), big["font"])
    raw_lines = {}
    if max_scale != 1.0:
        for b in page.get_text("rawdict")["blocks"]:
            if b["type"] == 0:
                for l in b["lines"]:
                    raw_lines[tuple(round(v, 1) for v in l["bbox"])] = l

    clusters = cluster(main, gap_tol=0.0, graphic_tol=8.0, text_overlap=3.0)
    fixed = []
    for gi, c in enumerate(clusters):
        h = c["y1"] - c["y0"]
        text_h = sum(it.y1 - it.y0 for it in c["items"] if it.is_text)
        if h > usable_h * 0.6 and text_h > 0.5 * h:
            # a tall box of text lines (code listing, long note box): break at line
            # gaps, but remember the group so it can be kept on one page when it fits
            for sc in cluster(c["items"], gap_tol=0.0, max_h=4 * g.body_size):
                sc["group"] = (pno, gi)
                fixed.append(sc)
        else:
            fixed.append(c)        # diagrams stay whole; if too tall they are scaled to fit
    clusters = sorted(fixed, key=lambda c: c["y0"])

    units = []
    c_items = {}
    for c, (top, bot) in zip(clusters, _bands(clusters, 2.0, 2.0, g.header_y)):
        wide = [(it.x0, it.y0, it.x1, it.y1) for it in c["items"] if it.x1 > g.split + 3]
        left = min(g.col_l, min(it.x0 for it in c["items"]) - 2)
        right = max(g.col_r, max((w[2] for w in wide), default=0) + 3)
        text_items = [it for it in c["items"] if it.is_text]
        heading = False
        chapter = False
        single_row = text_items and len(text_items) == len(c["items"]) and (c["y1"] - c["y0"]) <= 1.6 * max(
            line_fonts.get((round(it.x0, 1), round(it.y0, 1)), (g.body_size, ""))[0] for it in text_items)
        if single_row:
            # one row of text: a heading if its type is larger than the body ("1.1" and
            # its title are separate lines on the same row; so are run-in headings)
            fonts = [line_fonts.get((round(it.x0, 1), round(it.y0, 1)), (0, "")) for it in text_items]
            sz = max(f[0] for f in fonts)
            heading = sz > g.body_size + 0.6 and not any("Mono" in f[1] for f in fonts)
            chapter = heading and sz >= 1.8 * g.body_size
            if heading and not wide and max_scale > 1.0:
                # a heading gets a tight clip so it can be enlarged like the body text
                right = max(right - (g.col_r - min(g.col_r, max(it.x1 for it in text_items) + 3)), left + 1)
        scale = min(max_scale, g.col_w / (right - left))
        if (bot - top) * scale > usable_h - 2:
            scale = (usable_h - 2) / (bot - top)          # too tall for a page: shrink to fit
        u = {
            "type": "main", "pno": pno, "top": top, "bot": bot, "left": left, "right": right,
            "scale": scale, "heading": heading, "chapter": chapter, "wide": wide, "group": c.get("group"),
            "y0": c["y0"], "y1": c["y1"], "x0min": min(it.x0 for it in c["items"]),
            "ntext": len(text_items), "text_x1": max((it.x1 for it in text_items), default=None),
            "graphics": len(text_items) != len(c["items"]), "words": None,
        }
        # one row of plain text: keep its words so paragraphs can be re-wrapped. A row
        # may be several extraction "lines" sitting side by side (a run-in italic
        # heading followed by regular text); merge them when they are adjacent.
        row_ok = (raw_lines and not u["graphics"] and not wide and not heading
                  and u["x0min"] >= g.col_l and (c["y1"] - c["y0"]) <= 1.6 * g.body_size)
        if row_ok and u["ntext"] >= 2:
            ordered = sorted(text_items, key=lambda it: it.x0)
            row_ok = all(b.x0 - a.x1 < 1.2 * g.body_size for a, b in zip(ordered, ordered[1:]))
        if row_ok:
            rls = [raw_lines.get((round(it.x0, 1), round(it.y0, 1), round(it.x1, 1), round(it.y1, 1)))
                   for it in sorted(text_items, key=lambda it: it.x0)]
            if all(rl is not None for rl in rls):
                words, baseline = [], None
                for rl in rls:
                    w, b = _line_words(rl, g)
                    words += w
                    baseline = baseline if baseline is not None else b
                leaders = any(re.fullmatch(r"[.\s]{3,}", w["text"]) for w in words) or \
                    sum(1 for w in words if w["text"] == ".") >= 3
                u["leaders"] = leaders
                if words and not leaders and all(w["max_size"] <= g.body_size + 0.6 for w in words):
                    u["words"], u["baseline"] = words, baseline
        units.append(u)
        c_items[id(u)] = c["items"]

    if units:
        units[0]["first_on_page"] = True
    # consecutive code lines (mono-dominant, close together) form one block that is
    # kept on a page when it fits; frames are irrelevant, blank lines are not breaks
    def is_code(u):
        if u["ntext"] == 0:
            return False
        # ignore gutter glyphs (line numbers, wrap arrows) when judging the row,
        # unless the row is nothing but short tokens such as "}" or ")"
        texts = [it for it in c_items[id(u)] if it.is_text]
        wide = [it for it in texts if (it.x1 - it.x0) >= 2 * g.body_size] or texts
        fonts = [line_fonts.get((round(it.x0, 1), round(it.y0, 1)), (0, ""))[1] for it in wide]
        return bool(fonts) and sum("Mono" in f for f in fonts) * 2 >= len(fonts)
    def is_gutter(u):
        # a blank code line in a numbered listing is just its tiny line number
        return u["ntext"] == 1 and (u["y1"] - u["y0"]) < 0.8 * g.body_size and \
            all((it.x1 - it.x0) < 2 * g.body_size for it in c_items[id(u)])
    run = None
    for i, u in enumerate(units):
        prev = units[i - 1] if i else None
        in_run = prev is not None and (is_code(prev) or (prev.get("group") is not None and is_gutter(prev)))
        # blank lines inside a listing are about one line pitch; allow 2.5
        if (is_code(u) or (in_run and is_gutter(u))) and in_run and u["y0"] - prev["y1"] < 2.5 * g.line_pitch:
            if prev.get("group") is None:
                run = run or (pno, "code", prev["y0"])
                prev["group"] = run
            u["group"] = prev["group"]
        else:
            run = None
    for u in units:
        u["mono"] = is_code(u)
        if u.get("group") is not None:
            u["words"] = None                          # code is never re-wrapped
    # table-of-contents pages (many dot-leader lines) are never re-wrapped
    if len(re.findall(r"(?:\.\s){4,}", page.get_text())) >= 5:
        for u in units:
            u["words"] = None

    # Real ink right-edge of single-line units (text bboxes are noisy on justified lines).
    pix = page.get_pixmap(dpi=72, colorspace="gray")
    im = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    for u in units:
        if u["ntext"] == 1:
            band = im.crop((int(g.col_l), int(u["y0"]), int(g.col_r) + 1, int(u["y1"]) + 1))
            bb = band.point(lambda v: 255 if v < 160 else 0).getbbox()
            if bb:
                u["text_x1"] = g.col_l + bb[2]

    # Tables / wide blocks: short cell lines inside a wide region must share the
    # region's scale or they render displaced. Propagate wide-ness to adjacent
    # indented units.
    changed = True
    while changed:
        changed = False
        for i, u in enumerate(units):
            if u["right"] > g.col_r or u["x0min"] <= g.text_l + 50:
                continue
            for j in (i - 1, i + 1):
                if 0 <= j < len(units) and units[j]["right"] > g.col_r and \
                        min(abs(u["y0"] - units[j]["y1"]), abs(units[j]["y0"] - u["y1"])) < 3 * g.line_gap:
                    u["right"] = units[j]["right"]
                    u["scale"] = min(max_scale, g.col_w / (u["right"] - u["left"]))
                    u["words"] = None
                    changed = True
                    break

    notes = []
    if keep_notes and g.has_margin:
        nclusters = cluster(margin, gap_tol=g.line_gap + 1.5, max_h=usable_h / note_scale * 0.6)
        for c, (top, bot) in zip(nclusters, _bands(nclusters, 2.0, 2.0, g.header_y)):
            if all(it.is_text and (it.x1 - it.x0) < g.body_size for it in c["items"]):
                continue                               # a stray glyph in the margin, not a note
            top, bot = max(top, c["y0"] - 2.0), min(bot, c["y1"] + 2.0)
            subs = cluster(c["items"], gap_tol=0.0, max_h=4 * g.body_size)
            bands = _bands(subs, 0, 0, g.header_y) if subs else []
            if bands:
                bands[0] = (top, bands[0][1])
                bands[-1] = (bands[-1][0], bot)
            else:
                bands = [(top, bot)]
            notes.append({
                "type": "note", "pno": pno, "top": top, "bot": bot, "bands": bands,
                "left": max(g.split, c["x0"] - 2), "right": min(g.margin_r, c["x1"] + 2), "anchor": c["y0"],
            })
    return units, notes


BULLET_RE = re.compile(r"^([•◦▪–—\-*·]|\(?(\d{1,3}|[a-zA-Z]|[ivxIVX]{1,4})[.)])$")   # •, 1., (a), iv.


def _is_end(u, nxt, g: Geometry, bullet: bool, body_x: float, hanging: bool) -> bool:
    """Is unit u the last line of its paragraph? (bullet/body_x/hanging describe
    the paragraph u belongs to: does it start with a bullet, where does its text
    body start, and is it a hanging-indent paragraph such as a bibliography entry.)"""
    if nxt is None:
        return True
    same_page = nxt["pno"] == u["pno"]
    gap = nxt["y0"] - u["y1"] if same_page else 0.0
    if same_page and gap < 3 * g.line_gap:
        if u["tabular"] and nxt["tabular"]:
            return False                              # rows of one table
        if u["right"] > g.col_r and nxt["right"] > g.col_r:
            return False                              # rows of one wide table
    if (u["ntext"] != 1 and u["words"] is None) or (nxt["ntext"] != 1 and nxt["words"] is None):
        return True                                   # headings, boxes, figures, code
    if same_page and gap > g.line_gap + 2.5:
        return True                                   # vertical space
    short = u["text_x1"] is None or u["text_x1"] < g.text_r - 9
    if u["words"] and u["words"][-1].get("hyphen"):
        return False                                  # a hyphenated word continues on the next line
    if u["words"] is None or nxt["words"] is None:
        if u["words"] is None and nxt["words"] is None:
            return short or nxt["x0min"] > g.text_l + 8   # no word data (scale 1.0)
        return True
    nx = nxt["x0min"]
    if BULLET_RE.match(nxt["words"][0]["text"]):
        return True                                   # next item
    if nx > body_x + 3:
        # deeper indent: a new indented paragraph after a short line, or a
        # hanging-indent continuation after a full line (bibliographies)
        return short or nx > body_x + 20
    if nx < body_x - 3:
        if bullet or hanging:
            return True                               # de-dent: next item / next entry
        return False                                  # second line after an indented first line
    if not short:
        return False
    # A short line usually ends the paragraph, but LaTeX occasionally leaves a
    # justified line short. Ask for corroboration unless the line is very short.
    very_short = u["text_x1"] < g.text_l + 0.55 * (g.text_r - g.text_l)
    last, first = u["words"][-1]["text"], nxt["words"][0]["text"]
    ends_sentence = last[-1:] in '.?!:;”"'
    starts_sentence = first[:1].isupper() or first[:1].isdigit() or first[:1] in '“("'
    if bullet:
        return very_short or ends_sentence or starts_sentence
    return very_short or (ends_sentence and starts_sentence)


def mark_paragraphs(units, g: Geometry):
    """Set u["para_end"] on every main unit with a stateful scan."""
    # tables inside the column: rows with side-by-side cells, plus their deeply
    # indented continuation lines
    for u in units:
        u["tabular"] = u["ntext"] >= 2 and u["words"] is None
    changed = True
    while changed:
        changed = False
        for i, u in enumerate(units):
            if u["tabular"] or u["ntext"] != 1 or u["x0min"] <= g.text_l + 40:
                continue
            for j in (i - 1, i + 1):
                if 0 <= j < len(units) and units[j]["tabular"] and units[j]["pno"] == u["pno"] and \
                        min(abs(u["y0"] - units[j]["y1"]), abs(units[j]["y0"] - u["y1"])) < 3 * g.line_gap:
                    u["tabular"] = True
                    changed = True
                    break
    start, bullet, body_x, first_x = True, False, 0.0, 0.0
    for i, u in enumerate(units):
        nxt = units[i + 1] if i + 1 < len(units) else None
        w = u["words"]
        if start:
            bullet = bool(w) and len(w) > 1 and BULLET_RE.match(w[0]["text"]) is not None
            body_x = w[1]["x0"] if bullet else u["x0min"]
            first_x = u["x0min"]
        elif not bullet:
            body_x = u["x0min"]
        hanging = not bullet and not start and u["x0min"] > first_x + 3
        u["para_end"] = _is_end(u, nxt, g, bullet, body_x, hanging)
        start = u["para_end"]


def anchor_notes(units, notes, g: Geometry):
    """Decide after which main unit each note is inserted (global reading order)."""
    mark_paragraphs(units, g)
    first_unit = {}
    for i, u in enumerate(units):
        first_unit.setdefault(u["pno"], i)
    for n in notes:
        ay = n["anchor"] + 3.0
        pno = n["pno"]
        target = None
        i = first_unit.get(pno)
        if i is not None:
            while i < len(units) and units[i]["pno"] == pno:
                u = units[i]
                if u["y0"] - 1 <= ay <= u["y1"] + 1 or u["y0"] > ay:
                    target = i
                    break
                i += 1
            if target is None:
                target = i - 1                        # note hangs below the page's last line
        else:                                         # page with no main text
            target = max([i for i, u in enumerate(units) if u["pno"] < pno], default=-1)
        if target >= 0:
            span = 0.0
            while not units[target]["para_end"] and target + 1 < len(units) and span < 200:
                span += units[target + 1]["bot"] - units[target + 1]["top"]
                target += 1
            grp = units[target].get("group")
            if grp is not None:
                # never interrupt a code block: place the note after it
                while target + 1 < len(units) and units[target + 1].get("group") == grp:
                    target += 1
        n["after"] = target


# --------------------------------------------------------------------------- #
# Reflow
# --------------------------------------------------------------------------- #
def reflow(seq, g: Geometry, scale: float):
    """Re-wrap runs of plain body lines at `scale`, word by word.

    Each word is a clip of the source page, so glyphs, kerning, ligatures and
    superscripts are untouched; only the line breaks and inter-word spacing are
    recomputed. Everything that is not a plain body line (figures, boxes, code,
    display math, headings) passes through unchanged."""
    out = []
    para = []

    def flush_para():
        if para:
            out.extend(_wrap_paragraph(para, g, scale))
            para.clear()

    for u in seq:
        if u["type"] == "main" and u["words"] is not None:
            para.append(u)
            if u["para_end"]:
                flush_para()
        else:
            flush_para()
            out.append(u)
    flush_para()
    return out


def _wrap_paragraph(lines, g: Geometry, s: float):
    total = sum(len(w["text"]) for u in lines for w in u["words"])
    body = sum(w["body_chars"] for u in lines for w in u["words"])
    justified = any(u["text_x1"] is not None and u["text_x1"] >= g.text_r - 9 for u in lines)
    if total == 0 or (body < 0.5 * total and not justified):
        return lines                                 # code, math: leave as bands

    # tokens: (list of (pno, clip rect, baseline), width_src)
    tokens = []
    for u in lines:
        top, bot, b, pno = u["top"], u["bot"], u["baseline"], u["pno"]
        for i, w in enumerate(u["words"]):
            piece = (pno, pymupdf.Rect(w["x0"] - 0.6, top, w["x1"] + 0.6, bot), b)
            if tokens and tokens[-1]["glue"]:
                tokens[-1]["pieces"].append(piece)
                tokens[-1]["glue"] = False
            else:
                tokens.append({"pieces": [piece], "glue": False, "text": w["text"]})
            if w.get("hyphen") and i == len(u["words"]) - 1:
                tokens[-1]["glue"] = True
    if tokens:
        tokens[-1]["glue"] = False
    for t in tokens:
        t["w"] = sum(p[1].width - 1.2 for p in t["pieces"])

    first_indent = lines[0]["x0min"] - g.text_l
    if len(lines) > 1:
        rest_indent = lines[1]["x0min"] - g.text_l
    elif BULLET_RE.match(tokens[0]["text"]) and len(tokens) > 1:
        rest_indent = tokens[1]["pieces"][0][1].x0 + 0.6 - g.text_l
    else:
        rest_indent = 0.0
    if BULLET_RE.match(tokens[0]["text"]) and len(tokens) > 1 and len(lines) > 1:
        rest_indent = max(rest_indent, tokens[1]["pieces"][0][1].x0 + 0.6 - g.text_l)

    out_lines = []
    i = 0
    while i < len(tokens):
        indent_src = first_indent if not out_lines else rest_indent
        # a single token wider than the column (a long URL) shrinks its own line
        ls = min(s, (g.col_w - indent_src * s) / tokens[i]["w"]) if tokens[i]["w"] > 0 else s
        indent = indent_src * ls
        avail = g.col_w - indent
        space = g.space_w * ls
        j, width = i, 0.0
        while j < len(tokens):
            w = tokens[j]["w"] * ls
            if j > i and width + space + w > avail:
                break
            width += (space if j > i else 0) + w
            j += 1
        chosen = tokens[i:j]
        last = j >= len(tokens)
        gap = space
        if not last and len(chosen) > 1:
            extra = avail - width
            if extra < space * 4 * (len(chosen) - 1):
                gap = space + extra / (len(chosen) - 1)
        x = indent
        placed = []
        for t in chosen:
            for (pno, clip, b) in t["pieces"]:
                placed.append((pno, clip, b, x - 0.6 * ls))
                x += (clip.width - 1.2) * ls
            x += gap
        out_lines.append({"type": "line", "pno": lines[0]["pno"], "height": g.line_pitch * ls,
                          "scale": ls, "placed": placed, "para_end": last})
        i = j
    return out_lines


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
class Layout:
    def __init__(self, src, g: Geometry, aspect: float, pad: float, note_scale: float, scale: float = 1.0,
                 verbose=False):
        self.src, self.g = src, g
        self.pad, self.note_scale, self.scale = pad, note_scale, scale
        self.out_w = g.col_w + 2 * pad
        self.out_h = round(self.out_w * aspect)
        self.usable_h = self.out_h - 2 * pad
        self.note_gap = 5.0
        self.note_indent = 16.0
        self.out = pymupdf.open()
        self.cur = None
        self.y = pad
        self.page_map = {}
        self.verbose = verbose
        self.src_h = src[0].rect.height
        self.xobj = {}          # source pno -> xref of its full-page form in `out`
        self.out.new_page(width=10, height=10)   # page 0 hosts the imports, deleted at the end
        self.ops = []           # pending content-stream ops for the current page
        self.names = {}         # source pno -> resource name on the current page

    # -- primitives ---------------------------------------------------------
    def flush_page(self):
        if self.cur is not None and self.ops:
            pymupdf.TOOLS._insert_contents(self.cur, "".join(self.ops).encode(), True)
        self.ops, self.names = [], {}

    def new_page(self):
        self.flush_page()
        self.cur = self.out.new_page(width=self.out_w, height=self.out_h)
        self.y = self.pad

    def blit(self, pno, clip, dst):
        """Draw the source region `clip` (page pno) into `dst` on the current page.
        Both rects are in top-left coordinates. One shared form XObject per source
        page keeps the file small even with tens of thousands of word clips."""
        name = self.names.get(pno)
        if name is None:
            xref = self.xobj.get(pno)
            if xref is None:
                scratch = self.out[0]          # page objects go stale when pages are added
                xref = scratch.show_pdf_page(scratch.rect, self.src, pno)
                self.xobj[pno] = xref
            name = f"Src{pno}"
            res = self.out.xref_get_key(self.cur.xref, "Resources")
            if res[0] == "xref":
                self.out.xref_set_key(int(res[1].split()[0]), f"XObject/{name}", f"{xref} 0 R")
            else:
                self.out.xref_set_key(self.cur.xref, f"Resources/XObject/{name}", f"{xref} 0 R")
            self.names[pno] = name
        s = dst.width / clip.width
        tx = dst.x0 - s * clip.x0
        ty = (self.out_h - dst.y1) - s * (self.src_h - clip.y1)
        self.ops.append(f"q {dst.x0:.2f} {self.out_h - dst.y1:.2f} {dst.width:.2f} {dst.height:.2f} re W n "
                        f"{s:.4f} 0 0 {s:.4f} {tx:.2f} {ty:.2f} cm /{name} Do Q\n")

    def finish(self):
        self.flush_page()
        self.out.delete_page(0)
        return self.out

    def unit_height(self, u):
        if u["type"] == "main":
            return (u["bot"] - u["top"]) * u["scale"]
        if u["type"] == "line":
            return u["height"]
        return (u["bot"] - u["top"]) * self.note_eff(u) + 2 * self.note_gap

    def note_eff(self, n):
        """Note magnification, capped so the note fits the column."""
        avail = self.out_w - 2 * self.pad - self.note_indent
        return min(self.note_scale, avail / (n["right"] - n["left"]))

    def draw_line(self, u):
        """Render one re-wrapped line of word clips, aligned on their baselines."""
        s = u["scale"]
        base_y = self.y + (self.g.line_pitch - 0.3 * self.g.body_size) * s
        for (pno, clip, b, x) in u["placed"]:
            dst = pymupdf.Rect(self.pad + x, base_y - (b - clip.y0) * s,
                               self.pad + x + clip.width * s, base_y + (clip.y1 - b) * s)
            self.blit(pno, clip, dst)
        self.y += u["height"]

    def flush_strip(self, strip):
        """Render a run of consecutive main units from one page as one clip."""
        if not strip:
            return
        g, pno = self.g, strip[0]["pno"]
        top, bot = strip[0]["top"], strip[-1]["bot"]
        left = min(u["left"] for u in strip)
        right = max(u["right"] for u in strip)
        scale = min([self.scale, g.col_w / (right - left)] + [u["scale"] for u in strip])
        h = (bot - top) * scale
        clip = pymupdf.Rect(left, top, min(right, g.col_r), bot)
        dst = pymupdf.Rect(self.pad, self.y, self.pad + clip.width * scale, self.y + h)
        self.blit(pno, clip, dst)
        # content that spills right of the column (wide tables, long code lines)
        wclips = []
        for u in strip:
            for (_, wy0, wx1, wy1) in u["wide"]:
                r = pymupdf.Rect(g.col_r, max(top, wy0 - 1.5), wx1 + 3, min(bot, wy1 + 1.5))
                if not r.is_empty and r.width > 0:
                    wclips.append(r)
        wclips.sort(key=lambda r: r.y0)
        merged = []
        for r in wclips:                      # merge overlaps so nothing draws twice
            if merged and r.y0 <= merged[-1].y1:
                merged[-1] |= r
            else:
                merged.append(r)
        for r in merged:
            wdst = pymupdf.Rect(self.pad + (r.x0 - left) * scale, self.y + (r.y0 - top) * scale,
                                self.pad + (r.x1 - left) * scale, self.y + (r.y1 - top) * scale)
            self.blit(pno, r, wdst)
        self.y += h

    def draw_note(self, n, top, bot):
        self.y += self.note_gap
        clip = pymupdf.Rect(n["left"], top, n["right"], bot)
        ns = self.note_eff(n)
        h, w = clip.height * ns, clip.width * ns
        x0 = self.pad + self.note_indent
        self.blit(n["pno"], clip, pymupdf.Rect(x0, self.y, x0 + w, self.y + h))
        sh = self.cur.new_shape()
        sh.draw_line((self.pad + 5, self.y + 1), (self.pad + 5, self.y + h - 1))
        sh.finish(color=(0.55, 0.55, 0.55), width=1.2)
        sh.commit()
        self.y += h + self.note_gap

    def place_note(self, n):
        """Place a note's line bands, breaking to a new page when needed."""
        run_top = run_bot = None
        acc = 0.0
        ns = self.note_eff(n)
        for (bt, bb) in n["bands"]:
            bh = (bb - bt) * ns
            extra = 0.0 if run_top is not None else 2 * self.note_gap
            if self.y + acc + bh + extra > self.out_h - self.pad + 0.01 and (self.y + acc) > self.pad + 0.01:
                if run_top is not None:
                    self.draw_note(n, run_top, run_bot)
                self.new_page()
                run_top, acc, extra = None, 0.0, 2 * self.note_gap
            if run_top is None:
                run_top = bt
            run_bot = bb
            acc += bh + extra
        if run_top is not None:
            self.draw_note(n, run_top, run_bot)

    # -- driver ---------------------------------------------------------------
    def run(self, seq):
        self.new_page()
        strip, pending = [], 0.0
        limit = self.out_h - self.pad + 0.01
        for idx, u in enumerate(seq):
            pno = u["pno"]
            h = self.unit_height(u)
            need = h
            if u.get("heading"):
                # keep a heading with what follows it: any further headings plus
                # at least three lines of text, so it never ends a page alone
                body_lines = 0
                for nx in seq[idx + 1:idx + 8]:
                    nh = self.unit_height(nx)
                    if nx["type"] != "line" and not nx.get("heading"):
                        # a block (code, figure) follows: ask for three lines' worth of it
                        need += min(nh, 3 * self.g.line_pitch * self.scale)
                        break
                    need += nh
                    if not nx.get("heading"):
                        body_lines += 1
                        if body_lines >= 3:
                            break
            prev = seq[idx - 1] if idx else None
            if u.get("chapter") and u.get("first_on_page") and (self.y + pending) > self.pad + 0.01:
                need = float("inf")                    # a chapter title opens a new page
            grp = u.get("group")
            if grp is not None and not (prev and prev.get("group") == grp):
                # first line of a split block (code listing): if the whole block fits
                # on one page, do not let it straddle two
                gh = 0.0
                for nx in seq[idx:]:
                    if nx["type"] == "note":
                        continue
                    if nx.get("group") != grp:
                        break
                    gh += self.unit_height(nx)
                if gh <= self.out_h - 2 * self.pad:
                    need = max(need, gh)
                else:
                    need = float("inf")               # taller than a page: start it on a fresh page, then bleed
                if os.environ.get("KINDLEIFY_DEBUG"):
                    print(f"group {grp} src_p{pno + 1} gh={gh:.0f} at y={self.y + pending:.0f} limit={limit:.0f} "
                          f"need={need:.0f} -> {'BREAK' if self.y + pending + need > limit and self.y + pending > self.pad else 'stay'}", file=sys.stderr)
            if u["type"] in ("main", "line") and self.y + pending + need > limit and (self.y + pending) > self.pad + 0.01:
                self.flush_strip(strip)
                strip, pending = [], 0.0
                self.new_page()
            self.page_map.setdefault(pno, len(self.out) - 2)   # page 0 is the scratch page
            if u["type"] == "main":
                if strip and (abs(u["scale"] - strip[-1]["scale"]) > 0.01 or strip[-1]["pno"] != pno):
                    self.flush_strip(strip)
                    strip, pending = [], 0.0
                strip.append(u)
                pending += h
            elif u["type"] == "line":
                self.flush_strip(strip)
                strip, pending = [], 0.0
                self.draw_line(u)
            else:
                self.flush_strip(strip)
                strip, pending = [], 0.0
                self.place_note(u)
        self.flush_strip(strip)
        return self.finish()


def convert(src_path, dst_path, device="kindle", aspect=None, pad=8.0, note_scale=None, scale=1.0,
            keep_notes=True, overrides=None, verbose=False, dry_run=False):
    src = pymupdf.open(src_path)
    if src.is_encrypted:
        raise SystemExit("error: the PDF is encrypted; decrypt it first (e.g. qpdf --decrypt).")
    g = calibrate(src)
    for k, v in (overrides or {}).items():
        setattr(g, k, v)
        if k in ("split", "margin_r"):
            g.has_margin = True
    if overrides and ("text_l" in overrides or "text_r" in overrides or "split" in overrides):
        g.col_l = max(0.0, g.text_l - 6)
        g.col_r = g.split - 1 if g.has_margin else min(src[0].rect.width, g.text_r + 10)
    aspect = aspect or DEVICES[device]
    if note_scale is None:
        note_scale = 1.25 * scale
    if verbose or dry_run:
        print(f"calibration: {g.describe()}", file=sys.stderr)
        print(f"output page: {g.col_w + 2 * pad:.0f} x {round((g.col_w + 2 * pad) * aspect)} pt "
              f"(aspect {aspect:.3f}, {device if not overrides else 'custom'})", file=sys.stderr)
    if dry_run:
        return None

    lay = Layout(src, g, aspect, pad, note_scale, scale, verbose)
    units, notes = [], []
    for page in src:
        u, n = build_units(page, g, lay.usable_h, note_scale, keep_notes, scale)
        units.extend(u)
        notes.extend(n)
    # a listing that continues on the next source page is one block, whether the
    # continuation was split into lines or is small enough to be one atomic unit
    for a, b in zip(units, units[1:]):
        if (a.get("group") is not None or a.get("mono")) and b["pno"] == a["pno"] + 1 and b.get("first_on_page") \
                and (b.get("group") is not None or b.get("mono")) and a.get("group") != b.get("group"):
            if a.get("group") is None:
                a["group"] = (a["pno"], "cont", a["y0"])
            old = b.get("group")
            if old is None:
                b["group"] = a["group"]
            else:
                for u in units:
                    if u.get("group") == old:
                        u["group"] = a["group"]
    anchor_notes(units, notes, g)
    by_after = collections.defaultdict(list)
    for n in notes:
        by_after[n["after"]].append(n)
    seq = list(by_after.get(-1, []))
    for i, u in enumerate(units):
        seq.append(u)
        seq.extend(by_after.get(i, []))
    if scale != 1.0:
        seq = reflow(seq, g, scale)
    out = lay.run(seq)

    toc = [[lvl, title, lay.page_map[pg - 1] + 1] for lvl, title, pg in src.get_toc() if pg - 1 in lay.page_map]
    if toc:
        out.set_toc(toc)
    out.set_metadata(src.metadata or {})
    out.save(dst_path, garbage=4, deflate=True)
    return len(src), len(out), len(notes)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="kindleify",
        description="Re-layout a single-column PDF (with optional margin notes) for an e-reader screen. "
                    "Keeps vectors; lifts sidenotes inline.",
        epilog="examples:\n"
               "  kindleify reader.pdf                      # writes reader.kindle.pdf\n"
               "  kindleify reader.pdf -o out.pdf --device paperwhite\n"
               "  kindleify reader.pdf --scale 1.3          # re-wrap body text 30%% larger\n"
               "  kindleify reader.pdf --dry-run            # show detected geometry only\n"
               "  kindleify paper.pdf --no-notes --split 480 # drop the margin column\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("input", help="source PDF (needs a text layer; scans are not supported)")
    ap.add_argument("-o", "--output", help="output PDF (default: <input>.kindle.pdf)")
    ap.add_argument("--device", choices=sorted(DEVICES), default="kindle",
                    help="target screen aspect ratio (default: kindle)")
    ap.add_argument("--aspect", type=float, help="explicit height/width ratio, overrides --device")
    ap.add_argument("--pad", type=float, default=8.0, help="page margin in points (default: 8)")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="magnify body text by re-wrapping it word by word (e.g. 1.3). Figures, code, "
                         "math and boxes are enlarged only as far as the column allows. Default: 1.0, no re-wrap")
    ap.add_argument("--note-scale", type=float, default=None,
                    help="magnification applied to margin notes (default: 1.25 x --scale, i.e. 8pt -> body size)")
    ap.add_argument("--no-notes", action="store_true", help="drop the margin column instead of inlining it")
    ap.add_argument("--dry-run", action="store_true", help="print the detected geometry and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    geo = ap.add_argument_group("geometry overrides (points from the top-left; see --dry-run)")
    for f in ("header_y", "footer_y", "text_l", "text_r", "split", "margin_r", "line_gap"):
        geo.add_argument(f"--{f.replace('_', '-')}", type=float, dest=f"geo_{f}")
    args = ap.parse_args(argv)

    overrides = {f.name: getattr(args, f"geo_{f.name}") for f in fields(Geometry)
                 if getattr(args, f"geo_{f.name}", None) is not None}
    out = args.output or re.sub(r"\.pdf$", "", args.input, flags=re.I) + ".kindle.pdf"
    if out == args.input:
        raise SystemExit("error: output would overwrite the input; pass -o with a different name.")
    try:
        res = convert(args.input, out, device=args.device, aspect=args.aspect, pad=args.pad,
                      note_scale=args.note_scale, scale=args.scale, keep_notes=not args.no_notes,
                      overrides=overrides, verbose=args.verbose, dry_run=args.dry_run)
    except (pymupdf.FileNotFoundError, pymupdf.FileDataError) as e:
        raise SystemExit(f"error: cannot open {args.input}: {e}")
    if res:
        n_src, n_out, n_notes = res
        print(f"{args.input}: {n_src} pages -> {out}: {n_out} pages, {n_notes} margin notes inlined")
    return 0


if __name__ == "__main__":
    sys.exit(main())
