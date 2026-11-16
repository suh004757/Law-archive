# Law archive

대한민국 법령·행정규칙·자치법규·판례를 로컬 SQLite FTS5로 검색하는 도구입니다.
Legalize 미러를 색인하고 법제처 공식 API로 현행 법령을 대조·보완합니다.

현재는 DB 구축·검색·공식본 보완 CLI를 제공합니다. MCP 통합 서버와 자동 동기화는 아직 포함하지 않습니다.
공식 기관이 운영하거나 인증한 프로젝트가 아닙니다.

## 설치 및 구축

Python 3.10+, Git, FTS5 trigram을 지원하는 SQLite 3.34+가 필요합니다.
전체 구축 사례의 DB는 약 10.5GB이며 미러 저장 공간은 별도입니다.

```sh
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements.txt
git clone --depth 1 https://github.com/legalize-kr/legalize-kr.git lawdb/mirror/legalize-kr
git clone --depth 1 https://github.com/legalize-kr/admrule-kr.git lawdb/mirror/admrule-kr
git clone --depth 1 https://github.com/legalize-kr/ordinance-kr.git lawdb/mirror/ordinance-kr
git clone --depth 1 https://github.com/legalize-kr/precedent-kr.git lawdb/mirror/precedent-kr
python lawdb/lawdb.py build
python lawdb/lawdb.py stats
```

법령만 시작하려면 첫 번째 미러만 clone하고 `python lawdb/lawdb.py build --corpus law`를 실행합니다.
현재 스냅샷을 적재하며 Git 전체 개정 이력을 DB에 적재하지는 않습니다.

## 검색

```sh
python lawdb/lawdb.py search "확정일자" --corpus law --limit 5
python lawdb/lawdb.py get 주택임대차보호법 --kind 법률 --article 제3조의2
python lawdb/lawdb.py get 약사법 --upcoming
```

## 공식 현행본 보완

[국가법령정보 공동활용](https://open.law.go.kr/) 이용 신청과 접근 조건을 확인하세요.
환경변수 LAW_OC를 설정하거나 `.env.example`을 저장소 루트의 `.env`로 복사해 값을 입력합니다.
실제 인증값과 `.env`는 커밋하지 않습니다.

```sh
python lawdb/official.py plan
python lawdb/official.py fetch --max 300
python lawdb/official.py index
python lawdb/official.py status
python lawdb/verify.py --random 16 --fulltext 4 --delay 1.2
```

plan은 공식 목록 API를 여러 번 호출합니다. fetch는 필요한 본문을 제한된 수만큼 받습니다.
남은 항목이 있으면 fetch/index를 반복합니다. 수집 원본과 DB는 로컬에만 저장됩니다.
미러 갱신 후 build → plan → fetch → index 순서로 재처리합니다.
미러 이력 재작성 시 무조건 강제 reset하지 말고 원격 변경을 확인하세요.

## 출처와 한계

- 데이터 출처: [Legalize](https://github.com/legalize-kr), [국가법령정보센터](https://www.law.go.kr/).
- DB, 원본 미러, 수집 JSON, 개인 설정·대화·로그는 포함하지 않습니다.
- 전체성·최신성은 보장하지 않습니다. 미러 커밋 날짜만으로 자료의 최신 수록 시점을 판단하지 않습니다.
- 현행 판정은 마지막 공식 대조 당시 기준입니다. 시행일 도래에 따른 자동 재검증은 아직 없습니다.
- 공식본 보완은 법령에만 적용하며 행정규칙·자치법규·판례에는 적용하지 않습니다.
- 3글자 미만 검색어는 전체 스캔이 발생할 수 있습니다.
- 별표·서식·첨부파일과 모든 법률 자료 유형의 완전한 수록을 보장하지 않습니다.
- 기존 CLI는 조회 시에도 DB 초기화 설정을 실행하므로 읽기 전용 서비스용 인터페이스가 아닙니다.
- 색인 과정의 배치 커밋과 성능 설정 때문에 중단·장애 시 재구축이 필요할 수 있습니다.

원천 데이터와 제3자 코드의 이용조건은 각 출처를 따릅니다.
이 저장소 자체 코드의 오픈소스 라이선스는 아직 지정하지 않았습니다.
