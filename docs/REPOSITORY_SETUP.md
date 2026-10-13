# GitHub 소스 저장소 안내

이 저장소는 PILOT Studio의 개발 소스와 구현 기준 문서를 보관합니다. 통합설계 v1.6, ASAP 구현설계 v0.4, 앱 버전 1.0.0을 기준으로 합니다. 최종통합 문서는 `PILOT_인터페이스_최종통합본_ASAP반영.md`이며 `SPEC.md`에도 같은 기준을 보관합니다.

## 포함 범위

Python 백엔드·ASAP 어댑터·Windows 독립 창, React/Three.js 프런트엔드, 시험 소스, 의존성 lockfile, 설치파일 빌드 정의와 설계·사용 문서를 포함합니다.

기업 제공 CAD·Assembly Flow, 사용자 프로젝트·DB·인증 설정, 시험 실행 데이터·로그, Python 환경·node_modules, 사전학습 가중치·네이티브 바이너리·배포 설치파일은 포함하지 않습니다. 프로그램 소스의 일부 시험·재빌드 스크립트는 기존 개발 환경의 별도 런타임 또는 외부 검증 형상을 사용합니다.

## 개발 환경 준비

1. Windows x64에서 Python 3.12 개발 환경을 준비하고 `requirements.lock.txt`의 의존성을 설치합니다.
2. `frontend`에서 `npm ci`와 `npm run build`로 프런트엔드를 빌드합니다.
3. 전용 MariaDB 런타임과 별도의 Python 3.11 ASAP 환경을 준비합니다. ASAP의 고정 소스, 검증된 RedMax 네이티브 빌드, 공식 사전학습 가중치가 모두 필요합니다. 엔진 의존성은 `asap_service/engine/requirements.windows-tested.lock.txt`를 참조합니다.
4. 개발용 엔진 경로는 `PILOT_ASAP_ENGINE_PYTHON`, `PILOT_ASAP_ROOT`, `PILOT_ASAP_WEIGHTS`, `PILOT_ASAP_WORKSPACE`로 지정할 수 있습니다. 네이티브 작업 경로는 영문 경로로 준비합니다. 프로그램·사용자 자료 위치는 `PILOT_RUNTIME_DIR`, `PILOT_DATA_DIR`로 분리합니다. 인증값은 저장소에 기록하지 않습니다.
5. 영상 출력은 프로젝트에 준비한 Playwright Chromium과 FFmpeg가 필요합니다. Windows 독립 창은 WebView2를 사용합니다.

`scripts/stage_runtime.py`, `scripts/build_release.ps1`, `packaging/pilot-final.spec`는 기존 Windows 개발 환경의 로컬 런타임을 모아 오프라인 배포본을 만드는 정의입니다. 새 PC에서는 해당 로컬 경로와 구성 요소를 준비해야 합니다. 저장소를 복제했다는 것만으로 네이티브 엔진·설치파일·기업 검증 데이터가 함께 설치되지는 않습니다.

## 검증 상태

`IMPLEMENTATION_STATUS.md`는 완료된 구현과 실제 제품의 인수 범위를 구분합니다. HVJB의 전체 전처리는 확인했지만 전체 조립순서 성공은 아직 확보하지 않았습니다. 진행 중인 HVJB 문제 분석을 완료된 제품 검증으로 해석하지 않습니다. 이후 수정은 새로운 커밋으로 관리합니다.

이 소스 업로드는 원본 작업 폴더를 변경하지 않는 별도 복사본에서 수행했습니다.
