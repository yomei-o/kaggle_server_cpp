# THIRD PARTY NOTICES

kbridge 本体のコードは本リポジトリの LICENSE に従う。`cpp/third_party/` に同梱している
第三者ライブラリはそれぞれ以下のライセンスに従う。加えた変更は `cpp/third_party/PATCHES.md`
に全て記載してある。

| 同梱物 | 由来 | バージョン | ライセンス |
|---|---|---|---|
| `cpp/third_party/httplib.h` | [yhirose/cpp-httplib](https://github.com/yhirose/cpp-httplib) | 0.46.1 | MIT |
| `cpp/third_party/json.hpp` | [nlohmann/json](https://github.com/nlohmann/json) | 3.11.3 | MIT |
| `cpp/third_party/mbedtls/`, `mbedtlspp.hpp` | [Mbed TLS](https://github.com/Mbed-TLS/mbedtls)（ヘッダオンリー化した派生物） | 3.6.4 | Apache-2.0 |
| `cpp/third_party/openssl/` | Mbed TLS 上の OpenSSL 互換シム（同梱のみ・未使用） | — | Apache-2.0 準拠で扱う |

Python 側（`python/`）は標準ライブラリと FastAPI / uvicorn のみを使う。WebSocket
クライアントは外部依存を増やさないために自前実装している（`python/kbridge/ws.py`）。

Kaggle は Google LLC のサービスであり、本リポジトリとは無関係。kbridge は Kaggle が公開して
いる Jupyter Server エンドポイント（Notebook の "VSCode Compatible URL"）を、VS Code の
Jupyter 拡張の代わりに叩くだけのクライアントである。利用は Kaggle の利用規約に従うこと。
