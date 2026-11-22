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


def eyelids(open_cel, closed_cel, eyes, radius):
    mask = Image.new("L", open_cel.size)
    draw = ImageDraw.Draw(mask)
    dx, dy = radius
    for x, y in eyes:
        draw.ellipse((x - dx, y - dy, x + dx, y + dy), fill=255)
    return Image.composite(closed_cel, open_cel, mask.filter(ImageFilter.GaussianBlur(2)))


def blink(open_cel, closed_cel, index, starts):
    age = next((index - start for start in starts if 0 <= index - start < 4), None)
    if age is None:
        return open_cel
    if age in (1, 2):
        return closed_cel
    return Image.blend(open_cel, closed_cel, .63)


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


def save_gif(name, frames):
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
                    duration=FRAME_MS, loop=0, transparency=255, disposal=2, optimize=False)


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    poses = split(load("poses.png"), 627)
    idle, eyes_closed, reading, thinking = [build_cel(image, STAGE, (236, 235), "feet") for image in poses]
    idle_closed = eyelids(idle, eyes_closed, ((107, 103), (142, 100)), (17, 13))
    ready = build_cel(load("wave.png"), STAGE, (236, 235), "feet")
    for state, still, count in (("idle", idle, 96), ("reading", reading, 60),
                                ("thinking", thinking, 80), ("ready", ready, 58)):
        frames = []
        for index in range(count):
            image = blink(still, idle_closed, index, (26, 78)) if state == "idle" else still
            frames.append(body_motion(image, 2 * pi * index / count, state))
        save_gif(state, frames)

    side, side_closed, bottom, bottom_closed = split(load("peeks.png"), 625)
    for state, open_pose, closed_pose, stage, size, anchor, eyes, radius in (
        ("side", side, side_closed, SIDE, (186, 246), "left", ((41, 133), (89, 150)), (25, 19)),
        ("bottom", bottom, bottom_closed, BOTTOM, (248, 214), "bottom", ((101, 161), (157, 157)), (24, 19)),
    ):
        still = build_cel(open_pose, stage, size, anchor)
        closed = eyelids(still, build_cel(closed_pose, stage, size, anchor), eyes, radius)
        still.save(OUTPUT / f"{state}.png", optimize=True)
        save_gif(state, [blink(still, closed, index, (31, 94)) for index in range(140)])


if __name__ == "__main__":
    main()
