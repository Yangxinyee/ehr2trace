# Positioning figure

`system_framework.pptx` places this repository among the components a patient world
model or a clinical agent system needs. It is a talk and overview figure.

The five stages read left to right. Stage **b** is what this repository
implements. Stages **c** to **e** are drawn dashed because they are requirements this
repository identifies and does not deliver; that distinction is the
point of the figure. A feedback loop returns from evaluation to the contract, and a
band underneath names the properties stage b establishes for everything downstream.

`system_framework.pdf` is a vector export of the same slide.

Figure 1a of the paper is the TikZ counterpart of this strip, compact enough to sit
above the system diagram. This deck is the version for talks: wider, with icons, and
with room to say what each stage involves.

## Rebuild

```sh
pip install python-pptx
python3 tools/make_framework_figure.py
```

`python-pptx` is needed only for this figure and is deliberately not a project
dependency. The PDF export step runs when LibreOffice is on `PATH` and is skipped
otherwise.

## Editing

Every mark is a native PowerPoint object: cards and rules are autoshapes, the icons
are grouped vector custom geometry, and all text is live text. Nothing is a raster
image, so the deck can be recoloured, retyped, and resized in PowerPoint or
LibreOffice without returning to the generator. Regenerating overwrites manual
edits, so change the generator when a change should persist.

Colours carry the same distinction: teal for what
is implemented, orange for what is required but absent, grey for external input.
Subscripts are real subscript runs rather than Unicode subscript characters, which
Arial does not carry.
