# CISO Converter

Desktop tool to convert **ISO to CSO** and **CSO to ISO** (CISO v1, deflate), the
compressed disc image format used by PSP and its emulators. Dark-themed GUI built
with PySide6.

## Features

- Compression with 9 levels (zlib deflate) and full decompression
- Safe writing: the output is written to `<file>.part` and renamed only when the
  operation succeeds, so a cancelled or failed run never destroys an existing file
- Index alignment chosen automatically from the file size (shift 0 for images under
  2 GiB, which covers every PSP ISO), maximizing compatibility
- Input validation by signature: ISO9660 (`CD001`), CISO and ZISO are recognized;
  a CSO/ZSO cannot be fed to the compressor, and a non-CSO cannot be decompressed
- Progress bar, log, Stop button, confirmation before overwriting
- Window sized to the available screen, scrollable on small displays

## Requirements

- Python 3.9 or newer
- PySide6 (installed from `requirements.txt`)

Tested with PySide6 6.12.

## Installation and usage

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate     Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

1. Choose the mode (Compress or Decompress).
2. Select the input file; the output name is suggested automatically.
3. For compression, pick a level (1 = faster, 9 = smaller file).
4. Press **Start**. When the operation completes the paths are cleared, ready for
   the next one.

## Format notes

- CISO v1, block size 2048 bytes, raw deflate blocks (no zlib header)
- Blocks that do not compress are stored plain (bit 31 of the index entry)
- Index shift: 0, 1 or 2 depending on the size; the maximum output size is 8 GiB
- Not supported: ZSO (LZ4) and CSO v2

The compression level only changes how hard the compressor works: the result is a
standard deflate stream, so any decoder reads it the same way regardless of level.

## Limitations

- Compression is single-threaded
- Compression ratio is that of zlib; encoders such as zopfli or 7-Zip can save a few
  more percent at a much higher time cost

## Legal notice

Use this tool only with disc images you are legally entitled to have. Do not add game
images (ISO/CSO/ZSO) to the repository.

## Credits

Created by **Raffaele Catapano** - [github.com/MrGodLink](https://github.com/MrGodLink)

If you reuse, modify or redistribute this project, please keep the copyright notice and
credit the original author, as required by the MIT License.

### Other projects by the author

- [RcFux](https://github.com/MrGodLink/RuFux) - graphical utility (PySide6) to create bootable USB drives on Linux.

## License

The source code is released under the [MIT License](LICENSE).

The program depends on [PySide6](https://doc.qt.io/qtforpython/), licensed under the
LGPLv3 (or a Qt commercial license). It is installed separately via `pip`. If you
distribute a bundled executable (for example built with PyInstaller), you must also
comply with the LGPL terms for PySide6 and Qt.
