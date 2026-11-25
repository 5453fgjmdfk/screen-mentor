"""Refine coarse model coordinates using nearby straight control borders."""
from statistics import median


def tighten(frame, image_size, rect):
    sx, sy = frame.width / image_size[0], frame.height / image_size[1]
    x, y, w, h = (rect[k] for k in ("x", "y", "width", "height"))
    left, right, top, bottom = x * sx, (x + w) * sx, y * sy, (y + h) * sy
    if min(right - left, bottom - top) < 8:
        return rect
    dx, dy = min(80, (right - left) * .34), min(48, (bottom - top) * .40)
    l, t = max(0, int(left - dx - 3)), max(0, int(top - dy - 3))
    r, b = min(frame.width, int(right + dx + 4)), min(frame.height, int(bottom + dy + 4))
    if r - l < 8 or b - t < 8:
        return rect
    pixels = frame.crop((l, t, r, b)).convert("RGB").load()
    left, right, top, bottom = left - l, right - l, top - t, bottom - t

    def edge(origin, radius, start, end, vertical, required_sign=0):
        normal, tangent = (r - l, b - t) if vertical else (b - t, r - l)
        first, last = max(0, round(start)), min(tangent - 1, round(end))
        if last - first < 4:
            return origin, 0
        positions = range(first, last + 1, max(1, (last - first) // 25))
        best, sign, score = origin, 0, 0
        for i in range(max(1, round(origin - radius)), min(normal - 1, round(origin + radius) + 1)):
            diffs = [tuple(a - b for a, b in zip(pixels[i, j], pixels[i - 1, j]))
                     if vertical else tuple(a - b for a, b in zip(pixels[j, i], pixels[j, i - 1]))
                     for j in positions]
            contrast = max((median(channel) for channel in zip(*diffs)), key=abs)
            direction = 1 if contrast > 0 else -1
            if required_sign and direction != required_sign:
                continue
            strength = abs(contrast)
            candidate = strength - 7 * abs(i - origin) / max(1.0, radius)
            if strength >= 13 and candidate > score:
                best, sign, score = i, direction, candidate
        return best, sign

    x_inner = (left + (right - left) * .26, right - (right - left) * .26)
    upper, top_found = edge(top, dy, *x_inner, False)
    lower, bottom_found = edge(bottom, dy, *x_inner, False, -top_found)
    if not (top_found and bottom_found and lower - upper >= (bottom - top) * .56):
        upper, lower = top, bottom
    y_inner = (upper + (lower - upper) * .15, lower - (lower - upper) * .15)
    start, left_found = edge(left, dx, *y_inner, True)
    end, right_found = edge(right, dx, *y_inner, True, -left_found)
    if not (left_found and right_found and end - start >= (right - left) * .56):
        start, end = left, right
    return {"x": (start + l) / sx, "y": (upper + t) / sy,
            "width": (end - start) / sx, "height": (lower - upper) / sy}
