#!/bin/sh
# ssl_smoke 用の自己署名証明書を作り直す。生成物は .gitignore 済み（鍵を配らないため）。
#   sh cpp/build/testcert/gen.sh
set -e
cd "$(dirname "$0")"
openssl req -x509 -newkey rsa:2048 -keyout server.key -out server.crt \
        -days 3650 -nodes -config openssl.cnf
openssl x509 -in server.crt -noout -subject -ext subjectAltName
