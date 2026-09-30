<p align="center">
  <img src="frontend/assets/img/logo.png" alt="Borasuki logo" width="128">
</p>

# Borasuki

Borasuki is a Windows desktop app for upscaling anime and animation videos. It combines 2× Real-CUGAN processing, optional denoise, color controls, short before/after previews, and a persistent job queue.

## Download

Download the Windows installer from the [Releases page](https://github.com/huseyincancalti/Borasuki/releases). Pre-releases are for testing, not a stable v1.0 release. Windows 10/11 x64, a compatible NVIDIA GPU and driver, an internet connection, and at least 7 GB of free space are required. There is no CPU fallback.

The installer adds the desktop app and installs Microsoft WebView2 if needed. On first launch, open the setup screen and install the processing components: approximately 3 GB are downloaded directly from their publishers and checked against pinned SHA-256 hashes. Downloads can resume after an interruption. The first processing-engine preparation for a chosen video/GPU/settings combination may still take several minutes; it completes before that job can enter the queue.

To run from source, install Python 3.13 dependencies with `py -3.13 -m pip install -e .`, then run `py -3.13 main.py`. A compatible local processing runtime is still required.

## Architecture

- **Desktop UI:** HTML, CSS, and JavaScript in a local WebView2 window, connected to a Python service through pywebview.
- **Video pipeline:** FFMS2 decodes frames; VapourSynth and Real-CUGAN perform 2× processing through TensorRT on NVIDIA GPUs; FFmpeg creates the output video. MP4 and MKV are supported.
- **Color:** Adaptive analyzes samples from the video and applies one conservative grade to the whole video. Reference, Original, and manual controls are also available. Scene-by-scene color correction is not yet part of the production pipeline.
- **Jobs:** SQLite stores the queue and presets; settings are stored as JSON. Rendering uses recoverable segments, with pause, resume, cancellation, and short before/after previews.

Source videos stay in their original folders, and output videos go to the folder you choose. Job history, settings, logs, and caches stay on your computer in `%LOCALAPPDATA%\KaraKedi\Borasuki`. A new installation starts empty and does not import an older local workspace. Uninstalling the app does not delete this local data. Borasuki does not upload your media or job history; you may inspect, share, or delete your local files yourself.

Current limitations: only compatible SDR/progressive/constant-frame-rate sources are accepted, visual quality varies by source, and scene-by-scene color correction is not yet available. See [Third-party notices](THIRD_PARTY_NOTICES.md) for the components downloaded during setup.

---

Hüseyin Can ÇALTI · [karakedidub.com](https://karakedidub.com) · [hsyncalti2@gmail.com](mailto:hsyncalti2@gmail.com)
