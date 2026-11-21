"""Skin image limits, sprite-sheet selection and GIF export."""
from io import BytesIO
from math import floor

from PIL import Image
from PyQt6.QtCore import QPoint, QRect, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QDialog, QFormLayout, QHBoxLayout, QLabel, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)


MIB = 1024 * 1024
FILE_LIMIT = 8 * MIB
FRAME_SIDE = 1024
SHEET_SIDE = 4096
FRAME_COUNT = 180
PIXEL_BUDGET = 28_000_000
STATES = ("idle", "reading", "thinking", "ready")
ROLES = ("idle", "side", "bottom")
ASSET_FILES = {f"{state}.gif": "GIF" for state in STATES}
ASSET_FILES.update({f"{role}.{extension}": extension.upper() for role in ROLES[1:]
                    for extension in ("gif", "png")})


def require_roles(names):
    return "idle.gif" in names and all(any(f"{role}.{ext}" in names for ext in ("gif", "png"))
                                      for role in ROLES[1:])


def asset_bytes(path):
    try:
        with path.open("rb") as file:
            raw = file.read(FILE_LIMIT + 1)
    except OSError:
        raise ValueError("无法读取素材，请确认文件仍在原来的位置。") from None
    if len(raw) > FILE_LIMIT:
        raise ValueError(f"{path.name} 超出 8 MB。")
    return raw


def limit_frames(width, height, count):
    return (1 <= min(width, height) and max(width, height) <= FRAME_SIDE
            and 1 <= count <= FRAME_COUNT and width * height * count <= PIXEL_BUDGET)


def has_alpha(image):
    return "A" in image.getbands() or "transparency" in image.info


def check_image(filename, raw):
    try:
        with Image.open(BytesIO(raw)) as image:
            if image.format != ASSET_FILES[filename]:
                raise ValueError
            if image.format == "PNG" and (image.is_animated or not has_alpha(image)):
                raise ValueError
            index = 0
            visible = False
            while True:
                if not limit_frames(*image.size, index + 1):
                    raise ValueError
                image.load()
                if not visible:
                    visible = image.convert("RGBA").getbbox() is not None
                index += 1
                try:
                    image.seek(index)
                except EOFError:
                    break
    except (OSError, ValueError, Image.DecompressionBombError, SyntaxError):
        raise ValueError(
            f"{filename} 无法使用。单帧边长需在 1024 px 以内，最多 180 帧，"
            "总像素不超过 2800 万。PNG 须为单张透明图。"
        ) from None
    if not visible:
        raise ValueError(f"{filename} 的所有帧均为空白，请重新选择包含角色的区域。")


def open_sheet(raw):
    try:
        with Image.open(BytesIO(raw)) as image:
            if image.format != "PNG" or image.is_animated or not has_alpha(image) or max(image.size) > SHEET_SIDE:
                raise ValueError
            return image.convert("RGBA")
    except (OSError, ValueError, Image.DecompressionBombError, SyntaxError):
        raise ValueError("精灵图需为单张透明 PNG，长宽均不超过 4096 px。") from None


def crop_sheet(sheet, region, columns, rows, rate, role):
    x, y, width, height = region.getRect()
    count = columns * rows
    if (not limit_frames(width, height, count) or not 1 <= rate <= 30 or x < 0 or y < 0
            or x + columns * width > sheet.width or y + rows * height > sheet.height):
        raise ValueError("裁剪区域超出精灵图，或超出单帧尺寸、帧数限制。")
    frames = []
    for row in range(rows):
        for column in range(columns):
            left, top = x + column * width, y + row * height
            frames.append(sheet.crop((left, top, left + width, top + height)))
    if not any(frame.getbbox() for frame in frames):
        raise ValueError("裁剪范围内没有可见图像，请重新选择区域。")
    result = BytesIO()
    if count == 1 and role in ROLES[1:]:
        frames[0].save(result, "PNG")
        extension = "png"
    else:
        indexed = []
        for image in frames:
            transparent = image.getchannel("A").point(lambda value: 255 if value < 128 else 0)
            frame = image.convert("RGB").quantize(colors=255)
            frame.paste(255, mask=transparent)
            indexed.append(frame)
        indexed[0].save(result, "GIF", save_all=True, append_images=indexed[1:],
                        duration=round(1000 / rate), loop=0, transparency=255, disposal=2, optimize=False)
        extension = "gif"
    raw = result.getvalue()
    if len(raw) > FILE_LIMIT:
        raise ValueError("裁剪后的素材超过 8 MB，请减少帧数或缩小每帧尺寸。")
    return f"{role}.{extension}", raw


