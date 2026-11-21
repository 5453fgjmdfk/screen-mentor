"""Render the original Screen Mentor icon and distributable default mascot."""
import argparse
from math import sin, tau
from pathlib import Path

from PIL import Image, ImageDraw


INK = "#211735"
PLUM = "#694999"
LILAC = "#b798f0"
ICE = "#f3eaff"
MINT = "#a9ebd8"


def make_icon():
    image = Image.new("RGBA", (512, 512))
    background = Image.new("RGBA", image.size)
    color = ImageDraw.Draw(background)
    for y in range(background.height):
        amount = y / 511
        color.line((0, y, 512, y), fill=tuple(round(a * (1 - amount) + b * amount)
                                             for a, b in zip((65, 43, 98), (28, 23, 47))))
    mask = Image.new("L", image.size)
    ImageDraw.Draw(mask).rounded_rectangle((14, 14, 498, 498), radius=120, fill=255)
    image.paste(background, mask=mask)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((19, 19, 493, 493), radius=115, outline="#7961a5", width=5)

    for x, y, sx, sy in ((102, 113, 1, 1), (410, 113, -1, 1),
                         (102, 399, 1, -1), (410, 399, -1, -1)):
        draw.line((x + sx * 50, y, x, y, x, y + sy * 50), fill=LILAC, width=17, joint="curve")
        draw.ellipse((x - 8, y - 8, x + 8, y + 8), fill=LILAC)

    draw.polygon(((162, 240), (141, 189), (158, 150), (183, 208), (208, 233)), fill=MINT)
    draw.polygon(((350, 240), (371, 189), (354, 150), (329, 208), (304, 233)), fill=MINT)
    draw.ellipse((143, 205, 369, 362), fill="#6e55aa")
    draw.ellipse((143, 196, 369, 349), fill=ICE)
    draw.rounded_rectangle((200, 249, 221, 285), radius=10, fill=PLUM)
    draw.rounded_rectangle((291, 249, 312, 285), radius=10, fill=PLUM)
    draw.arc((236, 270, 277, 299), 19, 161, fill=PLUM, width=7)
    draw.ellipse((397, 193, 419, 215), fill=MINT)
    draw.ellipse((429, 165, 442, 178), fill=MINT)
    return image


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
