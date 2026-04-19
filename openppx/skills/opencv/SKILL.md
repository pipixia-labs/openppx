---
name: opencv
description: Use OpenCV for Python image processing, contour analysis, thresholding, filtering, geometric transforms, and basic video I/O. Trigger when the user asks to process images or video with cv2/OpenCV, or when a task clearly fits classical computer vision instead of a remote VLM.
---

# OpenCV

Use this skill when the user wants local computer-vision work with OpenCV, especially:

- image filtering, thresholding, morphology, contours, or edge detection
- resize, crop, rotate, perspective transform, or format conversion
- camera or video frame read/write with `cv2.VideoCapture` or `cv2.VideoWriter`
- classical vision pipelines that should run locally in Python

Prefer this skill for deterministic image processing. If the task is mainly semantic understanding, captioning, or open-ended visual reasoning, consider an image-understanding expert instead of forcing OpenCV.

## Workflow

1. Confirm the real task type first: image enhancement, segmentation, contour extraction, geometry transform, or video I/O.
2. Start with the smallest reproducible pipeline and save intermediate outputs when debugging.
3. Use Python and import OpenCV as `cv2 as cv` unless the surrounding code clearly uses a different style.
4. For image-processing tasks, prefer a simple pipeline such as read -> colorspace convert -> threshold or filter -> morphology or contours -> save result.
5. For video tasks, check `cap.isOpened()`, check `ret` on every frame, and always release capture and writer objects.
6. If parameter choices are unclear, expose them as function arguments instead of hard-coding many magic numbers.
7. When the exact API behavior matters, read the relevant skill reference file before coding.

## Reference Map

Read these skill-local references on demand instead of expanding the skill with long examples:

- Installation and package choice:
  `references/install.md`
- Image-processing basics:
  `references/image-basics.md`
- Thresholding and contours:
  `references/threshold-and-contours.md`
- Video capture and writing:
  `references/video-io.md`

If a request goes beyond these references, keep the implementation conservative and prefer simple, well-known OpenCV APIs over speculative or heavyweight patterns.

## Guardrails

- OpenCV images are usually BGR, not RGB. Convert explicitly when mixing with PIL or matplotlib.
- `cv.threshold()` and many related operations assume grayscale input; do not skip the colorspace conversion step.
- Contour detection works best on binary images, and the foreground should usually be white on black.
- For server or CI environments, prefer `opencv-python-headless` or `opencv-contrib-python-headless`.
- Install exactly one OpenCV PyPI variant per environment unless there is a very specific reason not to.
- Do not drop in heavyweight DNN examples unless the user actually needs them and the model files are available locally.
- When debugging a broken pipeline, save intermediate files rather than guessing which stage failed.

## Minimal Patterns

### Read and threshold an image

```python
import cv2 as cv

image = cv.imread(input_path, cv.IMREAD_COLOR)
if image is None:
    raise FileNotFoundError(input_path)

gray = cv.cvtColor(image, cv.COLOR_BGR2GRAY)
_, binary = cv.threshold(gray, 0, 255, cv.THRESH_BINARY + cv.THRESH_OTSU)
cv.imwrite(output_path, binary)
```

### Find outer contours

```python
import cv2 as cv

image = cv.imread(input_path, cv.IMREAD_GRAYSCALE)
if image is None:
    raise FileNotFoundError(input_path)

_, binary = cv.threshold(image, 0, 255, cv.THRESH_BINARY + cv.THRESH_OTSU)
contours, _ = cv.findContours(binary, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE)
```

### Safe video loop

```python
import cv2 as cv

cap = cv.VideoCapture(input_path)
if not cap.isOpened():
    raise RuntimeError(f"Cannot open video: {input_path}")

while True:
    ret, frame = cap.read()
    if not ret:
        break
    # process frame here

cap.release()
```
