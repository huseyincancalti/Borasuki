# Borasuki 1.0.0 beta 3

A compatibility and release-validation update. This is a pre-release for testing, not stable 1.0.

## Changes

- Fix MP4 output on legacy FFmpeg (including 7.1.1), where `setts.prescale` does not exist. Detect executable capabilities instead of assuming a version. Preserve CFR timing, B-frame display order, frame content and audio without re-encoding the upscaled video.
- Check required MP4 timestamp options before an expensive render. Keep strict output verification.
- Add nonblocking update checks on startup, a new-version notice and a manual check in Settings. Compare beta versions numerically. Network failure is recoverable and does not block rendering. Nothing is downloaded or installed automatically.
- Fix the packaged Explorer context-menu command to launch Borasuki.exe rather than a missing Python launcher.
- Make source, UI, installed-package, FFmpeg compatibility and GPU output checks mandatory in the release workflow. Publication rejects missing evidence, changed installer hashes and changed source trees.

## Download and update

Download `Borasuki-Setup-1.0.0-beta.3-win64.exe` below. Close Borasuki and install over beta 1/2. Settings, history and caches remain in `%LOCALAPPDATA%\KaraKedi\Borasuki`; uninstalling first is unnecessary. Beta 1/2 do not have update notifications, so upgrading to this version is manual once.

Windows 10/11 x64, a compatible NVIDIA GPU/driver and at least 7 GB free disk space are required. Initial setup downloads approximately 3 GB of hash-verified processing components; first-time TensorRT preparation may take minutes.

## Release checks

The release gate must pass Python unit/contract/failure tests, JavaScript and WebView2 checks, isolated external renderer imports and installer extraction. Installed files must match the built bundle byte-for-byte. FFmpeg 7.1.1 and the app-owned 8.1.3 runtime are checked at 24 and 24000/1001 FPS with multiple segments, B-frames, audio, exact timestamps and decoded frame hash equality. The installed package is checked with real Adaptive analysis, TensorRT preparation, short 2× MP4/MKV queue outputs, full decode, original/processed Preview and frozen EXE startup/normal shutdown.

All fixtures are synthetic and isolated. No user videos, history, diagnostic logs or engine caches are included in the installer or public repository.

## Known limitations

Validation is on one Windows 11 / RTX 3050 Laptop GPU system, not a fresh Windows machine or broad GPU matrix. Compatible SDR/progressive/CFR inputs only. Adaptive applies one grade per video, not scene-by-scene correction; visual gains depend on source quality. The installer is not publisher-signed. Update checks require internet access and can fail because of connectivity or GitHub rate limits; Settings offers manual retry. The update request goes to public GitHub and does not include video paths, settings, jobs or diagnostics.
