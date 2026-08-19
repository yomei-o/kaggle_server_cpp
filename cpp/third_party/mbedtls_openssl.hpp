#pragma once
// openssl/mbedtls.h（mbedTLS 上に載せた OpenSSL 互換シム）は、取り込み側の翻訳単位に
// <regex> と <iostream> が既に入っている前提で書かれている。素で #include すると
//   error: 'match' was not declared / 'cout' is not a member of 'std'
// になるので、必要なヘッダを先に入れてから読み込むラッパをかませる。
// アプリ側はこのファイルだけを include すること。
#include <iostream>
#include <ostream>
#include <regex>
#include <string>
#include <vector>
#include <memory>

#include <openssl/ssl.h>
