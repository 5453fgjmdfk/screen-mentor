"""Build the local whale-maid skin from the transparent pose sheets."""
from math import cos, pi, sin
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


SOURCE = Path(__file__).resolve().parent
OUTPUT = SOURCE.parents[1] / "codex-plugin" / "assets" / "companion" / "whale-maid"
STAGE = (256, 256)
SIDE = (190, 250)
BOTTOM = (256, 214)
FRAME_MS = 50
BLINK_MS = 20
BLINK_PROGRESS = (.22, .53, .84, 1, 1, .89, .69, .46, .25, .10)


def load(name):
    with Image.open(SOURCE / name) as image:
        return image.convert("RGBA")


def build_cel(image, stage, fit, anchor):
    visible = image.getchannel("A").point(lambda alpha: 255 if alpha >= 150 else 0)
    image = image.crop(visible.getbbox())
    image.thumbnail(fit, Image.Resampling.LANCZOS)
    x = 0 if anchor == "left" else (stage[0] - image.width) // 2
    y = stage[1] - image.height - (8 if anchor == "feet" else 0)
    cel = Image.new("RGBA", stage)
    cel.paste(image, (x, y))
    return cel


def split(sheet, midpoint_y):
    midpoint_x = sheet.width // 2
    return [sheet.crop((left, top, right, bottom))
            for top, bottom in ((0, midpoint_y), (midpoint_y, sheet.height))
            for left, right in ((0, midpoint_x), (midpoint_x, sheet.width))]


def eye_mask(size, eyes, radius):
    mask = Image.new("L", size)
    draw = ImageDraw.Draw(mask)
    dx, dy = radius
    for x, y in eyes:
        draw.ellipse((x - dx, y - dy, x + dx, y + dy), fill=255)
    return mask.filter(ImageFilter.GaussianBlur(2))


def eye_mesh(eyes, radius, progress):
    # Compress the iris and lashes towards the closing line while the outer skin stays in place.
    dx, dy = radius
    for x, y in eyes:
        left, right = x - dx, x + dx + 1
        top, bottom = y - dy, y + dy + 1
        upper, lower = y - round(dy * .67), y + round(dy * .70)
        closing_line = y + round(dy * .14)
        warped_upper = round(upper + (closing_line - upper) * progress)
        warped_lower = max(warped_upper + 1, round(lower + (closing_line - lower) * progress))
        source = (top, upper, lower, bottom)
        target = (top, warped_upper, warped_lower, bottom)
        for before, after, begin, end in zip(source, source[1:], target, target[1:]):
            yield ((left, begin, right, end),
                   (left, before, left, after, right, after, right, before))


def blink_cels(open_cel, closed_cel, eyes, radius):
    mask = eye_mask(open_cel.size, eyes, radius)
    # Keep the blend within the transformed area, including the soft edges of the eye patches.
    mesh_radius = (radius[0] + 6, radius[1] + 6)
    closed = Image.composite(closed_cel, open_cel, mask)
    for progress in BLINK_PROGRESS:
        if progress == 1:
            yield closed
            continue
        mesh = list(eye_mesh(eyes, mesh_radius, progress))
        warped = open_cel.transform(open_cel.size, Image.Transform.MESH, mesh, Image.Resampling.BICUBIC)
        cel = Image.composite(warped, open_cel, mask)
        # Only switch to the drawn closed lash once the iris is nearly flat.
        closed_weight = max(0, (progress - .65) / .35)
        yield Image.blend(cel, closed, closed_weight) if closed_weight else cel


def blink_timeline(still, cels, starts, loop_ms, steady_ms):
    previous = 0
    for start in (*starts, loop_ms):
        for at in range(previous, start, steady_ms):
            yield at, still, min(steady_ms, start - at)
        if start < loop_ms:
            for index, cel in enumerate(cels):
                yield start + index * BLINK_MS, cel, BLINK_MS
        previous = start + len(cels) * BLINK_MS


