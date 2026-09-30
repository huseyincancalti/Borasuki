# Borasuki 1.0.0 beta 1

This is a public pre-release for testing, not a stable 1.0 release.

Borasuki processes anime and animation videos with 2× Real-CUGAN upscaling on NVIDIA GPUs through TensorRT. It includes independent denoise and color controls, short before/after previews, batch import, a persistent job queue, and MP4/MKV output.

Download `Borasuki-Setup-1.0.0-beta.1-win64.exe` below. Windows 10/11 x64, a compatible NVIDIA GPU and driver, internet access, and at least 7 GB of free disk space are required. The first in-app setup downloads approximately 3 GB of processing components directly from their publishers and verifies pinned SHA-256 hashes. First-time TensorRT engine preparation for a new video/GPU/settings combination can take several minutes; it happens before the job enters the queue.

Installer SHA-256: `71BEF4E8C705617F12FDB2C4BE78EF75BF6E74508B2FC4B84B1649677EE2AB27`

Videos, settings, history, logs, and caches stay local. Fresh installations start without someone else's data; uninstalling preserves your own app data in `%LOCALAPPDATA%\KaraKedi\Borasuki`. Nothing is uploaded automatically. Please share diagnostics only if you choose to.

Known limits: compatible SDR/progressive/constant-frame-rate input only; visual gains vary by source; Adaptive color currently applies one conservative grade per video, not scene-by-scene correction. The installer has no publisher code signature, so Windows may display a reputation warning. The full first-run path was tested on one Windows 11 NVIDIA system; other GPUs and a clean Windows machine still need tester feedback.
