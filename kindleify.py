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
import re
import statistics
import sys
from dataclasses import dataclass, fields

import pymupdf
from PIL import Image

__version__ = "0.1.0"

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
    line_gap: float = 4.0        # typical gap between consecutive body-line boxes
    has_margin: bool = False

    @property
    def col_w(self) -> float:
        return self.col_r - self.col_l

    def describe(self) -> str:
        return (
            f"header_y={self.header_y:.0f} footer_y={self.footer_y:.0f} "
            f"text=[{self.text_l:.0f}..{self.text_r:.0f}] clip=[{self.col_l:.0f}..{self.col_r:.0f}] "
            f"split={self.split:.0f} margin_r={self.margin_r:.0f} body={self.body_size:.1f}pt "
            f"gap={self.line_gap:.1f} margin_notes={'yes' if self.has_margin else 'no'}"
        )


def _mode(values, ndigits=0):
    if not values:
        return None
    return collections.Counter(round(v, ndigits) for v in values).most_common(1)[0][0]


def _text_lines(page):
    """Yield (bbox, text, size) for every non-empty text line on a page."""
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for l in b["lines"]:
            text = "".join(s["text"] for s in l["spans"])
            if text.strip():
                yield l["bbox"], text, l["spans"][0]["size"]


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
        for bbox, text, size in _text_lines(p):
            all_lines.append((pi, bbox, text, size))
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

    body = [(pi, bbox, text, size) for pi, bbox, text, size in all_lines
            if header_y < bbox[1] and bbox[3] < footer_y]
    if not body:
        raise SystemExit("error: no extractable text found (scanned PDF?). kindleify needs a text layer.")

    # ---- body font size: most common size weighted by characters
    size_w = collections.Counter()
    for _, _, text, size in body:
        size_w[round(size, 1)] += len(text)
    g.body_size = size_w.most_common(1)[0][0]
    bodyl = [(pi, bbox) for pi, bbox, _, size in body if abs(size - g.body_size) < 0.6]

    # ---- text column: left edge = mode of x0, right edge = mode of x1 among
    # lines that reach past the median (justified full lines)
    x0s = [b[0] for _, b in bodyl]
    g.text_l = _mode(x0s)
    x1s = [b[2] for _, b in bodyl]
    med = statistics.median(x1s)
    g.text_r = _mode([x for x in x1s if x >= med]) or max(x1s)

    # ---- margin column: lines that start clearly right of the text column
    margin = [(pi, bbox) for pi, bbox, _, _ in body if bbox[0] > g.text_r + 12]
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


def cluster(items, gap_tol, max_h=None):
    """Group items into atomic vertical clusters by y-overlap (plus gap_tol).
    Items taller than max_h (big boxes) are dropped so their contents can still
    be split at line gaps."""
    items = sorted(items, key=lambda i: (i.y0, i.x0))
    clusters = []
    cur = None
    for it in items:
        if max_h is not None and (it.y1 - it.y0) > max_h:
            continue
        if cur is not None and it.y0 <= cur["y1"] + gap_tol:
            cur["y1"] = max(cur["y1"], it.y1)
            cur["x0"] = min(cur["x0"], it.x0)
            cur["x1"] = max(cur["x1"], it.x1)
            cur["items"].append(it)
        else:
            cur = {"y0": it.y0, "y1": it.y1, "x0": it.x0, "x1": it.x1, "items": [it]}
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


def build_units(page, g: Geometry, usable_h: float, note_scale: float, keep_notes: bool):
    """Turn one source page into main-column units and margin-note units."""
    main, margin = collect_items(page, g)
    pno = page.number

    clusters = cluster(main, gap_tol=0.0)
    fixed = []
    for c in clusters:
        if c["y1"] - c["y0"] > usable_h * 0.6:
            fixed.extend(cluster(c["items"], gap_tol=0.0, max_h=4 * g.body_size))
        else:
            fixed.append(c)
    clusters = sorted(fixed, key=lambda c: c["y0"])

    units = []
    for c, (top, bot) in zip(clusters, _bands(clusters, 2.0, 2.0, g.header_y)):
        wide = [(it.x0, it.y0, it.x1, it.y1) for it in c["items"] if it.x1 > g.split + 3]
        left = min(g.col_l, min(it.x0 for it in c["items"]) - 2)
        right = max(g.col_r, max((w[2] for w in wide), default=0) + 3)
        text_items = [it for it in c["items"] if it.is_text]
        units.append({
            "type": "main", "pno": pno, "top": top, "bot": bot, "left": left, "right": right,
            "scale": min(1.0, g.col_w / (right - left)), "wide": wide,
            "y0": c["y0"], "y1": c["y1"], "x0min": min(it.x0 for it in c["items"]),
            "ntext": len(text_items), "text_x1": max((it.x1 for it in text_items), default=None),
        })

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
                    u["scale"] = min(1.0, g.col_w / (u["right"] - u["left"]))
                    changed = True
                    break

    notes = []
    if keep_notes and g.has_margin:
        nclusters = cluster(margin, gap_tol=g.line_gap + 1.5, max_h=usable_h / note_scale * 0.6)
        for c, (top, bot) in zip(nclusters, _bands(nclusters, 2.0, 2.0, g.header_y)):
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


