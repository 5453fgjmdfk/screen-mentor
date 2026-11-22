"""Render the original Screen Mentor icon and distributable default mascot."""
import argparse
from math import sin, tau
from pathlib import Path

from PIL import Image, ImageDraw


PLUM = "#694999"
LILAC = "#b798f0"
ICE = "#f3eaff"
MINT = "#a9ebd8"


def make_icon():
    image = Image.new("RGBA", (1024, 1024))
    draw = ImageDraw.Draw(image)

    def rounded(box, radius, color):
        draw.rounded_rectangle(tuple(value * 2 for value in box), radius=radius * 2, fill=color)

    rounded((16, 16, 496, 496), 106, "#283D4A")
    frame = "#E5ECEF"
    for box in ((123, 123, 251, 161), (123, 123, 161, 251),
                (261, 351, 389, 389), (351, 261, 389, 389)):
        rounded(box, 19, frame)
    rounded((210, 220, 306, 292), 15, "#9CB9C7")
    return image.resize((512, 512), Image.Resampling.LANCZOS)


def make_portrait(side, closed):
    size = (552, 744) if side else (668, 543)
    image = Image.new("RGBA", size)
    draw = ImageDraw.Draw(image)
    if side:
        face = (70, 200, 490, 595)
        eyes = ((155, 343), (305, 405))
        horns = [((135, 260), (72, 116), (117, 79), (211, 231)),
                 ((326, 249), (350, 100), (394, 134), (418, 284))]
        paws = ((1, 390, 99, 468), (1, 502, 105, 580))
        body = (54, 474, 432, 733)
        smile = (180, 414, 248, 465)
        ears = ((48, 306, 147, 461), (392, 321, 523, 471))
    else:
        face = (79, 160, 590, 524)
        eyes = ((240, 407), (406, 402))
        horns = [((153, 238), (118, 104), (174, 69), (243, 209)),
                 ((421, 213), (485, 76), (534, 108), (505, 245))]
        paws = ((158, 452, 294, 543), (372, 452, 508, 543))
        body = (112, 340, 552, 542)
        smile = (305, 422, 361, 459)
        ears = ((57, 265, 182, 395), (488, 252, 617, 382))
    draw.ellipse(body, fill=LILAC, outline=PLUM, width=10)
    for horn in horns:
        draw.polygon(horn, fill=MINT, outline=PLUM, width=8)
    for ear in ears:
        draw.ellipse(ear, fill=LILAC, outline=PLUM, width=8)
    draw.ellipse(face, fill=ICE, outline=PLUM, width=10)
    for x, y in eyes:
        if closed:
            draw.arc((x - 23, y - 19, x + 23, y + 15), 15, 165, fill=PLUM, width=8)
        else:
            draw.rounded_rectangle((x - 17, y - 27, x + 17, y + 26), radius=15, fill=PLUM)
            draw.ellipse((x + 2, y - 17, x + 9, y - 10), fill=ICE)
    draw.arc(smile, 20, 160, fill=PLUM, width=7)
    for x1, y1, x2, y2 in paws:
        draw.rounded_rectangle((x1, y1, x2, y2), radius=33, fill=ICE, outline=PLUM, width=8)
        for step in (1, 2):
            x = x1 + (x2 - x1) * (step + 1) // 4
            draw.line((x, y2 - 7, x, y2 - 24), fill="#bd9edb", width=6)
    return image


def make_poses(folder):
    for closed, name in ((False, "peek-poses.png"), (True, "peek-blink.png")):
        sheet = Image.new("RGBA", (1774, 887))
        sheet.paste(make_portrait(True, closed), (185, 77))
        sheet.paste(make_portrait(False, closed), (995, 207))
        sheet.save(folder / name, optimize=True)


def make_loop(folder, state):
    frames = []
    for index in range(16):
        high = state == "ready" and 3 <= index <= 8
        y = 4 + round(3 * sin(index / 16 * tau)) - (5 if high else 0)
        pose = make_portrait(False, index in (9, 10)).resize((145, 118), Image.Resampling.LANCZOS)
        frame = Image.new("RGBA", (160, 160))
        frame.alpha_composite(pose, (7, 25 + y))
        draw = ImageDraw.Draw(frame)
        if state != "idle":
            for dot in range(1 if state == "ready" else 3):
                x = 103 + dot * 12
                tint = MINT if (index + dot) % 5 < 3 else LILAC
                draw.ellipse((x, 17, x + 6, 23), fill=tint)
        rgb = frame.convert("RGB").quantize(colors=255)
        rgb.paste(255, mask=frame.getchannel("A").point(lambda value: 255 if value < 105 else 0))
        frames.append(rgb)
    frames[0].save(folder / f"{state}.gif", save_all=True, append_images=frames[1:],
                   duration=105, loop=0, disposal=2, transparency=255, optimize=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--public-mascot", action="store_true")
    args = parser.parse_args()
    args.assets.mkdir(parents=True, exist_ok=True)
    icon = make_icon()
    icon.save(args.assets / "app-icon.png", optimize=True)
    icon.save(args.assets / "app-icon.ico", sizes=[(edge, edge) for edge in (16, 24, 32, 48, 64, 96, 128, 256)])
    if args.public_mascot:
        folder = args.assets / "companion"
        folder.mkdir(exist_ok=True)
        make_poses(folder)
        for state in ("idle", "reading", "thinking", "ready"):
            make_loop(folder, state)


if __name__ == "__main__":
    main()
