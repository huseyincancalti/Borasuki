# Borasuki 1.0.0 beta 2

This pre-release fixes blocking defects in beta 1. It is not a stable 1.0 release.

## Fixes

- Include the Python helper files required by the external VSPipe renderer. Beta 1 could open and pass its runtime check but failed analysis and engine preparation with `No module named 'borasuki.storage'`.
- Preserve exact constant-frame-rate timing when combining MKV checkpoints into MP4, including B-frame ordering and fractional frame rates. Output verification is not weakened; video is stream-copied, not re-encoded.
- Add an isolated external-import gate to the installer build and an opt-in installed-package processing regression.

## Download and update

Download `Borasuki-Setup-1.0.0-beta.2-win64.exe` below. Install it over beta 1 with Borasuki closed. Your settings, history and caches in `%LOCALAPPDATA%\KaraKedi\Borasuki` are preserved; uninstalling first is unnecessary.

SHA-256: `81691165EE7BFD061483148FA9BF1D0DCF4D61929D50056D0EB95CDBC75BB478`

Windows 10/11 x64, a compatible NVIDIA GPU and driver, internet access for initial setup, and at least 7 GB free disk space are required. Initial setup downloads approximately 3 GB of pinned, hash-verified processing components. First-time TensorRT engine preparation can take several minutes.

## Verified

- 12 targeted packaging/encoding unit tests passed. The packaging regression fails against the beta 1 specification.
- The final installer upgraded the existing installation without changing local settings/history/log files.
- Using the installed package and app-owned runtime, without source-package imports: Adaptive analysis, denoise-2 TensorRT preparation, Queue completion, four-frame 2× MP4 output with audio, and full output decode passed. Original/enhanced one-frame Preview decode also passed.
- Two-segment MP4 checks at 24 and 24000/1001 FPS passed: exact timestamps, all 18 frames, B-frame order, audio and identical decoded frame hashes.
- Installed frozen EXE/WebView2 startup and normal shutdown passed. The processing regression exercised installed backend files; a full automated GUI-click workflow was not performed.

## Privacy and limitations

Videos, settings, history, logs and caches stay local. The installer contains no test videos, personal job data or engine caches. Diagnostics are not uploaded automatically.

Tests were run on one Windows 11 / RTX 3050 Laptop GPU system, not a fresh Windows machine or a broad GPU matrix. Compatible SDR/progressive/CFR input only; Adaptive uses one grade per video, and visual gains vary by source. The installer is not publisher-signed. Explorer context-menu launching remains a known packaged-build limitation; open the app and use its video picker or drag/drop instead.
