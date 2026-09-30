# kindleify

Re-layout a single-column PDF for an e-reader screen, without rasterizing it and
without losing margin notes.

Built for Tufte-style course readers and textbooks: a main text column plus a
margin column holding numbered sidenotes, author asides, and figure captions.
Cropping such a PDF throws the margin away. Re-flow tools (Calibre, k2pdfopt)
either mangle the layout or rasterize the text. This tool does neither.

## How it works

1. **Calibrate.** Sample the document to detect running headers and footers,
   the text column edges, the margin column, the body font size, and the line
   spacing. Every value can be overridden.
2. **Cut.** Slice each page's text column into horizontal bands at the gaps
   between lines. Figures, boxes and code blocks stay whole when they fit.
3. **Stack.** Lay the bands onto narrow pages shaped like the target screen, so
   the column fills the width. On a 6" Kindle the body text roughly doubles in
   size compared to showing the letter-size page.
4. **Inline the margin.** Each margin note is inserted right after the paragraph
   or figure it is attached to, scaled up from margin size to body size and
   marked with a gray rule. Notes break across pages at line boundaries.
5. **Keep the rest.** Bookmarks are remapped to the new pages. Metadata is kept.
   Content that spills right of the column (wide tables, long code lines) is
   scaled to fit instead of being cut.

Everything is done with vector clipping through PyMuPDF, so fonts, math, code
and figures are the original vectors.

## Install

```sh
pip install pymupdf pillow
python kindleify.py --help
```

or, without installing anything:

```sh
uvx --from git+https://github.com/yuliaknut/kindleify kindleify reader.pdf
```

## Usage

```sh
kindleify reader.pdf                          # writes reader.kindle.pdf
kindleify reader.pdf -o out.pdf --device paperwhite
kindleify reader.pdf --scale 1.3              # body text 30% larger, re-wrapped
kindleify reader.pdf --dry-run                # print the detected geometry only
kindleify paper.pdf --no-notes                # drop the margin column instead of inlining it
kindleify reader.pdf --header-y 50 --split 400  # override detected geometry
```

Devices: `kindle` (6", default), `paperwhite`, `oasis`, `scribe`, `kobo`,
`remarkable`, or `--aspect H/W` for anything else. The device only sets the
page aspect ratio. The reader scales the page to fit, so without `--scale` the
text size you get is set by the width of the source text column.

### Larger text: `--scale`

```sh
kindleify reader.pdf --scale 1.3
```

`--scale` re-wraps body paragraphs word by word. Each word is still a clip of
the source page, so glyphs, kerning, ligatures, bold and italic runs, inline
code and superscript note marks are untouched; only the line breaks and the
inter-word spacing are recomputed, and lines are re-justified. Headings and
other short lines are enlarged by the same factor. Figures, boxes, code
listings, display math and tables are enlarged only as far as the column allows,
so they keep their layout. Margin notes follow the body size.

Paragraph boundaries are inferred from the layout (indentation, bullets,
vertical space, short last lines with sentence punctuation), which is reliable
on LaTeX output but is a heuristic: expect the occasional early line break at a
line the source left short. A single token wider than the column, such as a
long URL, shrinks its own line to fit.

On a 6" Kindle, 1.3 turns 10pt body text into roughly 10pt on screen (from
about 7.7pt without it); on a Paperwhite, about 11.5pt.

Run with `--dry-run` first on a new document and sanity-check the numbers: the
header cutoff, the text column, and whether a margin column was detected. Then
skim the output. Any run on a new template is a draft.

## What it handles

- Single text column with justified body text, LaTeX or similar.
- Enlarging the body text with a real word-level re-wrap (`--scale`).
- A margin column on the right with sidenotes, asides, and captions.
- Running headers and footers, including bare page numbers.
- Figures, shaded boxes, code listings (including line numbers in the left
  margin), display math, and full-width tables.
- Paragraphs and notes that continue across source pages.

## What it does not handle

- **Scanned PDFs.** There is no text layer to work from.
- **Two-column papers.** The columns would be interleaved.
- **Designed brochures and slides.** Full-bleed backgrounds and floating text
  boxes have no column to detect.
- **Margin notes on the left**, or alternating sides.
- **Re-flowing anything but plain paragraphs.** Code, math and tables keep
  their source line breaks, so they cannot grow beyond the column width.
- The printed table of contents keeps its old page numbers, since it is part
  of the page image. The PDF bookmarks are correct.

## Origin

Written to make a 165-page evaluation course reader readable on a 6" Kindle.
The result was 255 pages with 124 margin notes inlined, verified by a word-count
diff against the source and a visual skim.

## License

MIT
