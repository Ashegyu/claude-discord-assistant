#!/bin/sh
# 컨테이너 시작 때마다: !update로 새 패키지가 생겼으면 설치(이미 있으면 몇 초), data 폴더 준비.
set -e
cd /app
[ -f .env ] || { echo "❌ /app/.env 가 없어요. 저장소 폴더에서 cp .env.example .env 후 값을 채우세요."; exit 1; }
mkdir -p data
pip install -q --no-cache-dir -r requirements.txt || echo "⚠️ 패키지 설치 실패 — 이미지에 있는 패키지로 계속 실행해요"
exec "$@"
