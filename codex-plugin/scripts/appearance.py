"""Local skins, package validation, and the companion appearance dialog."""
from dataclasses import dataclass, replace
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from PIL import Image
from PyQt6.QtCore import QRectF, QSettings, QSize, Qt
from PyQt6.QtGui import QCursor, QGuiApplication, QImageReader, QMovie, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)


ART = Path(__file__).resolve().parent.parent / "assets" / "companion"
STATES = ("idle", "reading", "thinking", "ready")
ASSET_FILES = {f"{state}.gif": "GIF" for state in STATES}
ASSET_FILES.update({"side.png": "PNG", "bottom.png": "PNG"})
REQUIRED_ASSETS = {"idle.gif", "side.png", "bottom.png"}
MIB = 1024 * 1024


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
}
PALETTE_LABELS = {"lilac": "薰衣草", "mint": "薄荷绿", "blue": "雾蓝"}


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
        movie = QMovie(str(asset), parent=parent)
        native = QImageReader(str(asset)).size()
        if native.isValid():
            movie.setScaledSize(native.scaled(size, Qt.AspectRatioMode.KeepAspectRatio))
        return movie

    def peek(self, bottom):
        if self.path == ART:
            pixmap = QPixmap(str(ART / "peek-poses.png"))
            return pixmap.copy(995, 207, 668, 543) if bottom else pixmap.copy(185, 77, 552, 744)
        return QPixmap(str(self.path / ("bottom.png" if bottom else "side.png")))


BUILT_INS = (
    Skin("default", "窗边小龙 · 薰衣草", ART, PALETTES["lilac"]),
    Skin("mint", "窗边小龙 · 薄荷绿", ART, PALETTES["mint"]),
    Skin("blue", "窗边小龙 · 雾蓝", ART, PALETTES["blue"]),
)


def read_manifest(raw):
    try:
        data = json.loads(raw)
        name = data["name"].strip()
        palette = PALETTES[data.get("palette", "lilac")]
        if type(data["version"]) is not int or data["version"] != 1 or not 1 <= len(name) <= 32:
            raise ValueError
        if any(not ch.isprintable() for ch in name):
            raise ValueError
    except (ValueError, UnicodeError, TypeError, AttributeError, KeyError):
        raise ValueError("skin.json 需要版本 1、有效的皮肤名称和配色。") from None
    return name, palette


def check_image(filename, raw):
    try:
        with Image.open(BytesIO(raw)) as image:
            if image.format != ASSET_FILES[filename] or max(image.size) > 1024:
                raise ValueError
            if filename.endswith(".png"):
                if image.is_animated or ("A" not in image.getbands() and "transparency" not in image.info):
                    raise ValueError
            index = 0
            while True:
                if index >= 180 or image.width * image.height * (index + 1) > 28_000_000:
                    raise ValueError
                image.load()
                index += 1
                try:
                    image.seek(index)
                except EOFError:
                    break
    except (OSError, ValueError, Image.DecompressionBombError, SyntaxError):
        raise ValueError(f"{filename} 无法使用。请检查图片格式、透明背景、尺寸和帧数。") from None


