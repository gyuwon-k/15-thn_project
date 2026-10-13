# PILOT Studio 외부 구성요소 고지

작성 기준: 2026-10-12, Windows x64 개발 환경 및 설치형 PILOT Studio 1.0.0. 이 프로그램은 Open CASCADE Technology 소프트웨어가 제공하는 기능을 사용합니다. 원저작자의 저작권·라이선스 고지는 아래 원문과 함께 유지합니다.

추가 구성 요소: pywebview 6.2.1(BSD 계열), pythonnet 3.2.1(MIT), PyMySQL 1.2.3(MIT), MariaDB Community Server 12.3.2(GPLv2 및 포함 구성 요소), Microsoft WebView2 런타임(공급자의 재배포 조건), CPython 3.11 기반 독립 ASAP 실행 환경, PyTorch 2.6.0+cpu 및 PyTorch Geometric 2.7.0, ASAP 공식 소스·사전학습 가중치·검증된 RedMax 네이티브 빌드가 포함됩니다. 실제 라이선스 원문은 Python 배포판별 고지, runtime/mariadb/COPYING 및 THIRDPARTY, runtime/asap/ASAP/LICENSE, runtime/asap/python의 배포판 고지에 보존합니다. 새 서버 서비스는 설치하지 않으며 MariaDB는 사용자 DATA 전용 프로세스입니다.

설치 프로그램은 Inno Setup 6.4.3으로 작성합니다. GPL/LGPL 구성 요소의 대응 소스 제공 및 Microsoft 구성 요소의 재배포 조건은 외부 상용 배포 전에 적용해야 합니다. 패키지는 디지털 코드 서명되지 않았으므로 Windows가 게시자를 미확인으로 표시할 수 있습니다.

## 원문과 버전 기록 위치

실행 패키지의 `_internal/third_party_licenses/`에는 설치된 Python 배포판과 프런트엔드 런타임 라이브러리에서 수집한 LICENSE, COPYING, NOTICE 원문을 보존합니다. `versions.json`은 빌드 당시 Python 패키지와 프런트엔드 런타임의 실제 버전 목록입니다. Python 목록에는 빌드·시험 도구도 포함되며, 목록에 있다는 이유만으로 해당 도구의 실행 코드가 모두 번들에 들어간 것은 아닙니다.

개발 환경에서는 `.venv/Lib/site-packages/`의 해당 배포판 `*.dist-info/` 또는 패키지 경로와 `frontend/node_modules/<패키지>/LICENSE*`가 수집 원본입니다. 재빌드하면 `pilot.spec`이 설치본에서 목록과 고지 원문을 다시 수집합니다.

## 런타임 구성요소

