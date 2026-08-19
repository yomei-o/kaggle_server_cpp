# cpp/third_party/ に加えた変更

同梱ライブラリは原則そのまま置く。やむを得ず手を入れたものだけをここに全部書く。
差し替え時はこの一覧を再適用すること。

## mbedtls/net_sockets.cpp — Windows の read/write/close マクロを末尾で外した

元コードは Windows ビルドで

```c
#define read(fd, buf, len)  recv(fd, (char *) (buf), (int) (len), 0)
#define write(fd, buf, len) send(fd, (char *) (buf), (int) (len), 0)
#define close(fd)           closesocket(fd)
```

を定義したまま解除しない。MSVC では実害が出なかったが、mingw (w64devkit / gcc 14.2) では
この関数マクロが以降に取り込まれるヘッダの宣言まで書き換えてしまい、`winsock2.h` の
`recv` / `send` / `closesocket` の宣言と衝突してビルドが落ちる
（`error: conflicting declaration of C function 'int send(...)'` など）。

この 3 つのマクロは `net_sockets.cpp` の中でしか使わないので、**ファイル末尾で `#undef`** した。

## openssl/mbedtls.h — 今は使っていない（同梱のみ）

`openssl/` 以下は mbedTLS の上に OpenSSL API を被せた互換シム。当初は
`cpp-httplib` に HTTPS を喋らせるために必要だと考えて持ち込んだが、**cpp-httplib 0.46.1 は
mbedTLS をネイティブに扱える**（`CPPHTTPLIB_MBEDTLS_SUPPORT`、しかも `<mbedtlspp.hpp>` が
あればそれを使う）ことが分かったため、kbridge 本体はシムを経由しない。

将来 OpenSSL API を前提にした別ライブラリを足すときのために残してある。その際は以下 2 つの
修正が入った状態であることに注意（どちらもこのリポジトリで入れたもの）:

1. **CA チェーンを conf に結び付ける** — 元は `mbedtls_ssl_conf_ca_chain()` の呼び出しが
   コメントアウトされており、`SSL_CTX_get_cert_store()` / `X509_STORE_add_cert()` で積んだ
   ルート証明書がハンドシェイクで一切参照されず、サーバ証明書の検証が必ず失敗した。
   `SSL` のコンストラクタで `mbedtls_ssl_setup()` の直前に結び付けるようにした。
2. **`X509_STORE_add_cert()` の `assert` を外した** — Windows のシステムルートストアには
   mbedTLS が解釈できない証明書が数枚混ざっており、NDEBUG なしのビルドでは abort していた。
   読めない証明書は捨てて戻り値 0 を返すだけにした。

なお、この 2 点は使わない経路の話なので、シムを外しても kbridge の動作には影響しない。
`#include <openssl/ssl.h>` する場合は `<regex>` と `<iostream>` を先に入れる必要があるため、
`cpp/third_party/mbedtls_openssl.hpp` というラッパを用意してある。
