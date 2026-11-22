"""Local skins, package validation, and the companion appearance dialog."""
from dataclasses import dataclass, replace
import json
from math import pi, sin
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from PyQt6.QtCore import QElapsedTimer, QRect, QRectF, QSettings, QSignalBlocker, QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QImageReader, QLinearGradient, QMovie, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QColorDialog, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)

from resident_agent import PROFILE
from skin_assets import ASSET_FILES, FILE_LIMIT, MIB, ROLES, STATES, CropDialog, asset_bytes, check_image, require_roles


ART = Path(__file__).resolve().parent.parent / "assets" / "companion"
APP_ICON = ART.parent / "app-icon.ico"
PEEK_BOUNDS = {False: QRect(185, 77, 552, 744), True: QRect(995, 207, 668, 543)}
BLINK_BOUNDS = {
    False: (QRect(276, 369, 119, 106), QRect(426, 428, 128, 101)),
    True: (QRect(1167, 552, 143, 112), QRect(1342, 551, 139, 108)),
}


@dataclass(frozen=True)
class Palette:
    background: str
    surface: str
    border: str
    text: str
    highlight: str


PALETTES = {
    "lilac": Palette("#251d3b", "#342851", "#79639f", "#f2edff", "#cbb6fa"),
    "mint": Palette("#152e2c", "#23413d", "#4e8a80", "#edfdfa", "#a5ead5"),
    "blue": Palette("#1a2841", "#293b5b", "#5e7cae", "#eff5ff", "#abcafa"),
    "peach": Palette("#39251f", "#55362b", "#aa7868", "#fff2eb", "#ffc5b1"),
    "gray": Palette("#272831", "#383a46", "#777a93", "#f3f4fb", "#cbd0e0"),
}
PALETTE_LABELS = {"lilac": "薰衣草", "mint": "薄荷绿", "blue": "雾蓝", "peach": "暖桃", "gray": "石墨灰"}


def palette_for(token):
    if not isinstance(token, str):
        raise ValueError("请填写有效的预设色或 #RRGGBB 颜色。")
    if token in PALETTES:
        return PALETTES[token]
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", token):
        raise ValueError("请填写有效的预设色或 #RRGGBB 颜色。")
    base = QColor(token)
    channels = [base.redF(), base.greenF(), base.blueF()]
    linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in channels]
    brightness = sum(value * weight for value, weight in zip(linear, (.2126, .7152, .0722)))
    light = brightness >= .18
    target = 0 if light else 255

    def blend(amount):
        return QColor(*(round(value * 255 * (1 - amount) + target * amount) for value in channels)).name()

    return Palette(base.name(), blend(.09), blend(.35), "#000000" if light else "#ffffff",
                   blend(.82))


def palette_key(palette):
    return next((key for key, color in PALETTES.items() if color == palette), palette.background)


def open_movie(path, parent, size):
    movie = QMovie(str(path), parent=parent)
    native = QImageReader(str(path)).size()
    if native.isValid():
        movie.setScaledSize(native.scaled(size, Qt.AspectRatioMode.KeepAspectRatio))
    # A downloaded GIF may declare a finite loop count.
    if movie.frameCount() > 1:
        movie.finished.connect(movie.start)
    return movie


@dataclass(frozen=True)
class Skin:
    key: str
    name: str
    path: Path
    palette: Palette

    def movie(self, state, parent, size):
        asset = self.path / f"{state}.gif"
        if not asset.is_file():
            asset = self.path / "idle.gif"
        return open_movie(asset, parent, size)

    def peek(self, bottom):
        if self.path == ART:
            pixmap = QPixmap(str(ART / "peek-poses.png"))
            return pixmap.copy(PEEK_BOUNDS[bottom])
        return QPixmap(str(self.path / ("bottom.png" if bottom else "side.png")))


