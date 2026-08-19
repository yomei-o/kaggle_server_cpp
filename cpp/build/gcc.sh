#!/bin/sh
# mingw / g++ ビルド（このマシンでは w64devkit）。
#   sh cpp/build/gcc.sh cpp/kbridge_server.cpp -o kbridge_server.exe
#   sh cpp/build/gcc.sh cpp/kbridge_cli.cpp    -o kbridge.exe
#   sh cpp/build/gcc.sh cpp/build/ssl_smoke.cpp -o ssl_smoke.exe   # ツールチェーン検証
#
# TLS は mbedTLS(ヘッダオンリー) を cpp-httplib のネイティブバックエンドとして使う。
# OpenSSL は不要。1 TU に mbedTLS 全体が入るのでビルドは 40 秒ほどかかる。
set -e
HERE=$(cd "$(dirname "$0")/../.." && pwd)   # リポジトリルート
SRC="$1"; shift
OUT="kbridge_server.exe"
if [ "$1" = "-o" ]; then OUT="$2"; shift 2; fi

g++ -std=c++20 -O2 \
  -DCPPHTTPLIB_MBEDTLS_SUPPORT \
  -I"$HERE/cpp/third_party" -I"$HERE/cpp/third_party/mbedtls" -I"$HERE/cpp/pure" \
  $EXTRA "$@" "$SRC" -o "$OUT" \
  -lws2_32 -lcrypt32 -lbcrypt
echo "built $OUT ($(g++ --version | head -1))"
