# Image Basics

## Core Conventions

- OpenCV usually uses BGR channel order.
- Many classic operations work best on grayscale images.
- `cv.imread()` returns `None` on failure, so always check it.
- Save intermediate outputs when debugging a pipeline.

## Typical Pipeline

For many tasks, start from this structure:

1. `cv.imread()`
2. `cv.cvtColor(..., cv.COLOR_BGR2GRAY)` if the next step expects grayscale
3. optional blur such as `cv.GaussianBlur()` or `cv.medianBlur()`
4. threshold, edge detection, transform, or morphology
5. save with `cv.imwrite()`

## Minimal Example

```python
import cv2 as cv

image = cv.imread(input_path, cv.IMREAD_COLOR)
if image is None:
    raise FileNotFoundError(input_path)

gray = cv.cvtColor(image, cv.COLOR_BGR2GRAY)
blurred = cv.GaussianBlur(gray, (5, 5), 0)
cv.imwrite(output_path, blurred)
```

## Common Pitfalls

- Colors look wrong in matplotlib because you forgot BGR to RGB conversion.
- Thresholding or contour extraction behaves poorly because you skipped grayscale conversion or denoising.
- Debugging is slow because no intermediate images are saved.