def blink_cels(still, bottom):
    sheet = QPixmap(str(ART / "peek-blink.png"))
    if still.isNull() or not sheet.rect().contains(PEEK_BOUNDS[bottom]):
        return (still,)
    patches = []
    for bounds in BLINK_BOUNDS[bottom]:
        patch = sheet.copy(bounds)
        painter = QPainter(patch)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        for x, y in ((patch.width(), 0), (0, patch.height())):
            fade = QLinearGradient(0, 0, x, y)
            fade.setColorAt(0, Qt.GlobalColor.transparent)
            fade.setColorAt(.09, Qt.GlobalColor.black)
            fade.setColorAt(.91, Qt.GlobalColor.black)
            fade.setColorAt(1, Qt.GlobalColor.transparent)
            painter.fillRect(patch.rect(), fade)
        painter.end()
        patches.append((bounds.topLeft() - PEEK_BOUNDS[bottom].topLeft(), patch))
    cels = [still]
    for opacity in (.64, 1):
        cel = still.copy()
        painter = QPainter(cel)
        painter.setOpacity(opacity)
        for point, patch in patches:
            painter.drawPixmap(point, patch)
        painter.end()
        cels.append(cel)
    return tuple(cels)


def blink_step(elapsed, bottom):
    # Let the two poses blink at different times and vary the pause between blinks.
    phase = (elapsed - (1.75 if bottom else 1.10)) % 12.4
    for beat in (0, 3.6, 8.0):
        age = phase - beat
        if 0 <= age < .18:
            return 2 if .035 <= age < .14 else 1
    return 0


class PeekFrames:
    def __init__(self, skin, bottom, owner):
        self.still = skin.peek(bottom)
        self.bottom = bottom
        self.cels = blink_cels(self.still, bottom) if skin.path == ART else (self.still,)
        path = skin.path / ("bottom.gif" if bottom else "side.gif")
        self.movie = open_movie(path, owner, QSize(164, 168)) if path.is_file() else None

    def start(self):
        if self.movie is not None:
            self.movie.jumpToFrame(0)
            self.movie.start()

    def stop(self):
        if self.movie is not None:
            self.movie.stop()

    def dispose(self):
        if self.movie is not None:
            self.movie.stop()
            self.movie.deleteLater()

    def current(self, elapsed=0):
        if self.movie is not None:
            frame = self.movie.currentPixmap()
            if not frame.isNull():
                return frame
        if len(self.cels) > 1:
            return self.cels[blink_step(elapsed, self.bottom)]
        return self.still


def paint_peek(painter, width, height, pose, bottom, elapsed, mirror=False):
    if pose.isNull():
        return
    hidden = (1 - min(1, elapsed / .54)) ** 3
    idle = max(0, elapsed - .54)
    breathe = .021 * sin(idle * 2.7)
    glance = sin(pi * min(1, max(0, (idle % 5.6 - 3) / 1.35))) ** 2
    size = pose.size().scaled(QSize(width - (2 if bottom else 3), height - 1),
                              Qt.AspectRatioMode.KeepAspectRatio)
    if bottom:
        painter.translate(0, height * (1 + hidden))
        painter.scale(1, 1 + breathe + .055 * glance)
        painter.translate(0, -height)
        x, y = (width - size.width()) / 2, height - size.height()
    else:
        if mirror:
            painter.translate(width, 0)
            painter.scale(-1, 1)
        painter.translate(-width * hidden, 0)
        painter.scale(1 + breathe, 1)
        pivot = height * .65
        painter.translate(3, pivot)
        painter.rotate(4 * glance)
        painter.translate(-3, -pivot)
        x, y = 0, (height - size.height()) / 2
    painter.drawPixmap(QRectF(x, y, size.width(), size.height()), pose, QRectF(pose.rect()))


BUILT_INS = (
    Skin("default", "窗边小龙 · 薰衣草", ART, PALETTES["lilac"]),
    Skin("mint", "窗边小龙 · 薄荷绿", ART, PALETTES["mint"]),
    Skin("blue", "窗边小龙 · 雾蓝", ART, PALETTES["blue"]),
)


