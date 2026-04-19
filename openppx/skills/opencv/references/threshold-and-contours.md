# Thresholding And Contours

## Thresholding

Use thresholding when you need a binary image for segmentation, document cleanup, or contour extraction.

Rules:

- Convert to grayscale first.
- If lighting is uneven, consider adaptive thresholding.
- If the threshold value is unclear, try Otsu thresholding first.

```python
import cv2 as cv

gray = cv.imread(input_path, cv.IMREAD_GRAYSCALE)
if gray is None:
    raise FileNotFoundError(input_path)

_, binary = cv.threshold(gray, 0, 255, cv.THRESH_BINARY + cv.THRESH_OTSU)
```

## Contours

Contours work best when:

- the image is already binary
- the foreground object is white
- the background is black

Choose retrieval mode based on the task:

- `cv.RETR_EXTERNAL`: only outer contours
- `cv.RETR_TREE`: nested contour relationships matter

Choose approximation mode based on the task:

- `cv.CHAIN_APPROX_SIMPLE`: default for most use cases
- `cv.CHAIN_APPROX_NONE`: only when every boundary point is needed

```python
contours, hierarchy = cv.findContours(
    binary,
    cv.RETR_EXTERNAL,
    cv.CHAIN_APPROX_SIMPLE,
)
```

## Common Pitfalls

- Running `findContours()` on a noisy grayscale image instead of a clean binary image
- Forgetting to invert the image when the foreground and background polarity is wrong
- Using `RETR_TREE` when only the outer boundary is needed
