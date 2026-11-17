"""Daedalus PDF: view, combine, rearrange and mark up PDFs natively.

Left: page thumbnails (drag to reorder, multi-select, right-click for page actions). Centre: the pages in a
continuous scroll with the mark-up tools. Edits stay in memory until Save, which writes the file in one go
(mark-ups are drawn into the pages, so every PDF viewer shows them)."""
from __future__ import annotations

import base64
from pathlib import Path

from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QEvent,
    QIODevice,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPalette,
    QPixmap,
    QTransform,
)
from PySide6.QtPdf import QPdfDocument, QPdfDocumentRenderOptions
from PySide6.QtWidgets import (
    QAbstractItemView,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..pdf import ops as P
from . import icons

GAP = 14                                   # px between pages
THUMB = 118
TOOLS = [("select", "Select", "cursor", "Select, move and delete mark-ups (Delete removes; double-click text to edit)"),
         ("text", "Text", "text-t", "Click to place text"),
         ("highlight", "Highlight", "highlighter-circle", "Drag a box to highlight"),
         ("rect", "Box", "rectangle", "Drag a box outline"),
         ("arrow", "Arrow", "arrow-up-right", "Drag an arrow"),
         ("line", "Line", "line-segment", "Drag a line"),
         ("ink", "Pen", "pen", "Draw freehand"),
         ("whiteout", "Cover", "eraser", "White box over content — hides it on the page, but the text "
                                          "underneath stays in the file (not a redaction)"),
         ("image", "Image", "signature", "Place a signature or image (PNG / JPG); drag a box for its size")]
_WINDOWS: list = []


# ------------------------------------------------------------------ rendering
class Renderer:
    """Page images from the in-memory source bytes (QtPdf / PDFium), cached by size."""

    def __init__(self, doc: P.PdfDoc):
        self.doc = doc
        self._pdf: dict[str, QPdfDocument] = {}
        self._bufs: dict[str, QBuffer] = {}
        self._cache: dict[tuple, QImage] = {}

    def qdoc(self, src: str) -> QPdfDocument:
        if src not in self._pdf:
            ba = QByteArray(self.doc.data(src))
            buf = QBuffer()
            buf.setData(ba)
            buf.open(QIODevice.ReadOnly)
            d = QPdfDocument()
            d.load(buf)
            self._pdf[src], self._bufs[src] = d, buf
        return self._pdf[src]

    def forget(self, src=None):
        for k in [k for k in self._pdf if src is None or k == src]:
            self._pdf.pop(k).close()
            self._bufs.pop(k).close()
        self._cache = {k: v for k, v in self._cache.items() if src is not None and k[0] != src}

    def image(self, i: int, width_px: int) -> QImage:
        ref = self.doc.pages[i]
        bw, bh = self.doc.base_size(i)                 # before our extra rotation
        dw, dh = self.doc.size(i)                      # as displayed
        tw = max(8, int(width_px))
        rw = max(8, int(tw * dh / dw) if ref.rotate % 180 else tw)
        rh = max(8, int(rw * bh / bw))
        key = (ref.src, ref.index, ref.blank, rw, ref.rotate)
        img = self._cache.get(key)
        if img is None:
            if ref.blank:
                img = QImage(rw, rh, QImage.Format_RGB32)
                img.fill(Qt.white)
            else:
                d = self.qdoc(ref.src)
                opts = QPdfDocumentRenderOptions()
                img = d.render(ref.index, QSize(rw, rh), opts)
                if img.isNull():
                    img = QImage(rw, rh, QImage.Format_RGB32)
                    img.fill(QColor("#f3d7d7"))
                else:
                    bg = QImage(img.size(), QImage.Format_RGB32)
                    bg.fill(Qt.white)
                    p = QPainter(bg)
                    p.drawImage(0, 0, img)
                    p.end()
                    img = bg
            if ref.rotate:
                img = img.transformed(QTransform().rotate(ref.rotate), Qt.SmoothTransformation)
            if len(self._cache) > 160:
                self._cache.clear()
            self._cache[key] = img
        return img


# ------------------------------------------------------------------ page view
class PageView(QWidget):
    changed = Signal()                     # doc edited (mark-ups)
    current = Signal(int)                  # page under the view's centre

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.zoom = 1.0                    # px per point (logical)
        self.tool = "select"
        self.color = "#d11a1a"
        self.width_pt = 1.5
        self.font_pt = 12.0
        self.sel = None                    # (page, mark index)
        self._drag = None
        self._live = None                  # mark being drawn
        self._image = None                 # bytes for the image tool
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(QPalette.Window, QColor("#3a3f44"))
        self.setPalette(pal)

    @property
    def doc(self):
        return self.win.doc

    # layout
    def rects(self) -> list[QRectF]:
        out, y = [], GAP
        widest = max((self.doc.size(i)[0] for i in range(len(self.doc))), default=612) * self.zoom
        W = max(widest + 2 * GAP, self.win.scroll.viewport().width() if hasattr(self.win, "scroll") else 0)
        for i in range(len(self.doc)):
            w, h = self.doc.size(i)
            r = QRectF((W - w * self.zoom) / 2, y, w * self.zoom, h * self.zoom)
            out.append(r)
            y += r.height() + GAP
        return out

    def relayout(self):
        rs = self.rects()
        w = int(max((r.right() for r in rs), default=0) + GAP)
        h = int((rs[-1].bottom() if rs else 0) + GAP)
        self.setMinimumSize(w, h)
        self.resize(max(w, self.win.scroll.viewport().width() if hasattr(self.win, "scroll") else w), h)
        self.update()

    def page_at(self, pos) -> tuple[int, QPointF] | tuple[None, None]:
        for i, r in enumerate(self.rects()):
            if r.adjusted(-GAP / 2, -GAP / 2, GAP / 2, GAP / 2).contains(pos):
                return i, QPointF((pos.x() - r.left()) / self.zoom, (pos.y() - r.top()) / self.zoom)
        return None, None

    def scroll_to(self, i: int):
        rs = self.rects()
        if 0 <= i < len(rs):
            self.win.scroll.verticalScrollBar().setValue(int(rs[i].top() - GAP))

    # painting
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        vis = QRectF(e.rect())
        dpr = self.devicePixelRatioF()
        for i, r in enumerate(self.rects()):
            if not r.intersects(vis):
                continue
            p.fillRect(r.translated(2, 3), QColor(0, 0, 0, 60))
            img = self.win.renderer.image(i, int(r.width() * dpr))
            p.drawImage(r, img)
            p.save()
            p.translate(r.topLeft())
            marks = list(self.doc.pages[i].marks)
            if self._live and self._live[0] == i:
                marks.append(self._live[1])
            P.paint_marks(p, marks, self.zoom, self.sel[1] if self.sel and self.sel[0] == i else None)
            p.restore()
        p.end()

    # mouse
    def mousePressEvent(self, e):
        self.setFocus()
        i, pt = self.page_at(e.position())
        if i is None or e.button() != Qt.LeftButton:
            return super().mousePressEvent(e)
        x, y = pt.x(), pt.y()
        if self.tool == "select":
            self.sel = self.hit(i, pt)
            self._drag = (i, pt, dict(self.doc.pages[i].marks[self.sel[1]])) if self.sel else None
            self.update()
            return
        if self.tool == "text":
            self.place_text(i, x, y)
            return
        c, w = self.color, self.width_pt
        m = {"rect": {"kind": "rect", "x": x, "y": y, "w": 0, "h": 0, "color": c, "width": w},
             "highlight": {"kind": "highlight", "x": x, "y": y, "w": 0, "h": 0, "color": "#ffe14d"},
             "whiteout": {"kind": "whiteout", "x": x, "y": y, "w": 0, "h": 0, "color": "#ffffff"},
             "line": {"kind": "line", "x1": x, "y1": y, "x2": x, "y2": y, "color": c, "width": w},
             "arrow": {"kind": "arrow", "x1": x, "y1": y, "x2": x, "y2": y, "color": c, "width": w},
             "ink": {"kind": "ink", "points": [[x, y]], "color": c, "width": w},
             "image": {"kind": "image", "x": x, "y": y, "w": 0, "h": 0, "png": ""}}.get(self.tool)
        if m is not None:
            self._live = (i, m)
            self._origin = (x, y)

    def mouseMoveEvent(self, e):
        i, pt = self.page_at(e.position())
        if self._drag and pt is not None and i == self._drag[0]:
            pi, start, orig = self._drag
            m = P.move_mark(orig, pt.x() - start.x(), pt.y() - start.y())
            self.doc.pages[pi].marks[self.sel[1]] = m
            self.update()
            return
        if not self._live or pt is None or i != self._live[0]:
            return
        m, (x0, y0), x, y = self._live[1], self._origin, pt.x(), pt.y()
        if "points" in m:
            m["points"].append([x, y])
        elif "x1" in m:
            m["x2"], m["y2"] = x, y
            if e.modifiers() & Qt.ShiftModifier:          # snap to 45°
                import math
                dx, dy = x - x0, y - y0
                a = round(math.atan2(dy, dx) / (math.pi / 4)) * math.pi / 4
                L = math.hypot(dx, dy)
                m["x2"], m["y2"] = x0 + L * math.cos(a), y0 + L * math.sin(a)
        else:
            m["x"], m["y"], m["w"], m["h"] = min(x0, x), min(y0, y), abs(x - x0), abs(y - y0)
        self.update()

    def mouseReleaseEvent(self, e):
        if self._drag:
            pi, start, orig = self._drag
            new = self.doc.pages[pi].marks[self.sel[1]]
            self._drag = None
            if new != orig:
                self.doc.pages[pi].marks[self.sel[1]] = orig
                self.doc.set_mark(pi, self.sel[1], new)
                self.changed.emit()
            return
        if not self._live:
            return
        i, m = self._live
        self._live = None
        _, _, bw, bh = P.mark_bounds(m)
        if m["kind"] != "ink" and max(bw, bh) < 3:
            if m["kind"] == "image":                      # a click: default 150 pt wide
                m["w"], m["h"] = 150, 50
            else:
                self.update()
                return
        if m["kind"] == "image":
            if not self._image:
                self.update()
                return
            img = QImage.fromData(self._image)
            m["png"] = base64.b64encode(_png(img)).decode()
            if img.width() and img.height():               # keep the image's proportions inside the box
                ar = img.height() / img.width()
                m["h"] = m["w"] * ar
        self.add_mark(i, m)

    def mouseDoubleClickEvent(self, e):
        i, pt = self.page_at(e.position())
        if i is None:
            return
        h = self.hit(i, pt)
        if h and self.doc.pages[i].marks[h[1]].get("kind") == "text":
            m = dict(self.doc.pages[i].marks[h[1]])
            t = _ask_text(self, "Edit text", m.get("text", ""))
            if t is not None:
                if t.strip():
                    m["text"] = t
                    self.doc.set_mark(i, h[1], m)
                else:
                    self.doc.delete_mark(i, h[1])
                    self.sel = None
                self.changed.emit()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self.sel:
            self.doc.delete_mark(*self.sel)
            self.sel = None
            self.changed.emit()
            return
        if e.key() == Qt.Key_Escape:
            self.sel = None
            self._live = None
            self.update()
            return
        super().keyPressEvent(e)

    def hit(self, i, pt):
        for k in range(len(self.doc.pages[i].marks) - 1, -1, -1):
            x, y, w, h = P.mark_bounds(self.doc.pages[i].marks[k])
            if QRectF(x - 4, y - 4, w + 8, h + 8).contains(pt):
                return i, k
        return None

    def place_text(self, i, x, y):
        t = _ask_text(self, "Add text", "")
        if t and t.strip():
            self.add_mark(i, {"kind": "text", "x": x, "y": y, "text": t, "size": self.font_pt, "color": self.color})

    def add_mark(self, i, m):
        k = self.doc.add_mark(i, m)
        self.sel = (i, k) if self.tool == "select" else None
        self.changed.emit()
        self.update()


def _png(img: QImage) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba)


