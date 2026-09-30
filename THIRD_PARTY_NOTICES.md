# Third-party components

Borasuki's source code is licensed under [MIT](LICENSE). That license does not replace the licenses of its dependencies. The Windows installer contains the desktop application's Python dependencies and a Microsoft-signed WebView2 Evergreen bootstrapper. The processing packages below are downloaded from their publishers on first setup, verified with pinned SHA-256 hashes, and kept locally.

| Component | Version used | Publisher and license information |
| --- | --- | --- |
| CPython embedded | 3.13.12 | [Python Software Foundation](https://www.python.org/downloads/release/python-31312/) · [PSF License](https://docs.python.org/3/license.html) |
| NumPy | 2.5.1 | [NumPy](https://numpy.org/) · [BSD-3-Clause](https://github.com/numpy/numpy/blob/main/LICENSE.txt) |
| VapourSynth | R79 | [VapourSynth](https://github.com/vapoursynth/vapoursynth/releases/tag/R79) · [LGPL-2.1](https://github.com/vapoursynth/vapoursynth/blob/master/COPYING.LESSER) |
| Real-CUGAN models | v2 release | [vs-mlrt model release](https://github.com/AmusementClub/vs-mlrt/releases/tag/model-20211209) |
| FFMS2 | 5.0 | [FFMS2](https://github.com/FFMS/ffms2/releases/tag/5.0) · GPLv3 license in its archive |
| FFmpeg GPL build | 8.1.3 build | [BtbN FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) · [FFmpeg licensing](https://ffmpeg.org/legal.html) |
| vs-mlrt / VSTRT / TensorRT runtime archive | 15.16 | [vs-mlrt](https://github.com/AmusementClub/vs-mlrt/releases/tag/v15.16) · [project license](https://github.com/AmusementClub/vs-mlrt/blob/master/LICENSE), [NVIDIA TensorRT terms](https://docs.nvidia.com/deeplearning/tensorrt/latest/reference/sla.html) |
| 7zr extractor | 26.03 | [7-Zip](https://www.7-zip.org/download.html) · [licensing FAQ](https://www.7-zip.org/faq.html) |
| WebView2 Evergreen bootstrapper | Microsoft-signed | [Microsoft deployment documentation](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution) |

The desktop bundle also uses [pywebview](https://github.com/r0x0r/pywebview), [pythonnet](https://github.com/pythonnet/pythonnet), [NumPy](https://numpy.org/), and their dependencies. See each project's license and the notices included with its distribution. Borasuki does not modify the downloaded processing archives or upload videos to their publishers.
