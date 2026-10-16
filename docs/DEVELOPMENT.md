# PILOT Studio

STEP 등록, 완제품·부품 대응, 모델 검토, 조립순서 추천·검증, 순서 편집·재검증, 확정 이력, 템플릿 영상 출력과 평가 데이터 축적을 제공하는 Windows 데스크톱 프로그램입니다. Electron 화면과 독립 Python 백엔드를 사용하며, 사용자 데이터는 앱 전용 MariaDB와 관리 파일 저장소에 보관합니다.

## 2.4.1 영상 GPU 가속

영상용 Chromium은 하드웨어 WebGL 경로를 우선 사용하고, MP4 인코딩은 NVIDIA NVENC·Intel Quick Sync·AMD AMF를 실제로 시험해 자동 선택합니다. GPU 인코더가 없거나 작동하지 않으면 CPU 호환 모드로 처리합니다. 무손실 프레임 캡처의 중복 대기를 줄이며, 전체 MP4 디코딩과 프레임 수 검사는 유지합니다. 영상 목록과 출력 기록에 실제 인코더·렌더러·생성 시간을 남깁니다.

## 형상·조립순서 적용 범위

경미한 CAD 곡선 표현 불일치는 원형을 보존하고 실측한 오차만 메타데이터에 반영합니다. 기존 오류 형상은 생성·검증 시 자동 재평가하며, 일반·정밀 검사 및 저장 후 보존 검사를 통과해야 분석에 사용합니다. 계산 실패·실제 관통·지지 미확인은 계속 보류합니다. 형상 검토자 입력으로 검사를 우회하지 않습니다.

완제품을 정상 조립된 기준 상태로 두고 분해 중 새로 생기는 간섭을 검사합니다. 남아 있는 부품을 통과하는 분해는 허용하지 않으며, 지지·고정 연결을 확인한 뒤 역순으로 조립하고 전체 순서를 다시 검증합니다. 정확한 CAD 기준 접촉 영역만 구분하며 부품 쌍 전체를 충돌 검사에서 제외하지 않습니다.

순서 확정에는 최신 조립성 검증과 완전한 경로만 필요합니다. 토크·주의사항·작업정보 검토는 선택 입력입니다. 기본 분석 시간은 생성과 재검증 각각 30분이며 이전 기본 설정은 현재 프로젝트 사본에서 전환합니다. 사용자 취소와 미완료 결과의 기존 계획 보존을 지원합니다.

상세 기능 계약은 구현설계 v0.12, 실제 시험 결과와 제한은 2.4.1 배포 검증 보고서를 참고하세요. 지지 검사는 공중에 뜨는 순서를 막기 위한 형상·결합 관계 검사이며 강도·마찰·전도 해석을 포함하지 않습니다.

## 실행과 데이터

전달받은 설치 파일 `PILOT-Studio-<버전>-Setup.exe`를 실행해 사용자 계정 단위로 설치합니다. 개발 빌드 도구가 생성하는 원래 파일 이름은 `PILOT-Studio-Setup-<버전>.exe`이며 내용은 같습니다. 설치 후 `PILOT Studio` 바로가기를 사용합니다. 업데이트도 새 설치 파일을 실행하는 방식입니다. 설치기는 검증된 새 릴리스를 별도 폴더에 준비하고, 기존 데이터 백업과 실행 전 검사를 통과한 경우에만 활성 버전을 전환합니다. 실패하면 이전 활성 버전과 데이터를 보존합니다.

기본 사용자 데이터 위치는 `%LOCALAPPDATA%\PILOT Studio\data-v2`이며 설치 폴더와 분리됩니다. 운영 데이터 위치를 바꿀 때는 `PILOT_DATA_DIR` 환경변수로 지정할 수 있습니다. MariaDB 시작 실패를 빈 DB나 다른 저장 방식으로 숨기지 않습니다. 설정 화면에서 백업을 만들고, 복원은 별도 저장소에서 검증한 후 재시작하여 적용합니다. 이전 세대 프로그램의 DB는 자동으로 덮어쓰거나 변환하지 않습니다.

