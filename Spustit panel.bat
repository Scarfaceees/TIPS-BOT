@echo off
rem Tips Bot - panel bezi v Dockeru (kontejner tipsbot, startuje sam po restartu). Dvojklik ho zapne, kdyby stal, a otevre prohlizec.
cd /d "%~dp0"
docker compose up -d
start "" http://127.0.0.1:8000
