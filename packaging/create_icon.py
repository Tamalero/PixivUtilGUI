#!/usr/bin/env python3
"""
Generate the PNG icons for PixivUtilGUI.

Run once before building the AppImage (build-appimage.fish calls it).
Uses PyQt6, which is already a dependency, so there is no Pillow to install.

Writes pixivutilgui.png (512) and pixivutilgui-256.png next to this file.
"""

import sys
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QGuiApplication, QImage, QLinearGradient, QPainter,
    QPainterPath, QPen,
)

HERE = Path(__file__).resolve().parent
SIZE = 512

# Pixiv's blue, warmed slightly so the arrow reads against it.
TOP = QColor("#1f9df0")
BOTTOM = QColor("#0b6fb8")
ARROW = QColor("#ffffff")
TRAY = QColor("#eaf6ff")


def render(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)

    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / SIZE          # everything below is authored at 512 and scaled

    # Rounded-square background with a vertical gradient.
    gradient = QLinearGradient(QPointF(0, 0), QPointF(0, size))
    gradient.setColorAt(0.0, TOP)
    gradient.setColorAt(1.0, BOTTOM)
    body = QRectF(16 * s, 16 * s, size - 32 * s, size - 32 * s)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(gradient))
    p.drawRoundedRect(body, 96 * s, 96 * s)

    # Download arrow: shaft plus head.
    p.setBrush(QBrush(ARROW))
    p.drawRoundedRect(QRectF(226 * s, 118 * s, 60 * s, 168 * s), 22 * s, 22 * s)
    head = QPainterPath()
    head.moveTo(150 * s, 268 * s)
    head.lineTo(362 * s, 268 * s)
    head.lineTo(256 * s, 386 * s)
    head.closeSubpath()
    p.fillPath(head, QBrush(ARROW))

    # The tray it lands in — the "saved to disk" half of the idea.
    p.setBrush(QBrush(TRAY))
    p.drawRoundedRect(QRectF(132 * s, 404 * s, 248 * s, 30 * s), 15 * s, 15 * s)

    # A small "P" for Pixiv in the corner, so it is not just a generic arrow.
    # Pixel size, not point size: point sizes go through the font database's
    # DPI resolution, which qFatal()s on a headless box with no font config.
    font = QFont()
    font.setPixelSize(max(1, int(120 * s)))
    font.setBold(True)
    p.setFont(font)
    p.setPen(QPen(QColor(255, 255, 255, 235)))
    p.drawText(QRectF(58 * s, 52 * s, 130 * s, 130 * s),
               int(Qt.AlignmentFlag.AlignCenter), "P")
    p.end()
    return image


def main() -> int:
    # Must be kept in a live reference: if the QGuiApplication is collected,
    # the font database has no application to attach to and Qt qFatal()s.
    app = QGuiApplication(sys.argv)         # noqa: F841 — held on purpose
    for pixels, name in ((512, "pixivutilgui.png"), (256, "pixivutilgui-256.png")):
        out = HERE / name
        if not render(pixels).save(str(out), "PNG"):
            print(f"failed to write {out}", file=sys.stderr)
            return 1
        print(f"wrote {out} ({pixels}x{pixels})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
