"""
Detects the "Combat Power Change" style notification panel inside a
screenshot by its characteristic green/olive gradient box, so it can be
cropped out and handed to ocr_prep.py.

Color profile was measured directly off +delata.png / -delata.png:
  hue   ~ 70-115 (OpenCV 0-180 scale)
  sat   ~ 25-255 (dark top of gradient is less saturated than the
                    bright green bottom)
  value ~ 20-255 (the panel is a top-to-bottom gradient, dark olive
                    at the top fading into brighter green at the bottom)

The panel's white icon/text and black text outline punch holes through
that color mask, so detection morphologically closes the mask before
taking contours -- otherwise you get a ring instead of a filled blob.
"""
import sys
import cv2
import numpy as np

# HSV bounds for the panel's green/olive gradient (OpenCV H: 0-180)
HUE_RANGE = (65, 118)
SAT_MIN = 35
VAL_MIN = 20

# banner is wide and short, and a small fraction of a real screenshot --
# reject blobs that don't look like it (this also stops a same-hued full
# background from being mistaken for the banner)
MIN_AREA_FRAC = 0.01       # blob must cover at least 1% of the image
MAX_AREA_FRAC = 0.35       # ...but no more than 35% of the image
ASPECT_RANGE = (1.4, 5.0)  # width / height


def _green_mask(bgr):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    mask = (
        (h >= HUE_RANGE[0]) & (h <= HUE_RANGE[1])
        & (s >= SAT_MIN) & (v >= VAL_MIN)
    ).astype(np.uint8) * 255

    # close holes punched by white/black text & icon inside the panel
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (21, 21))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    mask = cv2.dilate(mask, kernel, iterations=1)
    return mask


def find_green_box_regions(bgr, min_area_frac=MIN_AREA_FRAC,
                            max_area_frac=MAX_AREA_FRAC,
                            aspect_range=ASPECT_RANGE):
    """Returns a list of (x, y, w, h, score) boxes, sorted best-first."""
    h_img, w_img = bgr.shape[:2]
    img_area = h_img * w_img
    mask = _green_mask(bgr)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    results = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        area = w * h
        area_frac = area / img_area
        if area_frac < min_area_frac or area_frac > max_area_frac:
            continue
        aspect = w / float(h)
        if not (aspect_range[0] <= aspect <= aspect_range[1]):
            continue
        fill = cv2.contourArea(c) / float(area)
        # score favors bigger, more rectangular (higher fill) blobs
        score = fill * (area / img_area)
        results.append((x, y, w, h, score))

    results.sort(key=lambda r: r[-1], reverse=True)
    return results


def detect(image_path, out_path=None, min_area_frac=MIN_AREA_FRAC):
    bgr = cv2.imread(image_path, cv2.IMREAD_COLOR)
    if bgr is None:
        raise FileNotFoundError(image_path)

    boxes = find_green_box_regions(bgr, min_area_frac=min_area_frac)

    if out_path:
        annotated = bgr.copy()
        for x, y, w, h, score in boxes:
            cv2.rectangle(annotated, (x, y), (x + w, y + h), (0, 0, 255), 2)
            cv2.putText(annotated, f"{score:.2f}", (x, max(y - 5, 10)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
        cv2.imwrite(out_path, annotated)

    return boxes


def crop_best(image_path, pad=4):
    """Convenience: returns the cropped BGR region of the best detection,
    or None if nothing matched."""
    bgr = cv2.imread(image_path, cv2.IMREAD_COLOR)
    boxes = find_green_box_regions(bgr)
    if not boxes:
        return None
    x, y, w, h, _ = boxes[0]
    h_img, w_img = bgr.shape[:2]
    x0, y0 = max(x - pad, 0), max(y - pad, 0)
    x1, y1 = min(x + w + pad, w_img), min(y + h + pad, h_img)
    return bgr[y0:y1, x0:x1]


if __name__ == "__main__":
    src = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else None
    boxes = detect(src, out)
    if not boxes:
        print("no green box detected")
    for x, y, w, h, score in boxes:
        print(f"box: x={x} y={y} w={w} h={h} score={score:.3f}")
    if out:
        print(f"annotated saved: {out}")
