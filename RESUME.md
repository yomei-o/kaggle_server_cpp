# RESUME — kbridge の実装状況と残作業

最終更新: 2026-08-19

使い方は [README.md](README.md)、REST 仕様は [spec/API.md](spec/API.md)（こちらが正）。

---

## いま何ができるか

| 機能 | python 版 | cpp 版 | 備考 |
|---|---|---|---|
| セッション（接続/再利用/切断/割り込み/再起動） | ✅ | ⏳ | 既存カーネル再利用が既定 |
| `/exec`, `/exec/stream` | ✅ | ⏳ | NDJSON で逐次出力 |
| `/sh`, `/sh/stream`（終了コード付き） | ✅ | ⏳ | |
| `/gpu` | ✅ | ⏳ | GPU 無しでも `ok:true, gpus:[]` |
| `/upload`, `/download`, `/ls`, `/mkdir`, `/rm` | ✅ | ⏳ | バイナリは base64 |
| `/job`（切り離し実行・増分ログ・kill） | ✅ | ⏳ | **学習はこれを使う** |
| `/batch/*`（kaggle CLI） | ✅ | ⏳ | CLI 不在なら `ok:false` |
| CLI（connect/exec/run/put/get/sync/job/batch） | ✅ | ⏳ | |
| 偽 Jupyter サーバでの試験 | ✅ 37/37 | ⏳ | Kaggle 枠を使わない |
| TLS(mbedTLS) + WSS の土台 | — | ✅ 6/6 | mingw / MSVC 両方 |

✅ = 動作確認済み、⏳ = これから、— = 対象外

### 確認済みの事実（再調査しなくてよい）

* Kaggle の "VSCode Compatible URL" は
  `https://kkb-production.jupyter-proxy.kaggle.net/k/<id>/<token>/proxy` の形で、
  **token はクエリではなくパスの末尾から 2 番目**。`base_url` は `/proxy` まで含める。
* 認証は「URL のパスに token が入っていること」＋ `Authorization: token <token>` ヘッダ。
  POST/PUT/DELETE には最初の `GET <base>/` で受け取った `_xsrf` クッキーを
  `X-XSRFToken` ヘッダで返す必要がある。
* カーネルチャンネルは `wss://<base>/api/kernels/<kid>/channels?session_id=...`。
  サブプロトコル `v1.kernel.websocket.jupyter.org` は**要求しない**
  （要求するとバイナリ多重化形式になり JSON テキストで読めなくなる）。
* cpp-httplib 0.46.1 は **mbedTLS ネイティブ対応**（`CPPHTTPLIB_MBEDTLS_SUPPORT`）で、
  `<mbedtlspp.hpp>` があればそれを使う。**`WebSocketClient` も入っている**ので、
  C++ 側で RFC6455 を手書きする必要はない。OpenSSL 互換シムも要らない
  （`cpp/third_party/openssl/` は同梱してあるが未使用）。
* Windows のシステムルート証明書で `www.kaggle.com` と
  `kkb-production.jupyter-proxy.kaggle.net` の検証は通る。
  `example.com` が通らないのはローカルストアに当該ルートが無いだけ（`cpp/build/ca_probe.cpp` で確認）。
* mbedTLS を mingw でビルドするには `net_sockets.cpp` の `read/write/close` マクロを
  末尾で `#undef` する必要がある（`cpp/third_party/PATCHES.md`）。
* MSVC は `/std:c++20` が要る（mbedTLS が指定初期化子を使っている）。

---

## 残作業

### ⏭ 1. cpp 版の実装（最優先）

`spec/API.md` を python 版と同じに実装する。土台（TLS・WSS・HTTP サーバ・JSON）は
`ssl_smoke.cpp` で実証済みなので、残りは Jupyter プロトコルと REST の組み立てだけ。

予定しているファイル:

```
cpp/pure/jurl.hpp      VSCode Compatible URL のパーサ（python/kbridge/jurl.py と同じ規則）
cpp/pure/jupyter.hpp   REST + カーネル WebSocket（python/kbridge/jupyter.py と同じ手順）
cpp/pure/ops.hpp       kbagent の注入と /sh /gpu /job（python/kbridge/ops.py と同じ生成コード）
cpp/pure/batch.hpp     kaggle CLI 呼び出し
cpp/kbridge_server.cpp cpp-httplib のローカルサーバ
cpp/kbridge_cli.cpp    CLI（python -m kbridge.cli と同じサブコマンド）
```

**注入するコード文字列が python 版と一致していること**がパリティの肝。
`kaggle/kbagent.py` を読んで base64 にして送る、という手順まで同じにする。

### ⏭ 2. パリティ試験 `tests/parity.py`

両サーバを別ポートで起動し、同じリクエスト列を投げて `impl` 以外の JSON が一致することを見る。
実行ごとに変わる値（`elapsed` `uptime` `kernel_id` `pid` `id` `started` `ended`
`execution_count`）はキーの有無と型だけ比較する。

### ⏭ 3. 本物の Kaggle での疎通確認

ここまでは全部「偽 Jupyter サーバ」相手の結果で、**本物の Kaggle にはまだ 1 度も繋いでいない**。
最初に確かめること:

1. `connect` → `GET /gpu` で `Tesla T4` が出るか
2. `run "nvcc --version"` が通るか
3. `job start "nvcc -O2 x.cu -o x && ./x"` でビルドから実行まで通るか
4. セッションが切れたあと `connect` し直してログを読み直せるか

### ⏭ 4. あると嬉しい（優先度低）

* `/exec` の `display_data` から画像（PNG）を取れるようにする（学習曲線の確認用）
* `sync` の差分転送（今は毎回全部送る）
* Kaggle の Notebook 側テンプレート（`notebooks/`）— セッション起動を少しでも省力化する

---

## 既知の制約

* Kaggle のセッション起動（Run → Kaggle Jupyter Server → Start Session）は手作業。
  API では起こせない。
* Input（データセット・モデル）の追加は Kaggle UI からのみ。セッション開始前に揃える。
* 偽 Jupyter サーバのカーネルは同一プロセス内の Python 名前空間なので、
  本物と違って「別プロセスが落ちる」系の挙動は再現できない。
* `kaggle/kbagent.py` は本来 Linux 専用だが、ローカル試験のために Windows でも動くように
  してある（`POSIX` / `BASH` / `STDBUF` の分岐）。Kaggle 上の挙動は分岐前と同じ。

---

## 要望・依頼を書く欄（yolo_lpr_cpp 側の Claude へ）

ここに書いてもらえれば拾う。書式は自由。「何をしたいか」と「今どう詰まっているか」が
あれば十分。API の追加要望なら、欲しい入出力の例（JSON）があると早い。

### 未対応の依頼

（まだ無し）

### 対応済みの依頼

（まだ無し）

---

## 参考にした資料

* [ローカル VS Code から Kaggle GPU を操る（Qiita）](https://qiita.com/kuririrn/items/efea497f30519a3e6d59)
  — 出発点。VS Code の Jupyter 拡張で繋ぐ公式機能の使い方。
* [ローカルの Cursor/VSCode で Kaggle の Jupyter Server に接続する（Zenn）](https://zenn.dev/prgckwb/articles/kaggle-vscode-link)
* [JuypterSSH](https://github.com/yaelliethy/JuypterSSH) — URL の形と xsrf の扱いはここで確認した。
* [Jupyter Server の WebSocket プロトコル](https://jupyter-server.readthedocs.io/en/latest/developers/websocket-protocols.html)