class SkinLibrary:
    def __init__(self, root=None):
        self.root = root or Path.home() / ".codex" / "screen-mentor" / "appearance"
        self.installed = self.root / "skins"
        self.settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)

    def all(self):
        skins = list(BUILT_INS)
        for path in sorted(self.installed.glob("skin-*")):
            try:
                if not all((path / name).is_file() for name in REQUIRED_ASSETS):
                    continue
                manifest = path / "skin.json"
                if manifest.stat().st_size > 4096:
                    continue
                name, palette = read_manifest(manifest.read_bytes())
            except (OSError, ValueError):
                continue
            skins.append(Skin(path.name, name, path, palette))
        return [self.with_saved_palette(skin) for skin in skins]

    def with_saved_palette(self, skin):
        choice = self.settings.value(f"palettes/{skin.key}", "", type=str)
        return replace(skin, palette=PALETTES.get(choice, skin.palette))

    def current(self):
        self.settings.sync()
        key = self.settings.value("skin", BUILT_INS[0].key, type=str)
        return next((skin for skin in self.all() if skin.key == key), BUILT_INS[0])

    def select(self, skin):
        palette_key = next(key for key, palette in PALETTES.items() if palette == skin.palette)
        self.settings.setValue(f"palettes/{skin.key}", palette_key)
        self.settings.setValue("skin", skin.key)
        self.settings.sync()
        if self.settings.status() != QSettings.Status.NoError:
            raise ValueError("无法保存外观设置，请检查本机目录的写入权限。")

    def import_zip(self, source):
        required = REQUIRED_ASSETS | {"skin.json"}
        allowed = set(ASSET_FILES) | {"skin.json"}
        try:
            if source.stat().st_size > 32 * MIB:
                raise ValueError("皮肤包大小需在 32 MB 以内。")
            with ZipFile(source) as package:
                members = package.infolist()
                names = {entry.filename for entry in members}
                if not required <= names <= allowed or len(names) != len(members):
                    raise ValueError("请把 skin.json、idle.gif、side.png 和 bottom.png 放在 ZIP 根目录。")
                if sum(entry.file_size for entry in members) > 32 * MIB:
                    raise ValueError("解压后的素材总大小需在 32 MB 以内。")
                for entry in members:
                    limit = 4096 if entry.filename == "skin.json" else 8 * MIB
                    if entry.file_size > limit:
                        raise ValueError(f"{entry.filename} 超出单文件大小上限。")
                raw = {name: package.read(name) for name in names}
        except (BadZipFile, OSError, RuntimeError, NotImplementedError):
            raise ValueError("无法读取或保存皮肤包。请确认 ZIP 可打开，且未加密。") from None
        return self.install(raw)

    def create(self, name, sources):
        if not REQUIRED_ASSETS <= sources.keys() <= ASSET_FILES.keys():
            raise ValueError("请选择站姿、侧边探头和底部探头三张素材。")
        raw = {"skin.json": json.dumps({"version": 1, "name": name, "palette": "lilac"},
                                        ensure_ascii=False).encode("utf-8")}
        try:
            for filename, path in sources.items():
                with path.open("rb") as file:
                    content = file.read(8 * MIB + 1)
                if len(content) > 8 * MIB:
                    raise ValueError(f"{path.name} 超出单文件大小上限。")
                raw[filename] = content
        except OSError:
            raise ValueError("无法读取所选素材。请确认文件仍在原来的位置。") from None
        return self.install(raw)

    def install(self, raw):
        name, palette = read_manifest(raw["skin.json"])
        for filename in raw.keys() - {"skin.json"}:
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
        self.portrait = QPixmap()
        self.setFixedSize(88, 72)

    def paintEvent(self, event):
        if self.portrait.isNull():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        size = self.portrait.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        x = (self.width() - size.width()) / 2 if self.bottom else 0
        y = self.height() - size.height() if self.bottom else (self.height() - size.height()) / 2
        painter.drawPixmap(QRectF(x, y, size.width(), size.height()), self.portrait, QRectF(self.portrait.rect()))


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
        form.addRow("皮肤名称", self.name)
        for filename, label, file_filter in (
            ("idle.gif", "站姿动画", "GIF (*.gif)"),
            ("side.png", "侧边探头", "透明 PNG (*.png)"),
            ("bottom.png", "底部探头", "透明 PNG (*.png)"),
        ):
            button = QPushButton("选择素材…")
            button.clicked.connect(
                lambda checked, key=filename, selected=button, mode=file_filter: self.choose(key, selected, mode)
            )
            form.addRow(label, button)
        self.message = QLabel("侧边素材请把扶边的手画在左侧，右侧会自动镜像。")
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

    def choose(self, filename, button, file_filter):
        selected, _ = QFileDialog.getOpenFileName(self, "选择皮肤素材", "", file_filter)
        if selected:
            path = Path(selected)
            self.sources[filename] = path
            button.setText(button.fontMetrics().elidedText(path.name, Qt.TextElideMode.ElideMiddle, 225))
            button.setToolTip(path.name)

    def save(self):
        try:
            self.skin = self.library.create(self.name.text(), self.sources)
        except ValueError as error:
            self.message.setText(str(error))
            self.message.setObjectName("error")
            self.message.setStyleSheet("color:#ffbcaf;")
            return
        self.accept()