영상용 브라우저는 첫 사용 시 사용자 데이터의 `runtime/browsers/<빌드ID>`에 검증된 사본을 준비하여 실행합니다. GPU 프로세스가 만드는 로그와 임시 파일이 설치 원본의 무결성 검증을 방해하지 않도록 하기 위함입니다. 재사용할 때도 배포 파일의 해시를 확인하며, 캐시는 프로젝트 백업 대상에 포함하지 않습니다.

## 개발 환경

Windows x64, Python 3.12, Node.js와 npm, Inno Setup 6을 준비합니다. 프로젝트 루트에서 가상환경을 생성한 후 다음을 실행합니다.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.venv\Scripts\python.exe -m pip install -r requirements-test.txt
npm --prefix frontend ci
npm --prefix desktop ci
```

MariaDB Windows x64 배포본과 해당 Playwright 버전의 Chromium 브라우저 캐시를 준비합니다. 런타임을 프로젝트에 복사하는 명령은 다음과 같습니다. 경로 인수는 각 개발 환경에 맞게 지정합니다.

```powershell
.venv\Scripts\python.exe scripts/stage_runtime.py --mariadb <MariaDB배포폴더> --browsers <Playwright브라우저캐시>
```

소스 데스크톱 실행 시 Python 경로를 명시할 수 있습니다. 먼저 프런트엔드를 빌드한 후 앱을 시작합니다.

```powershell
$env:PILOT_PYTHON = (Resolve-Path .venv\Scripts\python.exe).Path
$env:PILOT_RESOURCE_DIR = (Resolve-Path packaging/resources).Path
$env:PLAYWRIGHT_BROWSERS_PATH = (Resolve-Path packaging/resources/browsers).Path
npm --prefix frontend run build
npm --prefix desktop start
```

소스 실행은 운영 데이터를 대상으로 하지 않도록 `PILOT_DATA_DIR`에 개발용 폴더를 지정하는 것이 좋습니다.

## 검증과 배포

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests
.venv\Scripts\python.exe scripts/build_release.py --python .venv/Scripts/python.exe --inno <ISCC.exe경로> --skip-stage
```

저장소 통합 검증은 테스트용 MariaDB 프로세스와 로컬 포트를 사용합니다. 테스트 데이터는 프로젝트의 `work/` 아래에 따로 생성합니다. 완제품의 원본 STEP 파일은 배포 소스에 포함되지 않으며 별도로 등록해야 합니다.

`release.json`을 버전의 단일 기준으로 사용합니다. 변경된 프로그램을 배포할 때 `app_version`, `build_id`, 각 구성 요소의 `*_build_id`, 적용 알고리즘 버전과 명세 버전을 함께 갱신합니다. 같은 빌드 ID로 이미 완성된 릴리스를 다시 덮어쓸 수 없습니다. 빌드 도구는 프런트엔드·백엔드·런처의 식별자와 설치기의 버전을 검증하며 모든 배포 파일의 해시를 기록합니다.

배포 결과는 `dist/PILOT-Studio-Setup-<버전>.exe`와 `dist/<빌드ID>/`에 생성됩니다. 패키지는 Python, CAD 라이브러리, MariaDB, 브라우저와 영상 인코더를 포함하므로 사용자 PC에 개발 도구를 설치할 필요가 없습니다. 재배포 시 `THIRD_PARTY_NOTICES.md`와 동봉 라이선스를 보존해야 합니다.

설치기 압축은 Windows 경로 길이 제한을 피하도록 프로젝트 안에 짧은 임시 디렉터리 연결을 만들고, 압축 후 연결만 제거합니다. 이미 봉인된 패키지의 설치기만 다시 만들 때는 같은 빌드 명령에 `--installer-only`를 지정합니다. 패키지 내용과 해시 검증은 다시 수행하며 실행 파일을 재빌드하지 않습니다.

릴리스 검증 도구 `scripts/build_release_smoke.py`는 동결된 백엔드에서 간단한 실제 STEP의 순서 생성 경로를 실행합니다. `scripts/build_release_verify_update.py`는 서로 다른 두 네이티브 빌드를 사용하여 독립 시험 폴더에서 데이터 보존과 실패한 업데이트의 롤백을 검사합니다. 이는 현장 제품의 물리적 조립 가능성을 대신 확인하지 않습니다.
