# Video I/O

Use simple, defensive loops for video work.

## Read Video Safely

```python
import cv2 as cv

cap = cv.VideoCapture(input_path)
if not cap.isOpened():
    raise RuntimeError(f"Cannot open video: {input_path}")

while True:
    ret, frame = cap.read()
    if not ret:
        break
    # process frame

cap.release()
```

## Write Video Safely

```python
import cv2 as cv

fourcc = cv.VideoWriter_fourcc(*"mp4v")
writer = cv.VideoWriter(output_path, fourcc, fps, (width, height))
if not writer.isOpened():
    raise RuntimeError(f"Cannot open writer: {output_path}")

writer.write(frame)
writer.release()
```

## Rules Of Thumb

- Check `cap.isOpened()` before entering the loop.
- Check `ret` on every `read()`.
- Release both capture and writer objects.
- Keep output frame size consistent with the `VideoWriter` configuration.
- Prefer conservative codecs like `mp4v` unless the environment already standardizes something else.

## Common Pitfalls

- The writer fails because frame size does not match the configured output size.
- Video opens on one machine but not another because the codec is unavailable.
- A loop hangs or crashes because `ret` is ignored.