def _ask_text(parent, title, text):
    d = QDialog(parent)
    d.setWindowTitle(title)
    v = QVBoxLayout(d)
    ed = QPlainTextEdit(text)
    ed.setMinimumSize(360, 110)
    v.addWidget(ed)
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    bb.accepted.connect(d.accept)
    bb.rejected.connect(d.reject)
    v.addWidget(bb)
    ed.setFocus()
    return ed.toPlainText() if d.exec() else None


# ------------------------------------------------------------------ thumbnails
class Thumbs(QListWidget):
    reordered = Signal(list)

    def __init__(self):
        super().__init__()
        self.setViewMode(QListWidget.ListMode)
        self.setIconSize(QSize(THUMB, int(THUMB * 1.3)))
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSpacing(4)
        self.setMinimumWidth(THUMB + 46)
        self.setMaximumWidth(THUMB + 90)
        self.setContextMenuPolicy(Qt.CustomContextMenu)

    def dropEvent(self, e):
        if e.mimeData().hasUrls():                        # files from Explorer: let the window take them
            return self.window().dropEvent(e)
        super().dropEvent(e)
        order = [self.item(r).data(Qt.UserRole) for r in range(self.count())]
        if order != list(range(len(order))):
            self.reordered.emit(order)


# ------------------------------------------------------------------ window
class PdfWindow(QMainWindow):
    def __init__(self, path: str | None = None, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.doc = P.PdfDoc(path) if path else P.PdfDoc()
        self.renderer = Renderer(self.doc)
        self.resize(1180, 860)
        self.setAcceptDrops(True)

        self.thumbs = Thumbs()
        self.thumbs.reordered.connect(self._reordered)
        self.thumbs.currentRowChanged.connect(self._thumb_clicked)
        self.thumbs.customContextMenuRequested.connect(self._thumb_menu)
        self.view = PageView(self)
        self.view.changed.connect(self._edited)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.view)
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignHCenter)
        self.scroll.verticalScrollBar().valueChanged.connect(self._scrolled)
        self.scroll.viewport().installEventFilter(self)
        sp = QSplitter()
        sp.addWidget(self.thumbs)
        sp.addWidget(self.scroll)
        sp.setStretchFactor(1, 1)
        self.setCentralWidget(sp)
        self._build_actions()
        self.page_lbl = QLabel()
        self.statusBar().addPermanentWidget(self.page_lbl)
        self._fit = True
        self.refresh(rebuild=True)
        QTimer.singleShot(0, self.fit_width)

    # ---------------------------------------------------------------- actions
    def _act(self, text, fn, key=None, ico=None, tip=None, bar=None):
        a = QAction(icons.icon(ico) if ico else icons.icon(""), text, self)
        if key:
            a.setShortcut(QKeySequence(key))
        a.setToolTip(f"{tip or text}" + (f"  ({QKeySequence(key).toString(QKeySequence.NativeText)})" if key else ""))
        a.triggered.connect(lambda _=False: fn())
        self.addAction(a)
        if bar is not None:
            bar.addAction(a)
        return a

    def _build_actions(self):
        mb = self.menuBar()
        f = mb.addMenu("&File")
        tb = QToolBar("File")
        tb.setIconSize(QSize(18, 18))
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon if icons.available() else Qt.ToolButtonTextOnly)
        self.addToolBar(tb)
        for a in (self._act("Open…", self.open_dialog, QKeySequence.Open, "folder-open", bar=tb),
                  self._act("Add files…", self.add_dialog, "Ctrl+Shift+O", "file-plus",
                            "Combine: add PDFs after the selected page", bar=tb),
                  self._act("Save", self.save, QKeySequence.Save, "floppy-disk", bar=tb),
                  self._act("Save as…", self.save_as, QKeySequence.SaveAs, None)):
            f.addAction(a)
        f.addSeparator()
        f.addAction(self._act("Extract selected pages…", self.extract, None, "file-arrow-up",
                              "Save the selected pages as a new PDF"))
        f.addAction(self._act("Split…", self.split, None, "scissors", "One file per page or per N pages / ranges"))
        f.addSeparator()
        f.addAction(self._act("Print…", self.print_, QKeySequence.Print, "printer", bar=tb))
        f.addAction(self._act("Properties", self.properties, None))
        f.addAction(self._act("Read into DIMS…", self.read_into_dims, None, "database",
                              "Read this drawing's title block, BOM and revisions into the DIMS part profile"))
        f.addSeparator()
        f.addAction(self._act("Close", self.close, QKeySequence.Close))

        e = mb.addMenu("&Edit")
        tb.addSeparator()
        self.a_undo = self._act("Undo", self.undo, QKeySequence.Undo, "arrow-counter-clockwise", bar=tb)
        self.a_redo = self._act("Redo", self.redo, QKeySequence.Redo, "arrow-clockwise", bar=tb)
        e.addActions([self.a_undo, self.a_redo])
        e.addSeparator()
        e.addAction(self._act("Find text…", self.find, QKeySequence.Find, "magnifying-glass"))
        e.addAction(self._act("Select all pages", self.thumbs.selectAll, None))

        pg = mb.addMenu("&Pages")
        tb2 = QToolBar("Pages")
        tb2.setIconSize(QSize(18, 18))
        self.addToolBar(tb2)
        for a in (self._act("Rotate left", lambda: self.rotate(-90), "Ctrl+Shift+L", "arrow-counter-clockwise",
                            "Rotate the selected pages 90° left", bar=tb2),
                  self._act("Rotate right", lambda: self.rotate(90), "Ctrl+Shift+R", "arrow-clockwise",
                            "Rotate the selected pages 90° right", bar=tb2),
                  self._act("Delete pages", self.delete_pages, None, "trash", "Delete the selected pages", bar=tb2),
                  self._act("Duplicate", self.duplicate, "Ctrl+Shift+D", "copy", "Duplicate the selected pages",
                            bar=tb2),
                  self._act("Insert blank page", self.insert_blank, None, "file", "Blank page after the selection",
                            bar=tb2),
                  self._act("Move up", lambda: self.shift(-1), "Ctrl+Up", "caret-up", bar=tb2),
                  self._act("Move down", lambda: self.shift(1), "Ctrl+Down", "caret-down", bar=tb2),
                  self._act("Reverse order", self.reverse, None, "arrows-down-up", "Reverse the selected pages "
                                                                                  "(or all)"),
                  self._act("Clear mark-ups", self.clear_marks, None, "broom", "Remove mark-ups from the selected "
                                                                              "pages")):
            pg.addAction(a)

        v = mb.addMenu("&View")
        tb3 = QToolBar("View")
        tb3.setIconSize(QSize(18, 18))
        self.addToolBar(tb3)
        v.addAction(self._act("Zoom in", lambda: self.set_zoom(self.view.zoom * 1.2), QKeySequence.ZoomIn,
                              "magnifying-glass-plus", bar=tb3))
        v.addAction(self._act("Zoom out", lambda: self.set_zoom(self.view.zoom / 1.2), QKeySequence.ZoomOut,
                              "magnifying-glass-minus", bar=tb3))
        v.addAction(self._act("Fit width", self.fit_width, "Ctrl+0", "arrows-horizontal", bar=tb3))
        v.addAction(self._act("Actual size", lambda: self.set_zoom(96 / 72), "Ctrl+1", None))
        v.addAction(self._act("Go to page…", self.goto, "Ctrl+G", None))

        self.addToolBarBreak()
        mk = QToolBar("Mark up")
        mk.setIconSize(QSize(18, 18))
        mk.setToolButtonStyle(Qt.ToolButtonTextBesideIcon if icons.available() else Qt.ToolButtonTextOnly)
        self.addToolBar(mk)
        self.tool_acts = {}
        for key, lab, ico, tip in TOOLS:
            a = QAction(icons.icon(ico), lab, self)
            a.setCheckable(True)
            a.setToolTip(tip)
            a.triggered.connect(lambda _=False, k=key: self.set_tool(k))
            mk.addAction(a)
            self.tool_acts[key] = a
        self.tool_acts["select"].setChecked(True)
        mk.addSeparator()
        self.col_btn = QToolButton()
        self.col_btn.setToolTip("Colour for text, boxes, lines and pen")
        self.col_btn.clicked.connect(self.pick_color)
        mk.addWidget(self.col_btn)
        mk.addWidget(QLabel(" Line "))
        self.w_spin = QDoubleSpinBox()
        self.w_spin.setRange(0.25, 12)
        self.w_spin.setSingleStep(0.25)
        self.w_spin.setValue(1.5)
        self.w_spin.setSuffix(" pt")
        self.w_spin.valueChanged.connect(lambda x: setattr(self.view, "width_pt", x))
        mk.addWidget(self.w_spin)
        mk.addWidget(QLabel(" Text "))
        self.f_spin = QSpinBox()
        self.f_spin.setRange(4, 96)
        self.f_spin.setValue(12)
        self.f_spin.setSuffix(" pt")
        self.f_spin.valueChanged.connect(lambda x: setattr(self.view, "font_pt", float(x)))
        mk.addWidget(self.f_spin)
        self._paint_color_btn()

    # ---------------------------------------------------------------- state
    def refresh(self, rebuild=False, keep=None):
        n = len(self.doc)
        if rebuild or self.thumbs.count() != n:
            sel = keep if keep is not None else [i.row() for i in self.thumbs.selectedIndexes()]
            self.thumbs.blockSignals(True)
            self.thumbs.clear()
            for i in range(n):
                it = QListWidgetItem(f"{i + 1}")
                it.setData(Qt.UserRole, i)
                it.setTextAlignment(Qt.AlignHCenter)
                self.thumbs.addItem(it)
            for r in sel:
                if 0 <= r < n:
                    self.thumbs.item(r).setSelected(True)
            if sel and 0 <= sel[0] < n:
                self.thumbs.setCurrentRow(sel[0], self.thumbs.selectionModel().SelectionFlag.NoUpdate)
            self.thumbs.blockSignals(False)
            QTimer.singleShot(0, self._draw_thumbs)
        else:
            self._draw_thumbs()
        self.view.relayout()
        self._title()
        self.a_undo.setEnabled(self.doc.can_undo())
        self.a_redo.setEnabled(self.doc.can_redo())
        self._scrolled()

    def _draw_thumbs(self):
        for r in range(min(self.thumbs.count(), len(self.doc))):
            img = self.renderer.image(r, THUMB * 2)
            if self.doc.pages[r].marks:
                img = img.copy()
                p = QPainter(img)
                p.setRenderHint(QPainter.Antialiasing)
                P.paint_marks(p, self.doc.pages[r].marks, img.width() / self.doc.size(r)[0])
                p.end()
            pm = QPixmap.fromImage(img.scaled(THUMB, int(THUMB * 1.4), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            ic = QIcon(pm)
            ic.addPixmap(pm, QIcon.Selected)
            self.thumbs.item(r).setIcon(ic)
            self.thumbs.item(r).setData(Qt.UserRole, r)

    def _title(self):
        name = Path(self.doc.path).name if self.doc.path else "Untitled PDF"
        self.setWindowTitle(f"{name}{' •' if self.doc.dirty else ''} — Daedalus PDF")

    def selected(self) -> list[int]:
        rows = sorted({i.row() for i in self.thumbs.selectedIndexes()})
        if not rows and len(self.doc):
            rows = [max(0, self.thumbs.currentRow())]
        return rows

    def _edited(self):
        self.refresh()

    def _scrolled(self, *_):
        rs = self.view.rects()
        mid = self.scroll.verticalScrollBar().value() + self.scroll.viewport().height() / 2
        cur = next((i for i, r in enumerate(rs) if r.bottom() + GAP >= mid), max(0, len(rs) - 1))
        self.page_lbl.setText(f"Page {cur + 1} of {len(rs)}   ·   {round(self.view.zoom * 72 / 96 * 100)} %")

    def _thumb_clicked(self, row):
        if row >= 0:
            self.view.scroll_to(row)

    def eventFilter(self, obj, e):
        if obj is self.scroll.viewport():
            if e.type() == QEvent.Resize and self._fit:
                QTimer.singleShot(0, self.fit_width)
            elif e.type() == QEvent.Wheel and e.modifiers() & Qt.ControlModifier:
                self.set_zoom(self.view.zoom * (1.15 if e.angleDelta().y() > 0 else 1 / 1.15))
                return True
        return super().eventFilter(obj, e)

    def set_zoom(self, z, fit=False):
        self._fit = fit
        top = self.scroll.verticalScrollBar().value() / max(1, self.view.height())
        self.view.zoom = max(0.1, min(8.0, z))
        self.view.relayout()
        self.scroll.verticalScrollBar().setValue(int(top * self.view.height()))
        self._scrolled()

    def fit_width(self):
        if not len(self.doc):
            return
        widest = max(self.doc.size(i)[0] for i in range(len(self.doc)))
        self.set_zoom((self.scroll.viewport().width() - 2 * GAP - 4) / widest, fit=True)

    def set_tool(self, key):
        for k, a in self.tool_acts.items():
            a.setChecked(k == key)
        if key == "image":
            path, _ = QFileDialog.getOpenFileName(self, "Signature or image", "", "Images (*.png *.jpg *.jpeg *.bmp)")
            if not path:
                self.set_tool("select")
                return
            self.view._image = Path(path).read_bytes()
            self.statusBar().showMessage("Drag a box (or click) where the image goes", 6000)
        self.view.tool = key
        self.view.sel = None
        self.view.setCursor(Qt.ArrowCursor if key == "select" else Qt.IBeamCursor if key == "text" else Qt.CrossCursor)
        self.view.update()

    def pick_color(self):
        c = QColorDialog.getColor(QColor(self.view.color), self, "Mark-up colour")
        if c.isValid():
            self.view.color = c.name()
            self._paint_color_btn()

    def _paint_color_btn(self):
        pm = QPixmap(18, 18)
        pm.fill(QColor(self.view.color))
        self.col_btn.setIcon(pm)

    # ---------------------------------------------------------------- page ops
    def _do(self, fn, keep=None):
        try:
            out = fn()
        except P.PdfError as e:
            QMessageBox.warning(self, "PDF", str(e))
            return None
        self.view.sel = None
        self.refresh(rebuild=True, keep=keep if keep is not None else self.selected())
        return out

    def _reordered(self, order):
        self._do(lambda: self.doc.reorder(order), keep=[order.index(i) for i in self.selected() if i in order][:1])

    def rotate(self, deg):
        self._do(lambda: self.doc.rotate(self.selected(), deg))

    def delete_pages(self):
        s = self.selected()
        self._do(lambda: self.doc.delete(s), keep=[min(s[0], len(self.doc) - len(s) - 1)] if s else [])

    def duplicate(self):
        self._do(lambda: self.doc.duplicate(self.selected()))

    def insert_blank(self):
        s = self.selected()
        at = (s[-1] + 1) if s else len(self.doc)
        self._do(lambda: self.doc.insert_blank(at), keep=[at])

    def shift(self, d):
        s = self.selected()
        if not s or (d < 0 and s[0] == 0) or (d > 0 and s[-1] >= len(self.doc) - 1):
            return
        to = s[0] + d if d < 0 else s[-1] + 2
        self._do(lambda: self.doc.move(s, to), keep=[i + d for i in s])

    def reverse(self):
        s = self.selected()
        self._do(lambda: self.doc.reverse(s if len(s) > 1 else None))

    def clear_marks(self):
        self._do(lambda: self.doc.clear_marks(self.selected()))

    def undo(self):
        if self.doc.undo():
            self.view.sel = None
            self.refresh(rebuild=True)

    def redo(self):
        if self.doc.redo():
            self.view.sel = None
            self.refresh(rebuild=True)

    def _thumb_menu(self, pos):
        m = QMenu(self)
        for a in self.actions():
            if a.text() in ("Rotate left", "Rotate right", "Duplicate", "Insert blank page", "Move up", "Move down",
                            "Delete pages", "Extract selected pages…", "Clear mark-ups"):
                m.addAction(a)
        m.exec(self.thumbs.mapToGlobal(pos))

    # ---------------------------------------------------------------- files
    def _dir(self):
        from PySide6.QtCore import QSettings
        return QSettings("Daedalus", "Daedalus").value("pdf/dir") or (str(Path(self.doc.path).parent) if self.doc.path
                                                                     else str(Path.home()))

    def _remember(self, path):
        from PySide6.QtCore import QSettings
        QSettings("Daedalus", "Daedalus").setValue("pdf/dir", str(Path(path).parent))

    def open_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Open PDF", self._dir(), "PDF (*.pdf)")
        for p in paths:
            open_pdf(p, self.parent())
        if paths:
            self._remember(paths[0])
            if not len(self.doc) and not self.doc.dirty:
                self.close()

    def add_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add PDFs (combine)", self._dir(), "PDF (*.pdf)")
        if paths:
            self._remember(paths[0])
            self.add_files(paths)

    def add_files(self, paths, at=None):
        s = self.selected()
        at = at if at is not None else ((s[-1] + 1) if s and len(self.doc) else len(self.doc))
        n0, added = at, 0
        for p in paths:
            got = self._do(lambda p=p, at=n0 + added: self.doc.add_file(p, at=at))
            if got is None:
                return
            added += got
        if not self.doc.path and paths:
            self.doc.path = None
        self.refresh(rebuild=True, keep=list(range(n0, n0 + added)))
        if self._fit:
            self.fit_width()
        self.statusBar().showMessage(f"Added {added} page{'s' if added != 1 else ''}", 5000)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and any(u.toLocalFile().lower().endswith(".pdf") for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        self.dragEnterEvent(e)

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.toLocalFile().lower().endswith(".pdf")]
        if paths:
            e.acceptProposedAction()
            self.add_files(paths)

    def save(self) -> bool:
        if not self.doc.path:
            return self.save_as()
        return self._write(self.doc.path)

    def save_as(self) -> bool:
        start = self.doc.path or str(Path(self._dir()) / "combined.pdf")
        path, _ = QFileDialog.getSaveFileName(self, "Save PDF", start, "PDF (*.pdf)")
        if not path:
            return False
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        self._remember(path)
        return self._write(path)

    def _write(self, path) -> bool:
        try:
            self.doc.save(path)
        except P.PdfError as e:
            QMessageBox.warning(self, "Save PDF", str(e))
            return False
        self.renderer.forget()
        self.refresh(rebuild=True)
        self.statusBar().showMessage(f"Saved {Path(path).name}", 5000)
        return True

    def extract(self):
        s = self.selected()
        if not s:
            return
        stem = Path(self.doc.path).stem if self.doc.path else "pages"
        lab = f"{s[0] + 1}" if len(s) == 1 else f"{s[0] + 1}-{s[-1] + 1}"
        path, _ = QFileDialog.getSaveFileName(self, "Extract pages", str(Path(self._dir()) / f"{stem}_p{lab}.pdf"),
                                              "PDF (*.pdf)")
        if path:
            try:
                self.doc.save(path, idx=s)
                self.statusBar().showMessage(f"Saved {len(s)} page{'s' if len(s) != 1 else ''} to {Path(path).name}",
                                             6000)
            except P.PdfError as e:
                QMessageBox.warning(self, "Extract", str(e))

    def split(self):
        d = QDialog(self)
        d.setWindowTitle("Split PDF")
        f = QFormLayout(d)
        mode = QComboBox()
        mode.addItems(["Every page as its own file", "Every N pages", "By page ranges"])
        n = QSpinBox()
        n.setRange(1, 9999)
        n.setValue(2)
        rng = QLineEdit()
        rng.setPlaceholderText("e.g. 1-3; 4-6; 7-  (one file per range)")
        f.addRow("Split", mode)
        f.addRow("Pages per file", n)
        f.addRow("Ranges", rng)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        f.addRow(bb)
        if not d.exec():
            return
        folder = QFileDialog.getExistingDirectory(self, "Save the files in", self._dir())
        if not folder:
            return
        try:
            if mode.currentIndex() == 2:
                groups = []
                for part in rng.text().split(";"):
                    if part.strip():
                        idx = P.parse_ranges(part, len(self.doc))
                        groups.append((min(idx), max(idx)))
                files = self.doc.split(folder, ranges=groups)
            else:
                files = self.doc.split(folder, every=1 if mode.currentIndex() == 0 else n.value())
        except (P.PdfError, ValueError) as e:
            QMessageBox.warning(self, "Split", str(e))
            return
        self.statusBar().showMessage(f"Wrote {len(files)} files to {folder}", 8000)

    def print_(self):
        from PySide6.QtPrintSupport import QPrintDialog, QPrinter
        pr = QPrinter(QPrinter.HighResolution)
        dlg = QPrintDialog(pr, self)
        dlg.setMinMax(1, len(self.doc))
        if not dlg.exec():
            return
        a, b = (pr.fromPage(), pr.toPage()) if pr.fromPage() else (1, len(self.doc))
        p = QPainter(pr)
        for k, i in enumerate(range(a - 1, b)):
            if k:
                pr.newPage()
            area = pr.pageRect(QPrinter.DevicePixel)
            w, h = self.doc.size(i)
            s = min(area.width() / w, area.height() / h)
            img = self.renderer.image(i, int(w * s))
            p.save()
            p.translate((area.width() - w * s) / 2, (area.height() - h * s) / 2)
            p.drawImage(QRectF(0, 0, w * s, h * s), img)
            P.paint_marks(p, self.doc.pages[i].marks, s)
            p.restore()
        p.end()

    def properties(self):
        m = self.doc.metadata()
        rows = [f"<b>File:</b> {self.doc.path or '(not saved)'}", f"<b>Pages:</b> {len(self.doc)}"]
        if len(self.doc):
            w, h = self.doc.size(0)
            rows.append(f"<b>Page 1:</b> {w / 72:.2f} × {h / 72:.2f} in ({w * 25.4 / 72:.0f} × {h * 25.4 / 72:.0f} mm)")
        rows += [f"<b>{k}:</b> {v}" for k, v in m.items() if v]
        QMessageBox.information(self, "PDF properties", "<br>".join(rows))

    def read_into_dims(self):
        from PySide6.QtWidgets import QApplication as _A
        from .inventory_ui import InventoryPage
        if self.doc.dirty or not self.doc.path:
            if QMessageBox.question(self, "Read into DIMS", "Save the PDF first?") != QMessageBox.Yes or not self.save():
                return
        page = next((w for top in _A.topLevelWidgets() for w in ([top] + top.findChildren(InventoryPage))
                     if isinstance(w, InventoryPage)), None)
        if page is None:
            QMessageBox.information(self, "Read into DIMS", "Open DIMS (Inventory) first, then try again.")
            return
        page.read_drawings([self.doc.path])

    def find(self):
        q, ok = QInputDialog.getText(self, "Find text", "Find:", text=getattr(self, "_q", ""))
        if not ok or not q.strip():
            return
        self._q = q
        hits = self.doc.find(q)
        if not hits:
            self.statusBar().showMessage(f"“{q}” not found", 5000)
            return
        self.thumbs.clearSelection()
        for i in hits:
            self.thumbs.item(i).setSelected(True)
        self.view.scroll_to(hits[0])
        self.statusBar().showMessage(f"“{q}” on page{'s' if len(hits) > 1 else ''} "
                                     f"{', '.join(str(i + 1) for i in hits[:20])}", 10000)

    def goto(self):
        n, ok = QInputDialog.getInt(self, "Go to page", f"Page (1–{len(self.doc)}):", 1, 1, max(1, len(self.doc)))
        if ok:
            self.thumbs.setCurrentRow(n - 1)
            self.view.scroll_to(n - 1)

    def closeEvent(self, e):
        if self.doc.dirty:
            r = QMessageBox.question(self, "Daedalus PDF", f"Save changes to {Path(self.doc.path).name if self.doc.path else 'this PDF'}?",
                                     QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
            if r == QMessageBox.Cancel or (r == QMessageBox.Save and not self.save()):
                e.ignore()
                return
        self.renderer.forget()
        if self in _WINDOWS:
            _WINDOWS.remove(self)
        super().closeEvent(e)


def open_pdf(path: str | None = None, parent=None) -> PdfWindow | None:
    """Open a PDF (or an empty window to combine into) in its own Daedalus PDF window."""
    try:
        w = PdfWindow(path, None)
    except P.PdfError as e:
        QMessageBox.warning(parent, "Open PDF", str(e))
        return None
    if parent is not None and parent.windowIcon() and not parent.windowIcon().isNull():
        w.setWindowIcon(parent.windowIcon())
    _WINDOWS.append(w)
    w.show()
    w.raise_()
    w.activateWindow()
    return w


def open_dialog(parent=None):
    from PySide6.QtCore import QSettings
    d = QSettings("Daedalus", "Daedalus").value("pdf/dir") or str(Path.home())
    paths, _ = QFileDialog.getOpenFileNames(parent, "Open PDF", d, "PDF (*.pdf)")
    for p in paths:
        open_pdf(p, parent)
    if paths:
        QSettings("Daedalus", "Daedalus").setValue("pdf/dir", str(Path(paths[0]).parent))


def combine_dialog(parent=None):
    """Pick several PDFs → one window with all their pages, ready to reorder and save."""
    from PySide6.QtCore import QSettings
    d = QSettings("Daedalus", "Daedalus").value("pdf/dir") or str(Path.home())
    paths, _ = QFileDialog.getOpenFileNames(parent, "Combine PDFs (pick them in order)", d, "PDF (*.pdf)")
    if not paths:
        return None
    QSettings("Daedalus", "Daedalus").setValue("pdf/dir", str(Path(paths[0]).parent))
    w = open_pdf(None, parent)
    if w:
        w.add_files(paths, at=0)
    return w
