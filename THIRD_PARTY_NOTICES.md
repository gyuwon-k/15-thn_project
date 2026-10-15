# Third-party components

This application includes independently licensed software. Original notices are retained in the release at the paths below. This list describes the main runtime components; each component's original license remains authoritative.

- Electron and Chromium: `desktop/LICENSE` and `desktop/LICENSES.chromium.html`.
- Python and Python libraries, including FastAPI, Uvicorn, Pydantic, PyMySQL, cadquery-ocp / Open CASCADE, NumPy, SciPy, scikit-learn, Pillow, Playwright, imageio-ffmpeg and psutil: `backend/_internal/third_party_licenses/` and notices in their packaged distributions.
- MariaDB: `backend/_internal/runtime/mariadb/COPYING`, `CREDITS` and `THIRDPARTY`. Upstream source and release information: <https://mariadb.org/download/>.
- Playwright's Chromium browser distribution: notices under `backend/_internal/browsers/`.
- React, React DOM, scheduler, Three.js and Lucide: original package notices and package metadata under `third_party_licenses/frontend/`.
- Noto Sans KR: SIL Open Font License 1.1, `frontend/fonts/OFL-NotoSansKR.txt`. Original font: <https://github.com/google/fonts/tree/main/ofl/notosanskr>.
- FFmpeg: the packaged imageio-ffmpeg distribution's notices under `backend/_internal/third_party_licenses/imageio-ffmpeg/`; bundled executable under `backend/_internal/ffmpeg/`. Upstream source: <https://ffmpeg.org/download.html>.

The build records exact component versions in `requirements.lock.txt`, the two npm lock files and the sealed release manifest. Redistribution must retain the relevant license notices and meet the corresponding source-distribution requirements. These files do not replace the individual components' licenses.