def read_manifest(raw):
    try:
        data = json.loads(raw)
        name = data["name"].strip()
        palette = palette_for(data.get("palette", "lilac"))
        if type(data["version"]) is not int or data["version"] != 1 or not 1 <= len(name) <= 32:
            raise ValueError
        if any(not ch.isprintable() for ch in name):
            raise ValueError
    except (ValueError, UnicodeError, TypeError, AttributeError, KeyError):
        raise ValueError("skin.json 需要版本 1、有效的皮肤名称和配色。") from None
    return name, palette


class SkinLibrary:
    def __init__(self, root=None):
        self.root = root or PROFILE / "appearance"
        self.installed = self.root / "skins"
        self.settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)

    def all(self, *, saved_palettes=True):
        skins = list(BUILT_INS)
        for path in sorted(self.installed.glob("skin-*")):
            try:
                if not require_roles({item.name for item in path.iterdir() if item.is_file()}):
                    continue
                manifest = path / "skin.json"
                if manifest.stat().st_size > 4096:
                    continue
                name, palette = read_manifest(manifest.read_bytes())
            except (OSError, ValueError):
                continue
            skins.append(Skin(path.name, name, path, palette))
        return [self.with_saved_palette(skin) for skin in skins] if saved_palettes else skins

    def with_saved_palette(self, skin):
        choice = self.settings.value(f"palettes/{skin.key}", "", type=str)
        try:
            return replace(skin, palette=palette_for(choice)) if choice else skin
        except ValueError:
            return skin

    def current(self):
        self.settings.sync()
        key = self.settings.value("skin", BUILT_INS[0].key, type=str)
        return self.resolve(key)

    def resolve(self, key, *, saved_palette=True):
        return next((skin for skin in self.all(saved_palettes=saved_palette) if skin.key == key), BUILT_INS[0])

    def select(self, skin):
        self.settings.setValue(f"palettes/{skin.key}", palette_key(skin.palette))
        self.settings.setValue("skin", skin.key)
        self.settings.sync()
        if self.settings.status() != QSettings.Status.NoError:
            raise ValueError("无法保存外观设置，请检查本机目录的写入权限。")

    def presets(self):
        self.settings.sync()
        skins = {skin.key: skin for skin in self.all()}
        presets = [(skin.name, skin) for skin in BUILT_INS]
        self.settings.beginGroup("presets")
        try:
            for key in self.settings.childKeys():
                try:
                    name, skin_key, token = json.loads(self.settings.value(key, "", type=str))
                    if not isinstance(name, str) or not 1 <= len(name) <= 32 or not name.isprintable():
                        continue
                    presets.append((name, replace(skins[skin_key], palette=palette_for(token))))
                except (KeyError, TypeError, ValueError):
                    continue
        finally:
            self.settings.endGroup()
        return presets[:len(BUILT_INS)] + sorted(presets[len(BUILT_INS):], key=lambda row: row[0].casefold())

    def save_preset(self, name, skin):
        name = name.strip()
        if not 1 <= len(name) <= 32 or not name.isprintable():
            raise ValueError("方案名称需要 1–32 个可见字符。")
        if any(existing.casefold() == name.casefold() for existing, _ in self.presets()):
            raise ValueError("已有同名方案，请换一个名称。")
        encoded = json.dumps([name, skin.key, palette_key(skin.palette)], ensure_ascii=False)
        self.settings.setValue(f"presets/{uuid4().hex}", encoded)
        self.settings.sync()
        if self.settings.status() != QSettings.Status.NoError:
            raise ValueError("无法保存本地方案，请检查目录的写入权限。")

    def import_zip(self, source):
        allowed = set(ASSET_FILES) | {"skin.json"}
        try:
            if source.stat().st_size > 32 * MIB:
                raise ValueError("皮肤包大小需在 32 MB 以内。")
            with ZipFile(source) as package:
                members = package.infolist()
                names = {entry.filename for entry in members}
                if "skin.json" not in names or not require_roles(names) or not names <= allowed or len(names) != len(members):
                    raise ValueError("ZIP 根目录须包含 skin.json、idle.gif 和各一张 side、bottom 素材（GIF 或 PNG）。")
                if sum(entry.file_size for entry in members) > 32 * MIB:
                    raise ValueError("解压后的素材总大小需在 32 MB 以内。")
                for entry in members:
                    limit = 4096 if entry.filename == "skin.json" else FILE_LIMIT
                    if entry.file_size > limit:
                        raise ValueError(f"{entry.filename} 超出单文件大小上限。")
                raw = {name: package.read(name) for name in names}
        except (BadZipFile, OSError, RuntimeError, NotImplementedError):
            raise ValueError("无法读取或保存皮肤包。请确认 ZIP 可打开，且未加密。") from None
        return self.install(raw)

    def create(self, name, sources):
        if not require_roles(sources) or not sources.keys() <= ASSET_FILES.keys():
            raise ValueError("请选择站姿、侧边探头和底部探头三张素材。")
        raw = {"skin.json": json.dumps({"version": 1, "name": name, "palette": "lilac"},
                                        ensure_ascii=False).encode("utf-8")}
        raw.update(sources)
        return self.install(raw)

    def install(self, raw):
        name, palette = read_manifest(raw["skin.json"])
        for filename in raw.keys() - {"skin.json"}:
            if len(raw[filename]) > FILE_LIMIT:
                raise ValueError(f"{filename} 超出 8 MB。")
            check_image(filename, raw[filename])
        key = f"skin-{uuid4().hex}"
        destination = self.installed / key
        try:
            self.installed.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(dir=self.installed, prefix=".import-") as temporary:
                for filename, content in raw.items():
                    (Path(temporary) / filename).write_bytes(content)
                Path(temporary).rename(destination)
        except OSError:
            raise ValueError("无法保存皮肤素材，请检查本机目录的写入权限。") from None
        return Skin(key, name, destination, palette)


