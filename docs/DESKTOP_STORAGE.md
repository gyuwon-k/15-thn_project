# PILOT Studio 설치·운영 저장 구조

기본 실행은 `PILOT-Studio.exe`의 독립 WebView2 프로그램 창이다. 설치 파일은 Python 3.12 앱 런타임, OCCT, 영상용 Chromium·FFmpeg, 사전학습 가중치와 RedMax를 포함한 Python 3.11 ASAP 런타임, 전용 MariaDB 실행 파일 및 오프라인 WebView2 설치 구성 요소를 포함한다. 사용자 PC에 Python이나 Node.js 개발 환경을 설치하도록 요구하지 않는다.

설치 위치는 `%LOCALAPPDATA%/Programs/PILOT Studio`, 기본 DATA는 `%LOCALAPPDATA%/PILOT Studio/data`다. 프로그램 폴더에는 사용자 CAD·프로젝트·DB를 저장하지 않는다. 설치 해제나 프로그램 파일 교체는 DATA를 삭제하지 않는다. 기본 실행에서 복원 저장소를 선택하면 새 창 준비 성공 뒤 사용자별 active-storage.json에 검증된 선택을 기록하며 다음 바로가기 실행도 같은 복원 저장소를 연다. 임의 경로나 검증되지 않은 복원 결과는 선택하지 않는다. `--data-dir` 또는 명시적 개발용 환경변수는 이번 실행의 별도 저장소 선택으로 처리하고 기본 선택을 바꾸지 않는다. 같은 DATA를 두 앱이 동시에 열면 `STORAGE_LOCKED`로 거절한다.

MariaDB는 운영 저장소다. 프로그램별 DATA/database와 보호 연결 설정을 만들며 127.0.0.1의 가용 포트에서 앱 수명 동안 실행한다. Windows 서비스나 기존 MariaDB 서버·기존 DB를 수정하지 않는다. 앱 종료는 전용 서버에 정상 SHUTDOWN을 요청한다. 이전 앱의 강제 종료 뒤 DATA 잠금을 획득한 경우에만 실행 파일 및 datadir가 정확히 일치하는 잔여 프로세스를 복구한다. SQLite를 자동 대체 운영 DB로 사용하지 않는다.

자격증명은 Windows 사용자별 DPAPI로 보호한다. 시작용 임시 SQL 파일은 성공과 실패 후 삭제한다. 암호·계정 보호 파일·활성 MariaDB datadir는 백업에 포함하지 않는다. 시작 실패는 DB 파일을 보존하고 명시적 오류와 logs 진단을 제공한다. MariaDB 런타임은 12.3.2, 앱 스키마는 1이며 별도 버전이다. 스키마 정의 해시가 이미 적용된 해시와 다르면 `MIGRATION_CHANGED`, 더 새 스키마는 `SCHEMA_TOO_NEW`로 쓰기를 차단한다.

운영 업무 데이터는 projects/snapshots/feedback/jobs/audit/request_log에 저장한다. 프로젝트 및 불변 스냅샷의 정규화 JSON 본문에 공정·공구·규칙·계획·CAD/단위 개정·ASAP 참조를 보존하고 외래키 및 MariaDB 트랜잭션으로 소속을 보호한다. schema_migration/storage_binding은 운영 테이블이다. 팀원의 개별 저장 테스트에서 보고한 19개 업무 테이블 구현을 이 앱의 실제 스키마로 표시하지 않는다. 업무 개체의 JSON 계약과 앱 버전이 기준이다.

`transaction_scope()`는 사용자 명령의 상태 변경과 request_log 응답을 같은 트랜잭션에 저장한다. 요청 ID 재사용 시 같은 입력 해시는 저장한 응답을 반환하고 다른 해시는 `IDEMPOTENCY_CONFLICT`로 거절한다. 불변 스냅샷은 저장한 SHA-256을 조회 때 다시 검증한다.

전체 백업 `pilot-backup/2`는 MariaDB 업무 테이블의 SQLite 논리 스냅샷과 DATA 관리 파일의 ZIP이다. SQLite는 백업 호환 형식이다. source_backend=mariadb/snapshot_format=sqlite3를 명시한다. CAD 원본, 캐시, ASAP 세션·단위 개정·경로·진단·실행 증적, 영상, 학습 파일을 포함하며 실제 작업이 실행 중이면 일관 백업을 거절한다. 파일 크기·SHA-256·전체 ZIP 항목·행 수·불변 스냅샷 해시를 검증한다. 단일 업로드·해제 총량은 16GB, 항목 200,000개, manifest 16MB로 제한한다. 큰 파일은 스트림 처리한다.

복원은 현재 DATA를 덮어쓰지 않는다. ZIP 경로 탈출·중복·심볼릭 링크·변조를 먼저 거절하고 새로운 `restored/restore-*` DATA와 독립 MariaDB datadir에 이관한다. 모든 논리 행을 다시 읽어 행 해시를 비교한 뒤 verified를 반환한다. 보호 자격증명은 새 저장소의 현재 사용자로 새로 만든다. 과거 절대경로는 path_map.json과 `desktop_storage.resolve_path()`로 새 DATA 안에서 읽으며 불변 원문 JSON과 스냅샷 해시는 바꾸지 않는다. 활성화는 자동으로 하지 않으며 기존 저장소를 보존한다. 독립 창에서는 검증된 복원 결과의 `복원 저장소로 다시 실행` 버튼으로 새 창을 연다. 활성 작업이 없어야 하며 알려진 복원 ID만 받는다. 새 창과 새 백엔드가 준비된 뒤 기존 창을 종료한다. 브라우저 시험 환경에서는 네이티브 전환 버튼을 제공하지 않는다. 스키마가 다른 백업의 자동 변환이나 MariaDB 물리 시점 복구 기능으로 표시하지 않는다.

개발용 `--no-browser --port` 서버 인수는 자동 시험용이다. 배포의 기본 실행 경로는 독립 창이다. 설치 패키지는 아직 코드 서명되지 않았으며 Windows가 확인되지 않은 게시자 안내를 표시할 수 있다. 완전히 새 Windows PC·다른 Windows 사용자·전원 차단·용량 부족·DB 버전 업그레이드는 별도 환경 인수 범위다.

빌드: `scripts/build_release.ps1`. 설치 패키지 경로는 `dist/PILOT-Studio-Setup-1.0.0.exe`, 실행 파일 경로는 `dist/PILOT-Studio/PILOT-Studio.exe`다. 기본 DATA를 포함한 전체 디렉터리를 복사하는 방식으로 배포하지 않는다.
