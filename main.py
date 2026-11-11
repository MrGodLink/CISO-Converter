#!/usr/bin/env python3
#
# =============================================================================
#  Project      : CISO Converter
#  Description  : Compress ISO to CSO and decompress CSO to ISO (PSP compatible, 9 levels)
#  Author       : Raffaele Catapano
#  License      : MIT (see LICENSE)
#  Created on   : 23/06/2025
#  Updated on   : 10/11/2026
#  Version      : 2.1 - PySide6 Dark Theme
#
#  Copyright (c) 2025-2026 Raffaele Catapano
#  Released under the MIT License - see the LICENSE file for details.
#  SPDX-License-Identifier: MIT
# =============================================================================

from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                              QHBoxLayout, QLabel, QLineEdit, QPushButton,
                              QSlider, QProgressBar, QTextEdit, QFileDialog,
                              QMessageBox, QFrame, QSizePolicy, QButtonGroup,
                              QScrollArea)
from PySide6.QtCore import Qt, QThread, QByteArray, Signal
from PySide6.QtGui import QPalette, QColor, QIcon, QPixmap, QPainter
from PySide6.QtSvg import QSvgRenderer
import sys
import html
import os
import struct
import time
import zlib
from pathlib import Path

# -----------------------------------------------------------------------------
#  Application data (the only place to edit at each release)
# -----------------------------------------------------------------------------
APP_NAME = "CISO Converter"
APP_VERSION = "2.1"
APP_AUTHOR = "Raffaele Catapano"
APP_YEARS = "2025-2026"
APP_ID = f"raffaelecatapano.cisoconvert.{APP_VERSION}"

CSO_BLOCK_SIZE = 2048          # ISO9660 / PSP UMD sector
CSO_ALIGN_MAX = 2              # maximum shift: index in 4-byte units (up to 8 GiB)
CSO_PLAIN_FLAG = 0x80000000    # index bit 31: uncompressed block
CSO_MAX_INDEX = 0x7FFFFFFF


def same_path(a, b):
    """True if the two paths point to the same file"""
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def part_path(output_file):
    """Temporary file the output is written to before the final rename"""
    return output_file + ".part"


def choose_align(total_blocks, block_size):
    """Smallest index shift that can address even the worst case
    (all blocks uncompressed). For PSP ISOs (< 2 GiB) it is 0."""
    data_start = 24 + 4 * (total_blocks + 1)
    for align in range(CSO_ALIGN_MAX + 1):
        step = 1 << align
        worst_end = data_start + total_blocks * (block_size + step - 1) + step
        if worst_end <= (CSO_MAX_INDEX << align):
            return align
    return CSO_ALIGN_MAX


def detect_input_kind(path):
    """Identify the file by its signature: 'cso' | 'zso' | 'iso' (ISO9660) | 'unknown'"""
    try:
        with open(path, 'rb') as f:
            head = f.read(4)
            if head == b'CISO':
                return "cso"
            if head == b'ZISO':
                return "zso"
            f.seek(0x8001)            # descrittore di volume ISO9660, settore 16
            if f.read(5) == b'CD001':
                return "iso"
    except OSError:
        pass
    return "unknown"


def deflate_raw(data, level):
    """Raw deflate (wbits=-15): the format required by CSO blocks"""
    comp = zlib.compressobj(level, zlib.DEFLATED, -15)
    return comp.compress(data) + comp.flush()


class CISOHeader:
    """CISO file header structure - PSP Compatible"""
    def __init__(self):
        self.magic = b'CISO'
        self.header_size = 0x18  # 24 bytes
        self.total_bytes = 0
        self.block_size = CSO_BLOCK_SIZE  # ISO9660 sector size (2048 bytes)
        self.ver = 1
        self.align = 0  # Index shift (scelto in compressione: 0 = offset in byte)
        self.rsv_06 = 0  # Reserved byte
        self.rsv_07 = 0  # Reserved byte

    def pack(self):
        """Pack header to bytes - PSP compatible format"""
        return struct.pack('<4sIQIBBBB',
                          self.magic,           # 4 bytes: "CISO"
                          self.header_size,     # 4 bytes: header size (24)
                          self.total_bytes,     # 8 bytes: uncompressed size
                          self.block_size,      # 4 bytes: block size (2048)
                          self.ver,             # 1 byte: version (1)
                          self.align,           # 1 byte: index shift
                          self.rsv_06,          # 1 byte: reserved
                          self.rsv_07)          # 1 byte: reserved

    @classmethod
    def unpack(cls, data):
        """Unpack header from bytes"""
        if len(data) < 24:
            raise ValueError("Header data too short")

        header = cls()
        unpacked = struct.unpack('<4sIQIBBBB', data[:24])
        header.magic = unpacked[0]
        header.header_size = unpacked[1]
        header.total_bytes = unpacked[2]
        header.block_size = unpacked[3]
        header.ver = unpacked[4]
        header.align = unpacked[5]
        header.rsv_06 = unpacked[6]
        header.rsv_07 = unpacked[7]
        return header


