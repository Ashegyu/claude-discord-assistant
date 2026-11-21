# 디스코드 비서 컨테이너 (선택 설치 방식). 코드는 이미지에 넣지 않고 저장소 폴더를 /app 에 연결해서 씀.
# 그래서 !update(git pull), config.local.yaml, data/, .env 가 그대로 호스트 폴더에 남는다.
FROM python:3.12-slim

ENV TZ=Asia/Seoul \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

RUN apt-get update \
 && apt-get install -y --no-install-recommends git tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system --add safe.directory /app

WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "assistant.bot"]