class PreviewPortrait(QWidget):
    def __init__(self, bottom):
        super().__init__()
        self.bottom = bottom
        self.frames = None
        self.clock = QElapsedTimer()
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self.update)
        self.setFixedSize(88, 72)

    def set_skin(self, skin):
        self.stop()
        self.frames = PeekFrames(skin, self.bottom, self)
        self.frames.start()
        self.clock.start()
        self.timer.start()
        self.update()

    def stop(self):
        self.timer.stop()
        if self.frames is not None:
            self.frames.dispose()
            self.frames = None

    def paintEvent(self, event):
        if self.frames is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        elapsed = self.clock.elapsed() / 1000
        paint_peek(painter, self.width(), self.height(), self.frames.current(elapsed), self.bottom, elapsed % 5.6)


FORMAT_HINT = ("单帧边长 1–1024 px，单个素材 ≤ 8 MB，最多 180 帧，所有帧合计 ≤ 2800 万像素。"
               "\n透明 PNG 精灵图长宽均 ≤ 4096 px。裁剪可设置首帧位置、单帧宽高、行列和 1–30 FPS。"
               "\n多帧裁剪会转为 GIF，半透明像素会变为透明或不透明。")


class NewSkinDialog(QDialog):
    def __init__(self, parent, library):
        super().__init__(parent)
        self.library = library
        self.sources = {}
        self.skin = None
        self.setWindowTitle("制作皮肤")
        self.setMinimumWidth(450)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 15, 18, 15)
        form = QFormLayout()
        layout.addLayout(form)
        self.name = QLineEdit()
        self.name.setMaxLength(32)
        self.name.setToolTip("名称为 1–32 个可见字符，会显示在角色素材列表里。")
        form.addRow("皮肤名称", self.name)
        for role, label, hint in (
            ("idle", "站姿动画", "建议单帧 192 × 192 px，人物居中。站姿 GIF 可直接导入。"),
            ("side", "侧边探头", "建议单帧 144 × 192 px，扶边的手贴近画面左侧。右侧会自动镜像。"),
            ("bottom", "底部探头", "建议单帧 192 × 156 px，双手贴近画面底边。"),
        ):
            row = QWidget()
            controls = QHBoxLayout(row)
            controls.setContentsMargins(0, 0, 0, 0)
            note = QLabel("未选择")
            note.setMinimumWidth(90)
            controls.addWidget(note, 1)
            button = QPushButton("导入 GIF…")
            button.clicked.connect(lambda checked, key=role, current=note: self.choose(key, current, crop=False))
            controls.addWidget(button)
            crop = QPushButton("裁剪 PNG…")
            crop.clicked.connect(lambda checked, key=role, current=note: self.choose(key, current, crop=True))
            controls.addWidget(crop)
            heading = QLabel(label)
            for widget in (heading, row, note, button, crop):
                widget.setToolTip(hint + "\n\n" + FORMAT_HINT)
            form.addRow(heading, row)
        self.message = QLabel("悬停任一素材栏可查看规格。PNG 可框一帧，也可按网格裁成循环动画。")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        actions = QHBoxLayout()
        actions.addStretch()
        layout.addLayout(actions)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        create = QPushButton("制作")
        create.setObjectName("apply")
        create.clicked.connect(self.save)
        actions.addWidget(create)

    def choose(self, role, note, *, crop):
        file_filter = "透明 PNG (*.png)" if crop else "GIF (*.gif)"
        selected, _ = QFileDialog.getOpenFileName(self, "选择皮肤素材", "", file_filter)
        if not selected:
            return
        path = Path(selected)
        try:
            raw = asset_bytes(path)
            filename = f"{role}.gif"
            if crop:
                editor = CropDialog(self, raw, role)
                accepted = editor.exec() == QDialog.DialogCode.Accepted
                asset = editor.asset
                editor.deleteLater()
                if not accepted:
                    return
                filename, raw = asset
            check_image(filename, raw)
        except ValueError as error:
            self.show_error(error)
            return
        self.sources[role] = (filename, raw)
        note.setText(note.fontMetrics().elidedText(path.name, Qt.TextElideMode.ElideMiddle, 118))
        self.message.setStyleSheet("")
        self.message.setText("素材已就绪。" if self.sources.keys() == set(ROLES) else "继续选择其余素材。")

    def save(self):
        try:
            self.skin = self.library.create(self.name.text(), dict(self.sources.values()))
        except ValueError as error:
            self.show_error(error)
            return
        self.accept()

    def show_error(self, error):
        self.message.setText(str(error))
        self.message.setStyleSheet("color:#ffbcaf;")


