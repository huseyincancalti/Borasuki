# Release validation

Run from the clean public repository on Windows, with Python 3.13, Node.js, PyInstaller, Inno Setup 6 and a compatible NVIDIA GPU. Stage the intended changes before building; the gate binds its evidence to that exact Git tree.

```powershell
./installer/build.ps1 -OutputRoot C:/release-build/beta-new `
  -RuntimeFolder C:/processing-runtime `
  -CompatibilityRuntimeFolder C:/legacy-ffmpeg `
  -EngineCache C:/isolated-test-engine-cache
```

Use a new, dedicated build directory. Both runtime folders must contain `ffmpeg.exe` and `ffprobe.exe`; the processing runtime also needs its installed Python, VapourSynth and TensorRT components. Test at least one legacy FFmpeg without `setts.prescale` and one modern FFmpeg with it. Never copy a user's settings, job database, diagnostic files or videos into fixtures. The optional cache argument copies compiled engines only into isolated test data; these files are never packaged.

The build stops on any failed check:

1. Python unit/contract/failure tests and JavaScript interaction tests.
2. Isolated WebView2 checks, including update notification and manual retry.
3. Frozen bundle external renderer imports, without source checkout imports.
4. Actual installer extraction into a verification-only directory, with no shortcuts or uninstall registry entry; installed files must match the built bundle byte-for-byte.
5. Installed-package MP4 checks on both FFmpeg versions: 24 and 24000/1001 FPS, multiple checkpoints, B-frame display order, exact timestamps, audio and decoded frame hashes.
6. Installed-package Adaptive analysis, TensorRT preparation, real 2× MP4/MKV queue outputs, audio, full decode, and original/processed Preview.
7. Actual frozen EXE/WebView2 startup and normal shutdown using isolated app data.

Only a successful run writes `release-verification.json`, bound to the installer SHA-256 and Git tree. This local report is evidence, not proof of compatibility with every computer or every input. A clean Windows/WebView2-first-install machine and a wider GPU matrix remain separate acceptance checks.

Commit the verified tree. **Publishing requires explicit owner approval each time.** After approval:

```powershell
py -3.13 installer/publish.py C:/release-build/beta-new v1.0.0-beta.N
```

The publisher rejects missing/failed evidence, a changed installer, a dirty or changed source tree, mismatched version/tag, or a private/incorrect origin. It publishes a new pre-release; it does not overwrite old release assets or install the update on a user's machine. Do not bypass the gate with a direct release upload.