class SheetCanvas(QWidget):
    frame_selected = pyqtSignal(QRect)

    def __init__(self, image):
        super().__init__()
        self.image = image
        self.frame = QRect()
        self.columns = self.rows = 1
        self.start = self.end = None
        self.setMinimumSize(435, 238)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setAccessibleName("拖动选择第一帧的裁剪范围")

    def destination(self):
        size = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        return QRectF((self.width() - size.width()) / 2, (self.height() - size.height()) / 2,
                      size.width(), size.height())

    def position_in_sheet(self, point):
        target = self.destination()
        x = (point.x() - target.left()) * self.image.width() / target.width()
        y = (point.y() - target.top()) * self.image.height() / target.height()
        return QPoint(min(self.image.width(), max(0, floor(x))), min(self.image.height(), max(0, floor(y))))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.destination().contains(event.position()):
            self.start = self.end = self.position_in_sheet(event.position())
            self.update()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.start is not None:
            self.end = self.position_in_sheet(event.position())
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.start is not None:
            self.end = self.position_in_sheet(event.position())
            chosen = self.dragged_frame()
            self.start = self.end = None
            if chosen.isValid():
                self.frame_selected.emit(chosen)
            self.update()
        else:
            super().mouseReleaseEvent(event)

    def dragged_frame(self):
        x, y = min(self.start.x(), self.end.x()), min(self.start.y(), self.end.y())
        return QRect(x, y, abs(self.start.x() - self.end.x()), abs(self.start.y() - self.end.y()))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#13111d"))
        target = self.destination()
        painter.fillRect(target, QColor("#3b334b"))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(target, self.image, QRectF(self.image.rect()))
        frame = self.dragged_frame() if self.start is not None else self.frame
        if not frame.isValid():
            return
        scale_x, scale_y = target.width() / self.image.width(), target.height() / self.image.height()
        for row in range(self.rows):
            for column in range(self.columns):
                x, y = frame.x() + column * frame.width(), frame.y() + row * frame.height()
                if x >= self.image.width() or y >= self.image.height():
                    continue
                bounds = QRectF(target.x() + x * scale_x, target.y() + y * scale_y,
                                frame.width() * scale_x, frame.height() * scale_y)
                selected = row == column == 0
                painter.setPen(QPen(QColor("#e2cfff" if selected else "#9378bd"), 1))
                if selected:
                    painter.fillRect(bounds, QColor(192, 161, 237, 45))
                painter.drawRect(bounds.intersected(target))


class CropDialog(QDialog):
    def __init__(self, parent, raw, role):
        super().__init__(parent)
        self.sheet = open_sheet(raw)
        self.role = role
        self.asset = None
        bitmap = QPixmap()
        if not bitmap.loadFromData(raw, "PNG"):
            raise ValueError("无法预览这张 PNG 精灵图。")
        self.setWindowTitle("裁剪精灵图")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"原图 {self.sheet.width} × {self.sheet.height} px · 框选第一帧，再填写帧网格"))
        self.canvas = SheetCanvas(bitmap)
        layout.addWidget(self.canvas)
        form = QFormLayout()
        layout.addLayout(form)
        self.x = self.spin("起点 X", 0, self.sheet.width - 1, 0)
        self.y = self.spin("起点 Y", 0, self.sheet.height - 1, 0)
        self.width_box = self.spin("单帧宽", 1, min(FRAME_SIDE, self.sheet.width), min(FRAME_SIDE, self.sheet.width))
        self.height_box = self.spin("单帧高", 1, min(FRAME_SIDE, self.sheet.height), min(FRAME_SIDE, self.sheet.height))
        self.columns = self.spin("列", 1, 30, 1)
        self.rows = self.spin("行", 1, 30, 1)
        self.rate = self.spin("FPS", 1, 30, 10)
        for label, boxes in (("位置（px）", (self.x, self.y)), ("单帧尺寸（px）", (self.width_box, self.height_box)),
                             ("帧网格", (self.columns, self.rows, self.rate))):
            group = QHBoxLayout()
            for box in boxes:
                group.addWidget(QLabel(box.accessibleName()))
                group.addWidget(box)
                box.valueChanged.connect(self.update_selection)
            group.addStretch()
            form.addRow(label, group)
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        buttons = QHBoxLayout()
        buttons.addStretch()
        layout.addLayout(buttons)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.use = QPushButton("使用裁剪结果")
        self.use.setObjectName("apply")
        self.use.clicked.connect(self.save)
        buttons.addWidget(self.use)
        self.canvas.frame_selected.connect(self.from_canvas)
        self.update_selection()

    def spin(self, label, minimum, maximum, value):
        box = QSpinBox()
        box.setAccessibleName(label)
        box.setRange(minimum, maximum)
        box.setValue(value)
        return box

    def from_canvas(self, frame):
        for control, value in zip((self.x, self.y, self.width_box, self.height_box), frame.getRect()):
            control.setValue(value)

    def update_selection(self):
        frame = QRect(self.x.value(), self.y.value(), self.width_box.value(), self.height_box.value())
        columns, rows = self.columns.value(), self.rows.value()
        count = columns * rows
        valid = (frame.x() + frame.width() * columns <= self.sheet.width
                 and frame.y() + frame.height() * rows <= self.sheet.height
                 and limit_frames(frame.width(), frame.height(), count))
        self.canvas.frame, self.canvas.columns, self.canvas.rows = frame, columns, rows
        self.canvas.update()
        self.use.setEnabled(valid)
        self.message.setText(f"{count} 帧 · 每帧 {frame.width()} × {frame.height()} px · 从左向右、逐行播放"
                             if valid else "请缩小帧网格或单帧尺寸，使选区在原图及规格范围内。")

    def save(self):
        try:
            self.asset = crop_sheet(self.sheet, self.canvas.frame, self.columns.value(), self.rows.value(),
                                    self.rate.value(), self.role)
        except ValueError as error:
            self.message.setText(str(error))
            return
        self.accept()
