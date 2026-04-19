# OpenCV Install

Use Python package installs by default.

## PyPI Variants

Install exactly one of these in a given environment:

- `opencv-python`: main OpenCV package for most local desktop usage
- `opencv-contrib-python`: main package plus contrib modules
- `opencv-python-headless`: no GUI backends, good for servers and CI
- `opencv-contrib-python-headless`: contrib modules plus no GUI backends

## Recommended Setup

1. Create a virtual environment.
2. Upgrade `pip`, `setuptools`, and `wheel`.
3. Install one OpenCV package variant.
4. Verify with a tiny import check.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
pip install opencv-python
python -c "import cv2 as cv; print(cv.__version__)"
```

## Rules Of Thumb

- Prefer `headless` on servers, containers, and CI.
- Prefer `contrib` only when the task actually needs extra modules.
- If imports fail in an IDE but work in terminal, the IDE is probably using a different interpreter.
- If wheel install fails on an unusual platform, avoid turning the skill into a source-build tutorial unless the user explicitly needs that path.
