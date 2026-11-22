# 자동 갱신 워크플로 설치 (한 번만)

Pickle의 GitHub 연결에는 `workflows` 권한이 없어서 워크플로 파일만은 대신 올릴 수 없습니다.
아래 3단계로 한 번만 넣어 주세요. 넣은 뒤 이 `setup/` 폴더는 지워도 됩니다.

1. 새 파일 만들기 화면을 연다
   <https://github.com/jjjystw/viral-news-app/new/main?filename=.github%2Fworkflows%2Fupdate-news.yml>
2. 이 폴더의 `update-news.yml` 내용을 그대로 붙여넣는다
   (복사용 원본: <https://raw.githubusercontent.com/jjjystw/viral-news-app/main/setup/update-news.yml>)
3. **Commit changes** → *Commit directly to the `main` branch* → **Commit**

저장소 → **Actions** 탭에 *뉴스 자동 갱신* 이 생기고, 매시 정각에 자동 실행됩니다.
지금 한 번 돌려보려면 Actions → 뉴스 자동 갱신 → **Run workflow**.
