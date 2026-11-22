#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v docker >/dev/null; then
  if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    echo 'Для автоматической установки Docker запусти: sudo ./install.sh' >&2
    exit 1
  fi
  if [[ ! -f /etc/os-release ]]; then echo 'Нужен Ubuntu/Debian или готовый Docker.' >&2; exit 1; fi
  . /etc/os-release
  if [[ $ID != ubuntu && $ID != debian ]]; then
    echo 'Автоустановка поддерживает Ubuntu/Debian. Для этой ОС установи Docker самостоятельно.' >&2
    exit 1
  fi
  echo "Устанавливаю Docker Engine и Compose из официального APT-репозитория для $ID..."
  apt-get update
  apt-get install -y ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  cat >/etc/apt/sources.list.d/docker.sources <<APT_SOURCE
Types: deb
URIs: https://download.docker.com/linux/$ID
Suites: ${VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
APT_SOURCE
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
# Если Docker установлен, но служба выключена после перезагрузки, поднимем её.
if command -v systemctl >/dev/null 2>&1 && [[ $(id -u) -eq 0 ]]; then
  systemctl enable --now docker
fi
docker compose version >/dev/null || { echo 'Нужен Docker Compose plugin. Проверь установку Docker.' >&2; exit 1; }
if [[ ! -f .env ]]; then cp .env.example .env; chmod 600 .env; fi
mkdir -p secrets
chmod 700 secrets
if [[ ! -s secrets/db_password ]]; then
  umask 077
  od -An -N32 -tx1 /dev/urandom | tr -d ' \n' > secrets/db_password
  printf '\n' >> secrets/db_password
fi
if ! grep -q '^DOMAIN=[[:alnum:]]' .env || grep -q '^DOMAIN=news.example.com$' .env; then
  echo 'Укажите настоящий домен в .env (DOMAIN=...).' >&2
  exit 1
fi
# Compose environment interpolation works with the same password from the Docker secret.
export DB_PASSWORD="$(cat secrets/db_password)"
docker compose up -d --build
printf '\nЗапущено. Откройте https://%s и введите код первичной настройки из логов:\n' "$(sed -n 's/^DOMAIN=//p' .env | head -1)"
echo '  sudo /opt/pars/logs.sh'
# Показываем код сразу, чтобы не требовалась отдельная команда.
for _ in $(seq 1 30); do
  if docker compose exec -T app test -s /data/first-run-code 2>/dev/null; then
    echo 'Код первого входа:'
    docker compose exec -T app cat /data/first-run-code
    break
  fi
  sleep 2
done