def body_motion(cel, phase, state):
    if state == "idle":
        anchor, stretch, angle, bob = 248, .009 * sin(phase), .36 * sin(phase), 0
    elif state == "reading":
        anchor, stretch, angle, bob = 245, .008 * sin(phase), 1.65 * sin(phase), 1.5 * sin(phase)
    elif state == "thinking":
        anchor, stretch, angle, bob = 248, .009 * sin(phase), 1.55 * sin(phase), 0
    else:
        # A small whole-body greeting bounce, followed by two small swings.
        greeting = (.5 - .5 * cos(phase)) ** 2
        anchor, stretch, angle, bob = 248, -.014 * greeting, 2 * greeting * sin(2 * phase), 5.5 * greeting
    scale = 1 + stretch
    image = cel.transform(cel.size, Image.Transform.AFFINE, (1, 0, 0, 0, 1 / scale, anchor * (1 - 1 / scale) + bob),
                          resample=Image.Resampling.BICUBIC)
    return image.rotate(angle, resample=Image.Resampling.BICUBIC, center=(128, anchor - 24))


def save_gif(name, frames, durations=FRAME_MS):
    samples = []
    for frame in frames[::3]:
        pixels = frame.tobytes()
        for offset in range(0, len(pixels), 52):
            if pixels[offset + 3] >= 128:
                samples.append(tuple(pixels[offset:offset + 3]))
    swatches = Image.new("RGB", (len(samples), 1))
    swatches.putdata(samples)
    palette = swatches.quantize(colors=255)
    indexed = []
    for frame in frames:
        colors = frame.convert("RGB").quantize(palette=palette, dither=Image.Dither.NONE)
        colors.paste(255, mask=frame.getchannel("A").point(lambda alpha: 255 if alpha < 128 else 0))
        indexed.append(colors)
    indexed[0].save(OUTPUT / f"{name}.gif", save_all=True, append_images=indexed[1:],
                    duration=durations, loop=0, transparency=255, disposal=2, optimize=False)


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    poses = split(load("poses.png"), 627)
    idle, eyes_closed, reading, thinking = [build_cel(image, STAGE, (236, 235), "feet") for image in poses]
    cels = tuple(blink_cels(idle, eyes_closed, ((107, 103), (142, 100)), (17, 13)))
    idle_timeline = list(blink_timeline(idle, cels, (1300, 3900), 4800, FRAME_MS))
    idle_frames = [body_motion(cel, 2 * pi * at / 4800, "idle") for at, cel, _ in idle_timeline]
    save_gif("idle", idle_frames, [duration for _, _, duration in idle_timeline])

    ready = build_cel(load("wave.png"), STAGE, (236, 235), "feet")
    for state, still, count in (("reading", reading, 60), ("thinking", thinking, 80), ("ready", ready, 58)):
        frames = [body_motion(still, 2 * pi * index / count, state) for index in range(count)]
        save_gif(state, frames)

    side, side_closed, bottom, bottom_closed = split(load("peeks.png"), 625)
    for state, open_pose, closed_pose, stage, size, anchor, eyes, radius in (
        ("side", side, side_closed, SIDE, (186, 246), "left", ((41, 133), (89, 150)), (25, 19)),
        ("bottom", bottom, bottom_closed, BOTTOM, (248, 214), "bottom", ((101, 161), (157, 157)), (24, 19)),
    ):
        still = build_cel(open_pose, stage, size, anchor)
        closed = build_cel(closed_pose, stage, size, anchor)
        cels = tuple(blink_cels(still, closed, eyes, radius))
        still.save(OUTPUT / f"{state}.png", optimize=True)
        timeline = list(blink_timeline(still, cels, (1550, 4700), 7000, 7000))
        save_gif(state, [cel for _, cel, _ in timeline], [duration for _, _, duration in timeline])


if __name__ == "__main__":
    main()