class AppearanceEditor(QWidget):
    def __init__(self, library, current, apply_skin):
        super().__init__()
        self.library = library
        self.selected = current
        self.apply_skin = apply_skin
        self.preview_movie = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        panes = QHBoxLayout()
        panes.setSpacing(14)
        layout.addLayout(panes, 1)
        library_panel = QVBoxLayout()
        panes.addLayout(library_panel, 2)
        library_panel.addWidget(QLabel("本地外观方案"))
        self.presets = QListWidget()
        self.presets.setAccessibleName("本地外观方案")
        self.presets.setMinimumWidth(163)
        library_panel.addWidget(self.presets, 1)
        save_preset = QPushButton("保存为方案…")
        save_preset.clicked.connect(self.save_preset)
        library_panel.addWidget(save_preset)
        preview = QFrame()
        preview.setObjectName("skinPreview")
        preview.setMinimumWidth(290)
        panes.addWidget(preview, 3)
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(14, 13, 14, 12)
        preview_layout.setSpacing(4)
        self.sample = QLabel("在核对下一步操作。")
        self.sample.setObjectName("skinSample")
        preview_layout.addWidget(self.sample, alignment=Qt.AlignmentFlag.AlignCenter)
        self.sprite = QLabel()
        self.sprite.setFixedSize(105, 105)
        self.sprite.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_layout.addWidget(self.sprite, alignment=Qt.AlignmentFlag.AlignHCenter)
        positions = QHBoxLayout()
        preview_layout.addLayout(positions)
        self.side = PreviewPortrait(False)
        self.bottom = PreviewPortrait(True)
        for portrait, label in ((self.side, "侧边探头"), (self.bottom, "底部探头")):
            slot = QVBoxLayout()
            slot.addWidget(portrait, alignment=Qt.AlignmentFlag.AlignHCenter)
            caption = QLabel(label)
            caption.setObjectName("previewCaption")
            slot.addWidget(caption, alignment=Qt.AlignmentFlag.AlignHCenter)
            positions.addLayout(slot)
        self.preview = preview
        assets = QHBoxLayout()
        layout.addLayout(assets)
        assets.addWidget(QLabel("角色素材"))
        self.materials = QComboBox()
        self.materials.setAccessibleName("角色素材")
        assets.addWidget(self.materials, 1)
        colors = QHBoxLayout()
        layout.addLayout(colors)
        colors.addWidget(QLabel("气泡配色"))
        self.colors = QComboBox()
        self.colors.setAccessibleName("气泡配色")
        for key, label in PALETTE_LABELS.items():
            self.colors.addItem(label, key)
        colors.addWidget(self.colors)
        rgb = QPushButton("自定义 RGB…")
        rgb.clicked.connect(self.choose_rgb)
        colors.addWidget(rgb)
        colors.addStretch()
        self.message = QLabel()
        self.message.setObjectName("notice")
        self.message.setWordWrap(True)
        self.message.hide()
        layout.addWidget(self.message)
        buttons = QHBoxLayout()
        layout.addLayout(buttons)
        create = QPushButton("制作皮肤…")
        create.clicked.connect(self.create_skin)
        buttons.addWidget(create)
        import_button = QPushButton("导入皮肤…")
        import_button.clicked.connect(self.import_skin)
        buttons.addWidget(import_button)
        reset = QPushButton("恢复默认")
        reset.clicked.connect(self.reset_default)
        buttons.addWidget(reset)
        buttons.addStretch()
        apply = QPushButton("应用到桌宠")
        apply.setObjectName("primary")
        apply.clicked.connect(self.save_selection)
        buttons.addWidget(apply)
        self.presets.currentItemChanged.connect(self.choose_preset)
        self.presets.itemClicked.connect(self.choose_preset)
        self.materials.currentIndexChanged.connect(self.choose_material)
        self.colors.currentIndexChanged.connect(self.choose_palette)
        self.load(current)

    def load(self, skin):
        with QSignalBlocker(self.materials):
            self.materials.clear()
            for material in self.library.all():
                if material.path != ART or material.key == BUILT_INS[0].key:
                    self.add_material(material)
        self.set_preview(skin)
        self.reload_presets()

    def showEvent(self, event):
        super().showEvent(event)
        self.start_preview()

    def hideEvent(self, event):
        self.stop_preview()
        super().hideEvent(event)

    def reload_presets(self, select_name=None):
        with QSignalBlocker(self.presets):
            self.presets.clear()
            for name, skin in self.library.presets():
                item = QListWidgetItem(name)
                item.setData(Qt.ItemDataRole.UserRole, skin)
                item.setToolTip(f"{skin.name} · {PALETTE_LABELS.get(palette_key(skin.palette), skin.palette.background.upper())}")
                self.presets.addItem(item)
                if name == select_name or (select_name is None and skin == self.selected and self.presets.currentRow() < 0):
                    self.presets.setCurrentItem(item)
            if self.presets.currentRow() < 0:
                self.presets.setCurrentRow(0)
                self.presets.clearSelection()

    def add_material(self, skin):
        self.materials.addItem("窗边小龙（内置）" if skin.path == ART else skin.name, skin)
        return self.materials.count() - 1

    def choose_preset(self, item, previous=None):
        if item is not None:
            skin = item.data(Qt.ItemDataRole.UserRole)
            if skin != self.selected:
                self.set_preview(skin)

    def choose_material(self):
        self.presets.clearSelection()
        self.set_preview(self.materials.currentData())

    def set_preview(self, skin):
        self.selected = skin
        with QSignalBlocker(self.materials):
            for row in range(self.materials.count()):
                if self.materials.itemData(row).path == skin.path:
                    self.materials.setCurrentIndex(row)
                    break
        self.set_palette(skin.palette)
        self.stop_preview()
        if self.isVisible():
            self.start_preview()
        self.message.hide()

    def start_preview(self):
        if self.preview_movie is not None:
            return
        self.preview_movie = self.selected.movie("idle", self, self.sprite.size())
        self.sprite.setMovie(self.preview_movie)
        self.preview_movie.start()
        for portrait in (self.side, self.bottom):
            portrait.set_skin(self.selected)

    def stop_preview(self):
        self.side.stop()
        self.bottom.stop()
        if self.preview_movie is not None:
            self.sprite.clear()
            self.preview_movie.stop()
            self.preview_movie.deleteLater()
            self.preview_movie = None

    def choose_palette(self):
        self.presets.clearSelection()
        self.set_palette(palette_for(self.colors.currentData()))

    def choose_rgb(self):
        result = QColorDialog.getColor(QColor(self.selected.palette.background), self, "自定义气泡 RGB")
        if result.isValid():
            self.presets.clearSelection()
            self.set_palette(palette_for(result.name()))

    def set_palette(self, palette):
        self.selected = replace(self.selected, palette=palette)
        token = palette_key(palette)
        with QSignalBlocker(self.colors):
            if self.colors.count() > len(PALETTES):
                self.colors.removeItem(len(PALETTES))
            if token not in PALETTES:
                self.colors.addItem(f"自定义 · {token.upper()}", token)
            self.colors.setCurrentIndex(self.colors.findData(token))
        self.preview.setStyleSheet(
            f"QFrame#skinPreview{{background:{palette.background};border:1px solid {palette.border};"
            f"border-radius:12px;}} QLabel#skinSample{{background:{palette.background};color:{palette.text};"
            f"border:1px solid {palette.border};"
            f"padding:6px 10px;border-radius:9px;}} QLabel#previewCaption{{color:{palette.text};}}"
        )

    def reset_default(self):
        self.presets.setCurrentRow(0)
        self.set_preview(BUILT_INS[0])

    def save_preset(self):
        name, accepted = QInputDialog.getText(self, "保存外观方案", "给当前角色和配色取个名字")
        if not accepted:
            return
        try:
            self.library.save_preset(name, self.selected)
        except ValueError as error:
            self.message.setText(str(error))
            self.message.show()
            return
        self.reload_presets(name.strip())
        self.message.hide()

    def import_skin(self):
        filename, _ = QFileDialog.getOpenFileName(self, "导入伴学皮肤", "", "皮肤包 (*.zip)")
        if not filename:
            return
        try:
            skin = self.library.import_zip(Path(filename))
        except ValueError as error:
            self.message.setText(str(error))
            self.message.show()
            return
        self.materials.setCurrentIndex(self.add_material(skin))

    def create_skin(self):
        form = NewSkinDialog(self, self.library)
        if form.exec() == QDialog.DialogCode.Accepted:
            self.materials.setCurrentIndex(self.add_material(form.skin))
        form.deleteLater()

    def save_selection(self):
        try:
            self.apply_skin(self.selected)
        except ValueError as error:
            self.message.setText(str(error))
            self.message.show()
            return
        self.message.setText("已应用到选中的桌宠。")
        self.message.show()


if __name__ == "__main__":
    from desktop import main

    raise SystemExit(main(initial_page="appearance"))