def anchor_notes(units, notes, g: Geometry):
    """Decide after which main unit each note is inserted (global reading order)."""
    # Paragraph ends: a short last line, an extra vertical gap, an indented or
    # bulleted next line, or a multi-line atomic cluster (box, figure, code).
    for i, u in enumerate(units):
        end = u["ntext"] != 1 or u["text_x1"] is None or u["text_x1"] < g.text_r - 9
        if i + 1 < len(units):
            nxt = units[i + 1]
            if nxt["pno"] == u["pno"] and nxt["y0"] - u["y1"] > g.line_gap + 2.5:
                end = True
            if nxt["x0min"] > g.text_l + 8 or nxt["ntext"] != 1:
                end = True
        else:
            end = True
        u["para_end"] = end

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
        n["after"] = target


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
class Layout:
    def __init__(self, src, g: Geometry, aspect: float, pad: float, note_scale: float, verbose=False):
        self.src, self.g = src, g
        self.pad, self.note_scale = pad, note_scale
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

    # -- primitives ---------------------------------------------------------
    def new_page(self):
        self.cur = self.out.new_page(width=self.out_w, height=self.out_h)
        self.y = self.pad

    def unit_height(self, u):
        if u["type"] == "main":
            return (u["bot"] - u["top"]) * u["scale"]
        return (u["bot"] - u["top"]) * self.note_scale + 2 * self.note_gap

    def flush_strip(self, strip):
        """Render a run of consecutive main units from one page as one clip."""
        if not strip:
            return
        g, pno = self.g, strip[0]["pno"]
        top, bot = strip[0]["top"], strip[-1]["bot"]
        left = min(u["left"] for u in strip)
        right = max(u["right"] for u in strip)
        scale = min(1.0, g.col_w / (right - left))
        h = (bot - top) * scale
        clip = pymupdf.Rect(left, top, min(right, g.col_r), bot)
        dst = pymupdf.Rect(self.pad, self.y, self.pad + clip.width * scale, self.y + h)
        self.cur.show_pdf_page(dst, self.src, pno, clip=clip)
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
            self.cur.show_pdf_page(wdst, self.src, pno, clip=r)
        self.y += h

    def draw_note(self, n, top, bot):
        self.y += self.note_gap
        clip = pymupdf.Rect(n["left"], top, n["right"], bot)
        h, w = clip.height * self.note_scale, clip.width * self.note_scale
        x0 = self.pad + self.note_indent
        w = min(w, self.out_w - self.pad - x0)
        self.cur.show_pdf_page(pymupdf.Rect(x0, self.y, x0 + w, self.y + h), self.src, n["pno"], clip=clip)
        sh = self.cur.new_shape()
        sh.draw_line((self.pad + 5, self.y + 1), (self.pad + 5, self.y + h - 1))
        sh.finish(color=(0.55, 0.55, 0.55), width=1.2)
        sh.commit()
        self.y += h + self.note_gap

    def place_note(self, n):
        """Place a note's line bands, breaking to a new page when needed."""
        run_top = run_bot = None
        acc = 0.0
        for (bt, bb) in n["bands"]:
            bh = (bb - bt) * self.note_scale
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
        for u in seq:
            pno = u["pno"]
            h = self.unit_height(u)
            if u["type"] == "main" and self.y + pending + h > limit and (self.y + pending) > self.pad + 0.01:
                self.flush_strip(strip)
                strip, pending = [], 0.0
                self.new_page()
            self.page_map.setdefault(pno, len(self.out) - 1)
            if u["type"] == "main":
                if strip and ((u["scale"] < 1.0) != (strip[-1]["scale"] < 1.0) or strip[-1]["pno"] != pno):
                    self.flush_strip(strip)
                    strip, pending = [], 0.0
                strip.append(u)
                pending += h
            else:
                self.flush_strip(strip)
                strip, pending = [], 0.0
                self.place_note(u)
        self.flush_strip(strip)
        return self.out


def convert(src_path, dst_path, device="kindle", aspect=None, pad=8.0, note_scale=1.25,
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
    if verbose or dry_run:
        print(f"calibration: {g.describe()}", file=sys.stderr)
        print(f"output page: {g.col_w + 2 * pad:.0f} x {round((g.col_w + 2 * pad) * aspect)} pt "
              f"(aspect {aspect:.3f}, {device if not overrides else 'custom'})", file=sys.stderr)
    if dry_run:
        return None

    lay = Layout(src, g, aspect, pad, note_scale, verbose)
    units, notes = [], []
    for page in src:
        u, n = build_units(page, g, lay.usable_h, note_scale, keep_notes)
        units.extend(u)
        notes.extend(n)
    anchor_notes(units, notes, g)
    by_after = collections.defaultdict(list)
    for n in notes:
        by_after[n["after"]].append(n)
    seq = list(by_after.get(-1, []))
    for i, u in enumerate(units):
        seq.append(u)
        seq.extend(by_after.get(i, []))
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
    ap.add_argument("--note-scale", type=float, default=1.25,
                    help="magnification applied to margin notes (default: 1.25, i.e. 8pt -> 10pt)")
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
                      note_scale=args.note_scale, keep_notes=not args.no_notes,
                      overrides=overrides, verbose=args.verbose, dry_run=args.dry_run)
    except (pymupdf.FileNotFoundError, pymupdf.FileDataError) as e:
        raise SystemExit(f"error: cannot open {args.input}: {e}")
    if res:
        n_src, n_out, n_notes = res
        print(f"{args.input}: {n_src} pages -> {out}: {n_out} pages, {n_notes} margin notes inlined")
    return 0


if __name__ == "__main__":
    sys.exit(main())
