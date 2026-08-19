#!/bin/sh
# 全部の試験を順に回す。Kaggle には繋がないので GPU 無料枠を消費しない。
#   sh tests/run_all.sh
set -e
cd "$(dirname "$0")/.."

if [ ! -f kbridge_server.exe ]; then
  echo "== building cpp =="
  sh cpp/build/gcc.sh cpp/kbridge_server.cpp -o kbridge_server.exe
  sh cpp/build/gcc.sh cpp/kbridge_cli.cpp    -o kbridge.exe
fi

echo "== e2e (python) ==";  python -u tests/e2e.py            | tail -3
echo "== e2e (cpp) ==";     python -u tests/e2e.py --impl cpp | tail -3
echo "== parity (server) =="; python -u tests/parity.py       | tail -3
echo "== parity (cli) ==";    python -u tests/cli_parity.py   | tail -3
