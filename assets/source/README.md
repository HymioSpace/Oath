# README artwork

`../oath-mark.svg` and `../oath-banner.svg` are editable vector artwork. The keeper's raised hand is a small visual reference to an oath; its blue ribbon connects it to HymioSpace's palette.

`render_demo.py` draws the synthetic client storyboard in `../oath-offline-demo.gif`. It illustrates supported offline execution and replay; it does not record a bundled UI or a live model session. The frame sequence shows a task received, a client disconnect, continued work, a saved reply, and ordered replay when the user returns.

The loop lasts about 11 seconds. Three changing headlines identify the task, disconnect, and return; a progress bar and highlighted server panel make continued execution visible during the offline segment.

To regenerate with Python and Pillow installed:

```bash
python assets/source/render_demo.py
```

The renderer uses Arial on macOS or DejaVu Sans elsewhere. Artwork is covered by the repository's MIT license.