class AppearanceDialog(QDialog):
    def __init__(self, parent, library, current):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.WindowStaysOnTopHint)
        self.library = library
        self.selected = current
        self.preview_movie = None
        self.setWindowTitle("伴学外观")
        self.setMinimumWidth(525)
        self.setStyleSheet(
            "QDialog{background:#191724;} QWidget{color:#f1ecff;font:13px 'Microsoft YaHei UI';}"
            "QLabel#title{font-size:21px;font-weight:600;} QLabel#note{color:#bdb4d2;}"
            "QLabel#error{color:#ffbcaf;} QListWidget{background:#211e30;border:1px solid #453957;"
            "border-radius:10px;padding:5px;outline:0;}"
            "QListWidget::item{padding:9px;border-radius:6px;}"
            "QListWidget::item:selected{background:#433453;color:#fff;}"
            "QPushButton{background:#2b243b;border:1px solid #4c3d64;border-radius:8px;padding:7px 13px;}"
            "QPushButton:hover,QPushButton:focus{border-color:#b49bd8;}"
            "QPushButton#apply{background:#c1aae9;color:#241834;font-weight:600;}"
            "QComboBox{background:#2b243b;border:1px solid #4c3d64;border-radius:6px;padding:4px 9px;}"
            "QComboBox QAbstractItemView{background:#2b243b;selection-background-color:#604978;}"
            "QLineEdit{background:#211e30;border:1px solid #453957;border-radius:6px;padding:5px;}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 19, 22, 19)
        layout.setSpacing(12)
        title = QLabel("伴学外观")
        title.setObjectName("title")
        layout.addWidget(title)
        panes = QHBoxLayout()
        panes.setSpacing(14)
        layout.addLayout(panes)
        self.skins = QListWidget()
        self.skins.setAccessibleName("皮肤列表")
        self.skins.setMinimumWidth(158)
        panes.addWidget(self.skins, 2)
        preview = QFrame()
        preview.setObjectName("skinPreview")
        preview.setMinimumWidth(290)
        panes.addWidget(preview, 3)
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(14, 13, 14, 12)
        preview_layout.setSpacing(4)
        self.sample = QLabel("在核对下一步操作。")
        self.sample.setObjectName("skinSample")
        preview_layout.addWidget(self.sample, alignment=Qt.AlignmentFlag.AlignHCenter)
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
            caption.setObjectName("note")
            slot.addWidget(caption, alignment=Qt.AlignmentFlag.AlignHCenter)
            positions.addLayout(slot)
        self.preview = preview
        colors = QHBoxLayout()
        layout.addLayout(colors)
        colors.addWidget(QLabel("气泡配色"))
        self.colors = QComboBox()
        self.colors.setAccessibleName("气泡配色")
        for key, label in PALETTE_LABELS.items():
            self.colors.addItem(label, key)
        self.colors.currentIndexChanged.connect(self.preview_palette)
        colors.addWidget(self.colors)
        colors.addStretch()
        self.message = QLabel()
        self.message.setObjectName("error")
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
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        apply = QPushButton("应用")
        apply.setObjectName("apply")
        apply.setDefault(True)
        apply.clicked.connect(self.save_selection)
        buttons.addWidget(apply)
        self.finished.connect(self.stop_preview)
        self.skins.currentItemChanged.connect(self.update_preview)
        for skin in library.all():
            self.add_skin(skin)
            if skin.key == current.key:
                self.skins.setCurrentRow(self.skins.count() - 1)

    def add_skin(self, skin):
        item = QListWidgetItem(skin.name)
        item.setData(Qt.ItemDataRole.UserRole, skin)
        self.skins.addItem(item)
        return item

    def update_preview(self, item, previous):
        if item is None:
            return
        self.selected = item.data(Qt.ItemDataRole.UserRole)
        key = next(key for key, palette in PALETTES.items() if palette == self.selected.palette)
        self.colors.setCurrentIndex(self.colors.findData(key))
        self.preview_palette()
        self.stop_preview()
        self.preview_movie = self.selected.movie("idle", self, self.sprite.size())
        self.sprite.setMovie(self.preview_movie)
        self.preview_movie.start()
        for portrait in (self.side, self.bottom):
            portrait.portrait = self.selected.peek(portrait.bottom)
            portrait.update()
        self.message.hide()

    def stop_preview(self):
        if self.preview_movie is not None:
            self.sprite.clear()
            self.preview_movie.stop()
            self.preview_movie.deleteLater()
            self.preview_movie = None

    def preview_palette(self):
        palette = PALETTES[self.colors.currentData()]
        self.selected = replace(self.selected, palette=palette)
        self.skins.currentItem().setData(Qt.ItemDataRole.UserRole, self.selected)
        self.preview.setStyleSheet(
            f"QFrame#skinPreview{{background:{palette.background};border:1px solid {palette.border};"
            f"border-radius:12px;}} QLabel#skinSample{{background:{palette.surface};color:{palette.text};"
            "padding:6px 10px;border-radius:9px;}"
        )

    def reset_default(self):
        self.skins.setCurrentRow(0)
        self.colors.setCurrentIndex(self.colors.findData("lilac"))

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
        self.skins.setCurrentItem(self.add_skin(skin))

    def create_skin(self):
        form = NewSkinDialog(self, self.library)
        if form.exec() == QDialog.DialogCode.Accepted:
            self.skins.setCurrentItem(self.add_skin(form.skin))
        form.deleteLater()

    def save_selection(self):
        try:
            self.library.select(self.selected)
        except ValueError as error:
            self.message.setText(str(error))
            self.message.show()
            return
        self.accept()

    def show_centered(self):
        self.adjustSize()
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        self.move(screen.availableGeometry().center() - self.rect().center())
        self.show()
        self.raise_()
        self.activateWindow()


if __name__ == "__main__":
    import sys

    from capture import enable_dpi_awareness

    enable_dpi_awareness()
    app = QApplication(sys.argv[:1])
    library = SkinLibrary()
    dialog = AppearanceDialog(None, library, library.current())
    dialog.show_centered()
    sys.exit(app.exec())