- **CPython 3.12.14**: Python 라이선스 및 포함 구성요소 고지. 패키지 원문: `_internal/third_party_licenses/python/LICENSE.txt`.
- **FastAPI 0.142.2**, **Pydantic 2.13.5**: MIT. 각 배포판의 `licenses/LICENSE` 원문을 수집합니다.
- **Uvicorn 0.54.0**: BSD-3-Clause. `uvicorn-0.54.0.dist-info/licenses/LICENSE.md`.
- **python-multipart 0.0.32**: Apache-2.0. 업로드 파서입니다.
- **NumPy 2.5.3**: 배포판 표기는 BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0입니다. 수치 계산에 포함된 별도 라이선스도 `numpy` 고지 폴더에 보존합니다.
- **SciPy 1.18.1**: 배포판 `LICENSE.txt`와 내부 Qhull, FFT 등 개별 고지를 보존합니다. 주 라이선스는 BSD 계열이며 모든 내부 구성요소를 하나의 라이선스로 통칭하지 않습니다.
- **scikit-learn 1.9.1**, **joblib 1.6.0**: BSD-3-Clause. `COPYING`과 `LICENSE.txt` 및 포함 구성요소 고지를 수집합니다.
- **Pillow 12.3.0**: MIT-CMU. `pillow` 배포판 라이선스 원문을 수집합니다.
- **cadquery-ocp 8.0.1.0.0 Python 바인딩**: Apache-2.0. 원문은 `cadquery_ocp/LICENSE`입니다. CAD 엔진 자체의 라이선스는 다음 항목과 구분합니다.
- **Open CASCADE Technology 엔진**: LGPL-2.1과 OCCT exception 1.0. 실제 바인딩이 보고하는 엔진 버전 문자열은 `OCCT/OCP 8.0.1.0`입니다. 원문은 `_internal/third_party_licenses/upstream/OCCT-LICENSE_LGPL_21.txt`, `OCCT-LGPL_EXCEPTION.txt`에 보존합니다. [OCCT 공식 소스 및 라이선스](https://github.com/Open-Cascade-SAS/OCCT), [OCCT 예외 원문](https://github.com/Open-Cascade-SAS/OCCT/blob/master/OCCT_LGPL_EXCEPTION.txt).
- **VTK 9.6.2**: BSD 계열 배포판. OCCT wheel이 요구하는 DLL을 함께 배포하며 `vtk-9.6.2.dist-info/licenses/LICENSE`를 보존합니다.
- **Playwright 1.63.0**: Apache-2.0. `_internal/playwright/driver/package/LICENSE`, `NOTICE`, `ThirdPartyNotices.txt`와 별도 라이선스 모음을 보존합니다.
- **Playwright 동봉 Node.js v24.21.0**: Node.js 및 포함 구성요소의 개별 라이선스. `_internal/playwright/driver/LICENSE`가 실제 바이너리에 동봉된 전체 고지입니다. 대상 사용자가 Node.js를 별도 설치할 필요는 없습니다.
- **Chromium 153.0.8010.12 / Playwright revision 1243**: Chromium과 다수 포함 구성요소의 개별 라이선스. `_internal/browsers/chromium_headless_shell-1243/chrome-headless-shell-win64/LICENSE.headless_shell`을 번들 안에 보존합니다. 브라우저는 MP4의 공통 장면 렌더링에 사용합니다.
- **imageio-ffmpeg 0.6.0 Python 래퍼**: BSD-2-Clause. 래퍼 라이선스는 실제 FFmpeg 실행 파일의 라이선스와 다릅니다.
- **FFmpeg 7.1 essentials build, www.gyan.dev**: 번들 실행 파일의 `-L` 출력은 **GPL version 3 or later**입니다. `--enable-gpl --enable-version3 --enable-libx264`가 활성화된 실제 바이너리를 별도 프로세스로 실행합니다. `_internal/third_party_licenses/ffmpeg-build-and-license.txt`에 바이너리의 직접 출력, `upstream/FFmpeg-GPL-3.0.txt`에 해당 원문을 보존합니다. [FFmpeg 7.1 공식 소스](https://github.com/FFmpeg/FFmpeg/tree/n7.1), [GPLv3 원문](https://github.com/FFmpeg/FFmpeg/blob/n7.1/COPYING.GPLv3), [바이너리 공급처](https://www.gyan.dev/ffmpeg/builds/). 이 초기 패키지는 FFmpeg·OCCT의 전체 대응 소스 배포본을 포함하지 않습니다. 외부 최종 배포 형태에 따른 대응 소스 제공 절차는 별도로 완결해야 합니다.

## 프런트엔드 런타임

- **React 19.3.0**, **React DOM 19.3.0**, **scheduler 0.28.0**: MIT.
- **Three.js 0.180.0**: MIT.
- **lucide-react 0.544.0**: ISC 및 원본 라이선스에 기록된 기여자 고지.

각 원문은 `_internal/third_party_licenses/frontend/<패키지>/LICENSE*`에 들어갑니다. 실제 버전은 프런트엔드 lockfile과 함께 기록합니다.

## 빌드 도구

- **PyInstaller 6.22.3**: GPLv2-or-later와 애플리케이션 배포를 허용하는 특별 예외. `pyinstaller-6.22.3.dist-info/licenses/COPYING.txt` 원문을 보존합니다.
- **Vite 7.3.6**: MIT, **TypeScript 5.9.3**: Apache-2.0. 프런트엔드 소스의 개발·빌드 도구이며 별도 실행 환경으로 최종 사용자에게 설치하도록 요구하지 않습니다.

Windows 기본 글꼴을 사용하며 별도 상용 폰트 파일은 복사하지 않았습니다. 모델·기업 제공 자료의 권리는 소프트웨어 라이브러리 라이선스와 별개입니다. 이 문서는 사용한 구성요소와 고지 파일의 위치를 기록하며, 개별 프로젝트 소스의 배포 라이선스를 새로 정하지 않습니다.
