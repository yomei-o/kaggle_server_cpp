#!/bin/sh
# MSVC ビルド（vcvars 不要）。VS2022 のツールセットと Windows SDK を自力で探して
# INCLUDE / LIB / PATH を組み立てる。姉妹リポジトリ yolo_lpr_cpp の build/cc.sh と同じ流儀。
#
#   sh cpp/build/cc.sh cpp/kbridge_server.cpp -o kbridge_server.exe
#   sh cpp/build/cc.sh cpp/build/ssl_smoke.cpp -o ssl_smoke.exe
set -e
HERE=$(cd "$(dirname "$0")/../.." && pwd)

VSROOT="/c/Program Files/Microsoft Visual Studio/2022"
MSVC_DIR=$(ls -d "$VSROOT"/*/VC/Tools/MSVC/* 2>/dev/null | sort -V | tail -1)
SDK_INC=$(ls -d "/c/Program Files (x86)/Windows Kits/10/Include"/* 2>/dev/null | sort -V | tail -1)
SDK_LIB=$(ls -d "/c/Program Files (x86)/Windows Kits/10/Lib"/* 2>/dev/null | sort -V | tail -1)
[ -n "$MSVC_DIR" ] || { echo "no MSVC toolset found under $VSROOT"; exit 1; }
[ -n "$SDK_INC" ] || { echo "no Windows SDK found"; exit 1; }

BIN="$MSVC_DIR/bin/Hostx64/x64"
export INCLUDE="$(cygpath -w "$MSVC_DIR/include");$(cygpath -w "$SDK_INC/ucrt");$(cygpath -w "$SDK_INC/um");$(cygpath -w "$SDK_INC/shared");$(cygpath -w "$SDK_INC/winrt")"
export LIB="$(cygpath -w "$MSVC_DIR/lib/x64");$(cygpath -w "$SDK_LIB/ucrt/x64");$(cygpath -w "$SDK_LIB/um/x64")"
export PATH="$BIN:$PATH"

SRC="$1"; shift
OUT="kbridge_server.exe"
if [ "$1" = "-o" ]; then OUT="$2"; shift 2; fi

mkdir -p scratch
cl.exe //nologo //std:c++20 //O2 //EHsc //utf-8 //Zc:preprocessor //DNOMINMAX \
  //DCPPHTTPLIB_MBEDTLS_SUPPORT \
  //I "$(cygpath -w "$HERE/cpp/third_party")" \
  //I "$(cygpath -w "$HERE/cpp/third_party/mbedtls")" \
  //I "$(cygpath -w "$HERE/cpp/pure")" \
  $EXTRA "$@" "$(cygpath -w "$SRC")" //Fo:scratch\\ //Fe:"$(cygpath -w "$OUT")" \
  //link ws2_32.lib crypt32.lib bcrypt.lib
echo "built $OUT (MSVC $(basename "$MSVC_DIR"), SDK $(basename "$SDK_INC"))"
