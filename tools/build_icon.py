"""Generate the multi-resolution Windows icon from the project SVG."""
from pathlib import Path
import struct
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

app = QApplication([])
root = Path(__file__).resolve().parents[1]
icon = QIcon(str(root / 'assets' / 'tray.svg'))
images = []
for size in (16, 24, 32, 48, 64, 128, 256):
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not icon.pixmap(size, size).save(buffer, 'PNG'):
        raise RuntimeError('Icon rendering failed')
    images.append((size, bytes(buffer.data())))
offset = 6 + 16 * len(images)
header = struct.pack('<HHH', 0, 1, len(images))
entries = b''
for size, data in images:
    entries += struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
    offset += len(data)
(root / 'assets' / 'tray.ico').write_bytes(header + entries + b''.join(data for _, data in images))