class CISOConverter:
    """Main CISO converter class - PSP Compatible (one instance per operation)"""

    def __init__(self, progress_callback=None, log_callback=None):
        self.progress_callback = progress_callback
        self.log_callback = log_callback
        self.stop_requested = False
        self.cancelled = False
        self._last_progress = -1
        self._tmp_path = None

    def log(self, message):
        """Log message"""
        if self.log_callback:
            self.log_callback(message)
        else:
            print(message)

    def update_progress(self, value):
        """Update progress (emitted only when the percentage changes)"""
        if value != self._last_progress:
            self._last_progress = value
            if self.progress_callback:
                self.progress_callback(value)

    def stop(self):
        """Request stop"""
        self.stop_requested = True

    def _remove_partial(self):
        """Remove the incomplete temporary file: the final output is never touched"""
        if not self._tmp_path:
            return
        try:
            if os.path.exists(self._tmp_path):
                os.remove(self._tmp_path)
                self.log("Partial temporary file removed")
        except OSError as e:
            self.log(f"Warning: could not remove partial temporary file: {e}")
        self._tmp_path = None

    # ------------------------------------------------------------------
    #  ISO -> CSO compression
    # ------------------------------------------------------------------
    def compress_iso(self, input_file, output_file, compression_level=6):
        """Compress ISO to CSO - PSP Compatible"""
        ok = False
        try:
            ok = self._compress(input_file, output_file, compression_level)
        except Exception as e:
            self.log(f"Compression error: {str(e)}")
        if not ok:
            self._remove_partial()
        return ok

    def _compress(self, input_file, output_file, compression_level):
        tmp_file = part_path(output_file)
        if same_path(input_file, output_file) or same_path(input_file, tmp_file):
            raise ValueError("Input and output files must be different")

        kind = detect_input_kind(input_file)
        if kind in ("cso", "zso"):
            raise ValueError("Input file is already a compressed image (CSO/ZSO)")
        if kind == "unknown":
            self.log("Warning: ISO9660 signature not found - "
                     "the file may not be a valid PSP ISO")

        compression_level = max(1, min(9, int(compression_level)))
        started = time.monotonic()
        self.log(f"Compressing '{input_file}' to '{output_file}'")

        # Check input file size
        file_size = os.path.getsize(input_file)
        if file_size == 0:
            raise ValueError("Input file is empty")

        # Standard PSP ISO images are a multiple of 2048 bytes
        if file_size % CSO_BLOCK_SIZE != 0:
            self.log("Warning: file size is not a multiple of 2048 bytes - "
                     "the last block will be zero-padded")

        # Create CISO header
        header = CISOHeader()
        header.total_bytes = file_size
        total_blocks = (file_size + header.block_size - 1) // header.block_size
        header.align = choose_align(total_blocks, header.block_size)

        self.log(f"Total file size: {file_size} bytes")
        self.log(f"Block size: {header.block_size} bytes")
        self.log(f"Total blocks: {total_blocks}")
        self.log(f"Compression level: {compression_level}")
        self.log(f"Alignment: {1 << header.align} bytes")

        # Allocate index buffer
        index_buf = [0] * (total_blocks + 1)
        align_bytes = 1 << header.align
        align_mask = align_bytes - 1

        with open(input_file, 'rb') as fin, open(tmp_file, 'wb') as fout:
            self._tmp_path = tmp_file

            # Write header placeholder
            fout.write(header.pack())

            # Write index placeholder
            index_pos = fout.tell()
            index_size = (total_blocks + 1) * 4
            fout.write(b'\x00' * index_size)

            # Start writing compressed data
            write_pos = fout.tell()
            data_start = write_pos

            # Compress blocks
            for block in range(total_blocks):
                if self.stop_requested:
                    self.cancelled = True
                    self.log("Compression cancelled by user")
                    return False

                # Update progress
                progress = int((block / total_blocks) * 100)
                self.update_progress(progress)

                if block % max(1, total_blocks // 20) == 0:
                    if block > 0:
                        avg_rate = int(100 * (write_pos - data_start) / (block * header.block_size))
                        self.log(f"Progress: {progress}% - Average compression: {avg_rate}%")
                    else:
                        self.log(f"Progress: {progress}%")

                # Align write position (PSP requirement)
                if write_pos & align_mask:
                    padding = align_bytes - (write_pos & align_mask)
                    fout.write(b'\x00' * padding)
                    write_pos += padding

                # Store block offset in index (divided by alignment)
                block_index = write_pos >> header.align
                if block_index > CSO_MAX_INDEX:
                    raise ValueError("Output exceeds the maximum size supported "
                                     "by the CSO format (8 GiB)")
                index_buf[block] = block_index

                # Read block data
                block_data = fin.read(header.block_size)
                if not block_data:
                    raise ValueError("Unexpected end of input file")

                # Pad last block if necessary
                if len(block_data) < header.block_size:
                    padding_size = header.block_size - len(block_data)
                    block_data += b'\x00' * padding_size

                # Compress block using raw deflate
                try:
                    compressed_data = deflate_raw(block_data, compression_level)
                except Exception as e:
                    self.log(f"Compression error for block {block}: {e}")
                    compressed_data = block_data  # Fallback to uncompressed

                # Use compressed data only if it is actually smaller
                if len(compressed_data) < len(block_data):
                    final_data = compressed_data
                else:
                    final_data = block_data
                    index_buf[block] |= CSO_PLAIN_FLAG  # bit 31: plain block

                # Write block data
                fout.write(final_data)
                write_pos += len(final_data)

            # Align the end of the data too: the last index entry is stored shifted,
            # an unaligned end would lose up to 3 bytes of the last block
            if write_pos & align_mask:
                padding = align_bytes - (write_pos & align_mask)
                fout.write(b'\x00' * padding)
                write_pos += padding

            # Store final position
            end_index = write_pos >> header.align
            if end_index > CSO_MAX_INDEX:
                raise ValueError("Output exceeds the maximum size supported "
                                 "by the CSO format (8 GiB)")
            index_buf[total_blocks] = end_index

            # Write the actual index
            fout.seek(index_pos)
            fout.write(struct.pack(f'<{total_blocks + 1}I', *index_buf))

        # Success: the temporary file replaces the output in one step
        os.replace(tmp_file, output_file)
        self._tmp_path = None

        # Final statistics (write_pos = final file size)
        compression_ratio = int(write_pos * 100 / file_size)
        self.update_progress(100)
        self.log("CISO compression completed successfully!")
        self.log(f"Original size: {file_size} bytes")
        self.log(f"Compressed size: {write_pos} bytes")
        self.log(f"Compression ratio: {compression_ratio}%")
        self.log(f"Elapsed time: {time.monotonic() - started:.1f} s")
        return True

    # ------------------------------------------------------------------
    #  CSO -> ISO decompression
    # ------------------------------------------------------------------
    def decompress_cso(self, input_file, output_file):
        """Decompress CSO to ISO - PSP Compatible"""
        ok = False
        try:
            ok = self._decompress(input_file, output_file)
        except Exception as e:
            self.log(f"Decompression error: {str(e)}")
        if not ok:
            self._remove_partial()
        return ok

    def _decompress(self, input_file, output_file):
        tmp_file = part_path(output_file)
        if same_path(input_file, output_file) or same_path(input_file, tmp_file):
            raise ValueError("Input and output files must be different")

        started = time.monotonic()
        self.log(f"Decompressing '{input_file}' to '{output_file}'")

        with open(input_file, 'rb') as fin:
            # Read and validate header
            header_data = fin.read(24)
            if len(header_data) != 24:
                raise ValueError("Invalid CSO file: cannot read header")

            header = CISOHeader.unpack(header_data)

            if header.magic == b'ZISO':
                raise ValueError("ZSO files (LZ4) are not supported - only CSO (deflate)")

            if header.magic != b'CISO':
                raise ValueError(f"Invalid CSO file: wrong magic number (got {header.magic})")

            if header.ver > 1:
                raise ValueError(f"Unsupported CSO version {header.ver} (only v1 is supported)")

            if header.block_size != CSO_BLOCK_SIZE:
                raise ValueError(f"Invalid block size: {header.block_size} (expected {CSO_BLOCK_SIZE})")

            if header.total_bytes == 0:
                raise ValueError("Invalid CSO file: zero total bytes")

            total_blocks = (header.total_bytes + header.block_size - 1) // header.block_size

            self.log(f"Original size: {header.total_bytes} bytes")
            self.log(f"Block size: {header.block_size} bytes")
            self.log(f"Total blocks: {total_blocks}")
            self.log(f"Version: {header.ver}")
            self.log(f"Alignment: {1 << header.align} bytes")

            # Read index table
            index_size = (total_blocks + 1) * 4
            index_data = fin.read(index_size)
            if len(index_data) != index_size:
                raise ValueError("Invalid CSO file: cannot read index table")

            index_buf = list(struct.unpack(f'<{total_blocks + 1}I', index_data))

            with open(tmp_file, 'wb') as fout:
                self._tmp_path = tmp_file
                written = 0

                # Decompress blocks
                for block in range(total_blocks):
                    if self.stop_requested:
                        self.cancelled = True
                        self.log("Decompression cancelled by user")
                        return False

                    # Update progress
                    progress = int((block / total_blocks) * 100)
                    self.update_progress(progress)

                    if block % max(1, total_blocks // 20) == 0:
                        self.log(f"Decompression progress: {progress}%")

                    # Get block information
                    current_index = index_buf[block]
                    next_index = index_buf[block + 1]

                    # Check if block is compressed or plain
                    is_plain = (current_index & CSO_PLAIN_FLAG) != 0
                    current_index &= CSO_MAX_INDEX  # Remove compression flag
                    next_index &= CSO_MAX_INDEX

                    # Calculate read position and size (includes alignment padding)
                    read_pos = current_index << header.align
                    read_size = (next_index - current_index) << header.align

                    if read_size <= 0:
                        raise ValueError(f"Block {block}: invalid read size {read_size}")

                    # Read compressed/plain data
                    fin.seek(read_pos)
                    block_data = fin.read(read_size)

                    if len(block_data) != read_size:
                        raise ValueError(f"Block {block}: read error (expected {read_size}, got {len(block_data)})")

                    # Bytes expected from this block (the last one may be shorter)
                    expected = min(header.block_size, header.total_bytes - written)

                    if is_plain:
                        output_data = block_data[:header.block_size]
                    else:
                        # Raw deflate stream: alignment padding after it is ignored
                        try:
                            output_data = zlib.decompressobj(-15).decompress(
                                block_data, header.block_size)
                        except zlib.error as e:
                            raise ValueError(f"Block {block}: decompression failed - {str(e)}")

                    if len(output_data) < expected:
                        raise ValueError(f"Block {block}: size mismatch "
                                         f"(expected {expected}, got {len(output_data)})")

                    # Write decompressed block (truncated to the real ISO size)
                    fout.write(output_data[:expected])
                    written += expected

        # Success: the temporary file replaces the output in one step
        os.replace(tmp_file, output_file)
        self._tmp_path = None

        self.update_progress(100)
        self.log("CSO decompression completed successfully!")
        self.log(f"ISO size: {written} bytes")
        self.log(f"Elapsed time: {time.monotonic() - started:.1f} s")
        return True


class ConversionThread(QThread):
    """Thread for conversion process"""
    log_signal = Signal(str)
    progress_signal = Signal(int)
    finished_signal = Signal(str)   # "success" | "error" | "cancelled"

    def __init__(self, mode, input_file, output_file, compression_level):
        super().__init__()
        self.mode = mode
        self.input_file = input_file
        self.output_file = output_file
        self.compression_level = compression_level
        self.converter = None
        self._stop_flag = False

    def run(self):
        """Run conversion"""
        try:
            self.converter = CISOConverter(
                progress_callback=self.progress_signal.emit,
                log_callback=self.log_signal.emit
            )
            if self._stop_flag:
                self.converter.stop()

            if self.mode == "compress":
                success = self.converter.compress_iso(
                    self.input_file, self.output_file, self.compression_level
                )
            else:
                success = self.converter.decompress_cso(self.input_file, self.output_file)

            if success:
                status = "success"
            elif self.converter.cancelled:
                status = "cancelled"
            else:
                status = "error"
            self.finished_signal.emit(status)

        except Exception as e:
            self.log_signal.emit(f"Critical error: {str(e)}")
            self.finished_signal.emit("error")

    def stop(self):
        """Stop conversion"""
        self._stop_flag = True
        if self.converter:
            self.converter.stop()


# =============================================================================
#  GRAPHICS - SVG icon generated in the code + modern dark theme
# =============================================================================

# App icon: a disc (ISO) with two arrows "compressing" it from both sides.
# It is drawn directly in the code, so no external files are needed.
APP_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#2563eb"/>
      <stop offset="1" stop-color="#06b6d4"/>
    </linearGradient>
    <linearGradient id="disc" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#ffffff"/>
      <stop offset="1" stop-color="#cfe6ff"/>
    </linearGradient>
  </defs>
  <rect width="128" height="128" rx="28" fill="url(#bg)"/>
  <path d="M0 28 A28 28 0 0 1 28 0 H100 A28 28 0 0 1 128 28 V38 C90 52 40 52 0 38 Z"
        fill="#ffffff" fill-opacity="0.10"/>
  <circle cx="64" cy="64" r="28" fill="url(#disc)"/>
  <circle cx="64" cy="64" r="19" fill="none" stroke="#2563eb" stroke-opacity="0.18" stroke-width="2"/>
  <circle cx="64" cy="64" r="9" fill="url(#bg)"/>
  <circle cx="64" cy="64" r="3" fill="#ffffff" fill-opacity="0.9"/>
  <path d="M14 50 L28 64 L14 78" fill="none" stroke="#ffffff" stroke-width="7"
        stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M114 50 L100 64 L114 78" fill="none" stroke="#ffffff" stroke-width="7"
        stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""


def svg_pixmap(size):
    """Render the SVG icon into a QPixmap of the requested size"""
    renderer = QSvgRenderer(QByteArray(APP_ICON_SVG.encode("utf-8")))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    renderer.render(painter)
    painter.end()
    return pixmap


def build_app_icon():
    """Create a multi-resolution QIcon (window, taskbar, Alt+Tab)"""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(svg_pixmap(size))
    return icon


STYLESHEET = """
QMainWindow, QWidget#root, QScrollArea#scroll, QWidget#qt_scrollarea_viewport {
    background-color: #0f1117;
}
QWidget {
    color: #e6e9f0;
    font-family: "Segoe UI", "Inter", "SF Pro Text", "Helvetica Neue", Arial, sans-serif;
    font-size: 13px;
}

/* ---------- Header ---------- */
QFrame#header {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                      stop:0 #1b2338, stop:1 #12161f);
    border: 1px solid #272d3c;
    border-radius: 14px;
}
QLabel#title {
    font-size: 22px;
    font-weight: 700;
    color: #ffffff;
}
QLabel#subtitle {
    color: #8b93a7;
    font-size: 12px;
}
QLabel#badge {
    background-color: rgba(59, 130, 246, 0.16);
    color: #7db0ff;
    border: 1px solid rgba(59, 130, 246, 0.40);
    border-radius: 10px;
    padding: 2px 10px;
    font-size: 11px;
    font-weight: 600;
}

/* ---------- Card ---------- */
QFrame#card {
    background-color: #171a22;
    border: 1px solid #252a36;
    border-radius: 12px;
}
QLabel#cardTitle {
    color: #8b93a7;
    font-size: 11px;
    font-weight: 700;
}
QLabel#hint {
    color: #5f6880;
    font-size: 11px;
}
QLabel#hint:disabled {
    color: #3a4153;
}
QLabel#levelBadge {
    background-color: rgba(34, 211, 238, 0.12);
    color: #5eead4;
    border: 1px solid rgba(34, 211, 238, 0.35);
    border-radius: 9px;
    padding: 0px;
    font-weight: 700;
}
QLabel#levelBadge:disabled {
    background-color: #1a1e29;
    color: #4a5266;
    border: 1px solid #252a36;
}
QLabel#footer {
    color: #4f586d;
    font-size: 11px;
}

/* ---------- Selettore modalita' (segmented) ---------- */
QPushButton#segment {
    background-color: #0c0e13;
    border: 1px solid #252a36;
    border-radius: 8px;
    padding: 11px 18px;
    color: #8b93a7;
    font-weight: 600;
}
QPushButton#segment:hover {
    color: #e6e9f0;
    border: 1px solid #3a4258;
}
QPushButton#segment:checked {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                      stop:0 rgba(37, 99, 235, 0.35),
                                      stop:1 rgba(6, 182, 212, 0.25));
    border: 1px solid #3b82f6;
    color: #ffffff;
}

/* ---------- Campi di testo ---------- */
QLineEdit {
    background-color: #0c0e13;
    border: 1px solid #252a36;
    border-radius: 8px;
    padding: 9px 12px;
    selection-background-color: #3b82f6;
    selection-color: #ffffff;
}
QLineEdit:hover {
    border: 1px solid #343b4d;
}
QLineEdit:focus {
    border: 1px solid #3b82f6;
}

/* ---------- Pulsanti ---------- */
QPushButton {
    background-color: #202431;
    border: 1px solid #2c3242;
    border-radius: 8px;
    padding: 9px 18px;
    font-weight: 600;
    min-width: 60px;
}
QPushButton:hover {
    background-color: #272c3c;
    border: 1px solid #3b82f6;
}
QPushButton:pressed {
    background-color: #1a1e29;
}
QPushButton:disabled {
    background-color: #181b24;
    border: 1px solid #222736;
    color: #4a5266;
}

QPushButton#primary {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                      stop:0 #2563eb, stop:1 #06b6d4);
    border: none;
    border-radius: 10px;
    padding: 12px 38px;
    color: #ffffff;
    font-size: 14px;
    font-weight: 700;
}
QPushButton#primary:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                      stop:0 #3b82f6, stop:1 #22d3ee);
    border: none;
}
QPushButton#primary:pressed {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                      stop:0 #1d4ed8, stop:1 #0891b2);
}
QPushButton#primary:disabled {
    background-color: #1c202b;
    color: #4a5266;
    border: none;
}

QPushButton#danger {
    background-color: transparent;
    border: 1px solid #ef4444;
    border-radius: 10px;
    padding: 12px 38px;
    color: #ff7b7b;
    font-size: 14px;
    font-weight: 700;
}
QPushButton#danger:hover {
    background-color: rgba(239, 68, 68, 0.14);
    border: 1px solid #ff6b6b;
}
QPushButton#danger:pressed {
    background-color: rgba(239, 68, 68, 0.25);
}
QPushButton#danger:disabled {
    background-color: transparent;
    border: 1px solid #272c3a;
    color: #4a5266;
}

/* ---------- Slider ---------- */
QSlider::groove:horizontal {
    height: 6px;
    background-color: #0c0e13;
    border-radius: 3px;
}
QSlider::sub-page:horizontal {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                      stop:0 #2563eb, stop:1 #22d3ee);
    border-radius: 3px;
}
QSlider::handle:horizontal {
    background-color: #ffffff;
    width: 18px;
    height: 18px;
    margin: -6px 0;
    border-radius: 9px;
}
QSlider::handle:horizontal:hover {
    background-color: #bfe3ff;
}
QSlider::groove:horizontal:disabled {
    background-color: #181b24;
}
QSlider::sub-page:horizontal:disabled {
    background-color: #2a2f3d;
}
QSlider::handle:horizontal:disabled {
    background-color: #4a5266;
}

/* ---------- Progress bar ---------- */
QProgressBar {
    background-color: #0c0e13;
    border: 1px solid #252a36;
    border-radius: 9px;
    min-height: 22px;
    max-height: 22px;
    text-align: center;
    color: #ffffff;
    font-weight: 700;
}
QProgressBar::chunk {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                      stop:0 #2563eb, stop:1 #22d3ee);
    border-radius: 8px;
    margin: 1px;
}

/* ---------- Log ---------- */
QTextEdit#log {
    background-color: #0a0c10;
    border: 1px solid #252a36;
    border-radius: 8px;
    padding: 8px;
    color: #a9b4cc;
    font-family: "Cascadia Mono", "JetBrains Mono", "Consolas", "Menlo", "DejaVu Sans Mono", monospace;
    font-size: 12px;
    selection-background-color: #3b82f6;
}

/* ---------- Scrollbar ---------- */
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: #2c3242;
    border-radius: 4px;
    min-height: 28px;
}
QScrollBar::handle:vertical:hover {
    background: #3d4560;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
}

/* ---------- Finestre di dialogo ---------- */
QMessageBox, QDialog {
    background-color: #171a22;
}
QMessageBox QLabel {
    color: #e6e9f0;
    font-size: 13px;
}
QToolTip {
    background-color: #202431;
    color: #e6e9f0;
    border: 1px solid #3b82f6;
    padding: 4px 8px;
}
"""


class CISOApp(QMainWindow):
    """Main application GUI"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION} * Author: {APP_AUTHOR}")
        self.setWindowIcon(build_app_icon())
        self.setMinimumSize(560, 460)
        self.fit_to_screen()

        self.conversion_thread = None
        self.processing = False
        self.current_mode = "compress"

        self.setup_ui()
        self.apply_dark_theme()

    def fit_to_screen(self):
        """Initial size that does not exceed the available screen (taskbar excluded)"""
        screen = self.screen() or QApplication.primaryScreen()
        avail = screen.availableGeometry()
        self.resize(min(680, avail.width() - 40), min(940, avail.height() - 80))

    # ------------------------------------------------------------------
    #  UI helpers
    # ------------------------------------------------------------------
    def _make_card(self, title, right_widget=None):
        """Create a 'card' with a title and return (frame, content layout)"""
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(10)

        title_row = QHBoxLayout()
        title_label = QLabel(title.upper())
        title_label.setObjectName("cardTitle")
        title_row.addWidget(title_label)
        title_row.addStretch()
        if right_widget is not None:
            title_row.addWidget(right_widget)
        layout.addLayout(title_row)
        return card, layout

    def setup_ui(self):
        """Setup the user interface"""
        # Central widget
        central_widget = QWidget()
        central_widget.setObjectName("root")

        # The content scrolls if the screen is too short, instead of being cut off
        scroll = QScrollArea()
        scroll.setObjectName("scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(central_widget)
        self.setCentralWidget(scroll)

        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(20, 16, 20, 12)
        main_layout.setSpacing(10)

        # ---------------- Header ----------------
        header = QFrame()
        header.setObjectName("header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 14, 16, 14)
        header_layout.setSpacing(14)

        icon_label = QLabel()
        icon_label.setPixmap(svg_pixmap(56))
        icon_label.setFixedSize(56, 56)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        title_label = QLabel(APP_NAME)
        title_label.setObjectName("title")
        subtitle_label = QLabel("ISO  \u2194  CSO  \u00b7  PSP compatible  \u00b7  9 compression levels")
        subtitle_label.setObjectName("subtitle")
        text_col.addStretch()
        text_col.addWidget(title_label)
        text_col.addWidget(subtitle_label)
        text_col.addStretch()

        badge = QLabel(f"v{APP_VERSION}")
        badge.setObjectName("badge")

        header_layout.addWidget(icon_label)
        header_layout.addLayout(text_col, 1)
        header_layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        main_layout.addWidget(header)

        # ---------------- Mode selection ----------------
        mode_card, mode_box = self._make_card("Mode")
        mode_layout = QHBoxLayout()
        mode_layout.setSpacing(8)
        self.compress_radio = QPushButton("Compress ISO to CSO")
        self.decompress_radio = QPushButton("Decompress CSO to ISO")
        for btn in (self.compress_radio, self.decompress_radio):
            btn.setObjectName("segment")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            mode_layout.addWidget(btn)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_group.addButton(self.compress_radio)
        self.mode_group.addButton(self.decompress_radio)
        self.compress_radio.setChecked(True)
        self.compress_radio.toggled.connect(self.on_mode_change)
        mode_box.addLayout(mode_layout)
        main_layout.addWidget(mode_card)

        # ---------------- Compression level ----------------
        self.level_label = QLabel("6")
        self.level_label.setObjectName("levelBadge")
        self.level_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.level_label.setFixedSize(44, 22)
        level_card, level_box = self._make_card("Compression Level", self.level_label)

        self.level_slider = QSlider(Qt.Orientation.Horizontal)
        self.level_slider.setMinimum(1)
        self.level_slider.setMaximum(9)
        self.level_slider.setValue(6)
        self.level_slider.setTickPosition(QSlider.TickPosition.NoTicks)
        self.level_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.level_slider.valueChanged.connect(self.on_level_change)
        level_box.addWidget(self.level_slider)

        hint_row = QHBoxLayout()
        hint_fast = QLabel("1 \u00b7 Faster")
        hint_fast.setObjectName("hint")
        hint_small = QLabel("9 \u00b7 Smaller file")
        hint_small.setObjectName("hint")
        hint_row.addWidget(hint_fast)
        hint_row.addStretch()
        hint_row.addWidget(hint_small)
        level_box.addLayout(hint_row)
        self.level_hints = (hint_fast, hint_small)
        main_layout.addWidget(level_card)

        # ---------------- Input file ----------------
        input_card, input_box = self._make_card("Input File")
        input_layout = QHBoxLayout()
        input_layout.setSpacing(8)
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("Select the source file...")
        self.input_button = QPushButton("Browse...")
        self.input_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.input_button.clicked.connect(self.browse_input)
        input_layout.addWidget(self.input_edit)
        input_layout.addWidget(self.input_button)
        input_box.addLayout(input_layout)
        main_layout.addWidget(input_card)

        # ---------------- Output file ----------------
        output_card, output_box = self._make_card("Output File")
        output_layout = QHBoxLayout()
        output_layout.setSpacing(8)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Select the destination file...")
        self.output_button = QPushButton("Browse...")
        self.output_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.output_button.clicked.connect(self.browse_output)
        output_layout.addWidget(self.output_edit)
        output_layout.addWidget(self.output_button)
        output_box.addLayout(output_layout)
        main_layout.addWidget(output_card)

        # ---------------- Progress ----------------
        progress_card, progress_box = self._make_card("Progress")
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        progress_box.addWidget(self.progress_bar)
        main_layout.addWidget(progress_card)

        # ---------------- Buttons ----------------
        button_layout = QHBoxLayout()
        button_layout.setSpacing(12)
        button_layout.addStretch()
        self.start_button = QPushButton("\u25b6  Start")
        self.start_button.setObjectName("primary")
        self.start_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_button.clicked.connect(self.start_conversion)
        self.stop_button = QPushButton("\u25a0  Stop")
        self.stop_button.setObjectName("danger")
        self.stop_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_button.clicked.connect(self.stop_conversion)
        self.stop_button.setEnabled(False)
        button_layout.addWidget(self.start_button)
        button_layout.addWidget(self.stop_button)
        button_layout.addStretch()
        main_layout.addLayout(button_layout)

        # ---------------- Log ----------------
        log_card, log_box = self._make_card("Log")
        self.log_text = QTextEdit()
        self.log_text.setObjectName("log")
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(100)
        log_box.addWidget(self.log_text)
        main_layout.addWidget(log_card, 1)

        # ---------------- Footer ----------------
        footer = QLabel(f"\u00a9 {APP_YEARS} {APP_AUTHOR}  \u00b7  MIT License")
        footer.setObjectName("footer")
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(footer)

        # Initial UI state
        self.on_mode_change()

    def apply_dark_theme(self):
        """Apply dark theme to the application"""
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(15, 17, 23))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(230, 233, 240))
        palette.setColor(QPalette.ColorRole.Base, QColor(12, 14, 19))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor(23, 26, 34))
        palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(32, 36, 49))
        palette.setColor(QPalette.ColorRole.ToolTipText, QColor(230, 233, 240))
        palette.setColor(QPalette.ColorRole.Text, QColor(230, 233, 240))
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(95, 104, 128))
        palette.setColor(QPalette.ColorRole.Button, QColor(32, 36, 49))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(230, 233, 240))
        palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 80, 80))
        palette.setColor(QPalette.ColorRole.Link, QColor(59, 130, 246))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(59, 130, 246))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
        self.setPalette(palette)
        self.setStyleSheet(STYLESHEET)

    def on_mode_change(self):
        """Handle mode change (the slider is active only when compressing and idle)"""
        enabled = self.compress_radio.isChecked() and not self.processing
        self.level_slider.setEnabled(enabled)
        self.level_label.setEnabled(enabled)
        for hint in self.level_hints:
            hint.setEnabled(enabled)

    def set_controls_enabled(self, enabled):
        """Lock/unlock the configuration controls during a conversion"""
        for widget in (self.compress_radio, self.decompress_radio,
                       self.input_edit, self.output_edit,
                       self.input_button, self.output_button):
            widget.setEnabled(enabled)
        self.on_mode_change()

    def on_level_change(self, value):
        """Handle compression level change"""
        self.level_label.setText(str(value))

    def browse_input(self):
        """Browse for input file"""
        if self.compress_radio.isChecked():
            file_filter = "ISO files (*.iso);;All files (*.*)"
        else:
            file_filter = "CSO files (*.cso);;All files (*.*)"

        filename, _ = QFileDialog.getOpenFileName(
            self, "Select input file", "", file_filter
        )
        if filename:
            self.input_edit.setText(filename)
            # Auto-suggest output filename
            if not self.output_edit.text():
                input_path = Path(filename)
                if self.compress_radio.isChecked():
                    output_path = input_path.with_suffix('.cso')
                else:
                    output_path = input_path.with_suffix('.iso')
                self.output_edit.setText(str(output_path))

    def browse_output(self):
        """Browse for output file"""
        if self.compress_radio.isChecked():
            file_filter = "CSO files (*.cso);;All files (*.*)"
        else:
            file_filter = "ISO files (*.iso);;All files (*.*)"

        filename, _ = QFileDialog.getSaveFileName(
            self, "Select output file", "", file_filter
        )
        if filename:
            self.output_edit.setText(filename)

    def log_message(self, message):
        """Add message to log (colored by message type)"""
        low = message.lower()
        if "error" in low or "failed" in low or "invalid" in low:
            color = "#ff6b6b"
        elif "warning" in low or "stop" in low or "cancel" in low:
            color = "#fbbf24"
        elif "success" in low or "completed" in low:
            color = "#4ade80"
        elif "progress" in low:
            color = "#7db0ff"
        else:
            color = "#a9b4cc"
        self.log_text.append(f'<span style="color:{color};">{html.escape(message)}</span>')

    def update_progress(self, value):
        """Update progress bar"""
        self.progress_bar.setValue(value)

    def start_conversion(self):
        """Start conversion process"""
        if self.processing:
            return

        # Validate inputs
        input_file = self.input_edit.text().strip()
        output_file = self.output_edit.text().strip()

        if not input_file:
            QMessageBox.critical(self, "Error", "Please select an input file")
            return

        if not output_file:
            QMessageBox.critical(self, "Error", "Please select an output file")
            return

        if not os.path.isfile(input_file):
            QMessageBox.critical(self, "Error", "Input file does not exist")
            return

        if same_path(input_file, output_file):
            QMessageBox.critical(
                self, "Error",
                "Input and output files must be different.\n\n"
                "Writing the output would destroy the source file."
            )
            return

        # File type validation by signature
        kind = detect_input_kind(input_file)
        if self.compress_radio.isChecked():
            if kind in ("cso", "zso"):
                QMessageBox.critical(
                    self, "Error",
                    "The input file is already a compressed image (CSO/ZSO).\n\n"
                    "Use 'Decompress CSO to ISO' if you want the ISO back."
                )
                return
            if kind != "iso":
                reply = QMessageBox.question(
                    self, "Not a recognized ISO",
                    "The input file has no ISO9660 signature, so it does not look like "
                    "a valid PSP ISO.\n\nCompress it anyway?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
        else:
            if kind == "zso":
                QMessageBox.critical(
                    self, "Error",
                    "ZSO files (LZ4) are not supported.\n\nOnly CSO (deflate) can be decompressed."
                )
                return
            if kind != "cso":
                extra = ("\n\nThis file is an ISO image: use 'Compress ISO to CSO'."
                         if kind == "iso" else "")
                QMessageBox.critical(
                    self, "Error",
                    "The input file is not a valid CSO (CISO signature not found)." + extra
                )
                return

        if os.path.exists(output_file):
            reply = QMessageBox.question(
                self, "Overwrite file?",
                f"The output file already exists:\n{output_file}\n\nDo you want to overwrite it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        # Clear log and reset progress
        self.log_text.clear()
        self.progress_bar.setValue(0)

        # Update UI
        self.processing = True
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.set_controls_enabled(False)

        # Start conversion thread
        self.current_mode = "compress" if self.compress_radio.isChecked() else "decompress"
        self.conversion_thread = ConversionThread(
            self.current_mode, input_file, output_file, self.level_slider.value()
        )
        self.conversion_thread.log_signal.connect(self.log_message)
        self.conversion_thread.progress_signal.connect(self.update_progress)
        self.conversion_thread.finished_signal.connect(self.conversion_completed)
        self.conversion_thread.start()

    def stop_conversion(self):
        """Stop conversion process"""
        if self.processing and self.conversion_thread:
            self.conversion_thread.stop()
            self.stop_button.setEnabled(False)
            self.log_message("Stop requested - please wait...")

    def conversion_completed(self, status):
        """Handle conversion completion (status: success | error | cancelled)"""
        self.processing = False
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.set_controls_enabled(True)

        if status == "success":
            if self.current_mode == "compress":
                text = ("Compression completed successfully!\n\n"
                        "The CSO file follows the CISO v1 format and should be compatible with PSP.")
            else:
                text = "Decompression completed successfully!\n\nThe ISO file has been restored."
            QMessageBox.information(self, "Success", text)
            # Ready for a new operation: clear the input and output paths
            self.input_edit.clear()
            self.output_edit.clear()
        elif status == "cancelled":
            self.progress_bar.setValue(0)
            QMessageBox.information(
                self, "Cancelled",
                "Operation cancelled.\n\nThe incomplete temporary file has been removed.\n"
                "Any existing output file was left untouched."
            )
        else:
            QMessageBox.critical(
                self, "Error",
                "Conversion failed. Check the log for details."
            )

    def closeEvent(self, event):
        """Safe close: stop the thread if a conversion is running"""
        if self.processing and self.conversion_thread:
            reply = QMessageBox.question(
                self, "Conversion in progress",
                "A conversion is still running.\n\nStop it and exit? The incomplete temporary file will be removed.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.conversion_thread.blockSignals(True)
            self.conversion_thread.stop()
            self.conversion_thread.wait()
        event.accept()


def main():
    """Main function"""
    # Windows: an AppUserModelID is needed to show the correct icon in the taskbar
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setWindowIcon(build_app_icon())
    window = CISOApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
