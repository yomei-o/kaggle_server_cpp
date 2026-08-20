# RESUME — kbridge の実装状況と残作業

最終更新: 2026-08-19（本物の Kaggle T4 x2 で疎通確認済み）

使い方は [README.md](README.md)、REST 仕様は [spec/API.md](spec/API.md)（こちらが正）。

---

## いま何ができるか

| 機能 | python 版 | cpp 版 | 備考 |
|---|---|---|---|
| セッション（接続/再利用/切断/割り込み/再起動） | ✅ | ✅ | 既存カーネル再利用が既定 |
| `/exec`, `/exec/stream` | ✅ | ✅ | NDJSON で逐次出力 |
| `/sh`, `/sh/stream`（終了コード付き） | ✅ | ✅ | |
| `/gpu` | ✅ | ✅ | GPU 無しでも `ok:true, gpus:[]` |
| `/upload`, `/download`, `/ls`, `/mkdir`, `/rm` | ✅ | ✅ | バイナリは base64 |
| `/job`（切り離し実行・増分ログ・kill） | ✅ | ✅ | **学習はこれを使う** |
| `/batch/*`（kaggle CLI） | ✅ | ✅ | CLI 不在なら `ok:false` |
| CLI（connect/exec/run/put/get/sync/job/batch） | ✅ | ✅ | |
| 偽 Jupyter サーバでの試験 | ✅ 37/37 | ✅ 37/37 | Kaggle 枠を使わない |
| 2 実装のパリティ | ✅ 29/29 | ✅ | 生成コードはバイト単位で一致 |
| CLI のパリティ | ✅ 11/11 | ✅ | 出力と終了コードが一致 |
| TLS(mbedTLS) + WSS の土台 | — | ✅ 6/6 | mingw / MSVC 両方 |
| **本物の Kaggle GPU での実行** | ✅ | ✅ | 下記 |

✅ = 動作確認済み、— = 対象外

### 本物の Kaggle（T4 x2 セッション）で確認したこと

2026-08-19 に実機で通した。python 版・cpp 版の**両方**で同じ結果。

| 見たこと | 結果 |
|---|---|
| VSCode Compatible URL への接続 | 既存カーネルを再利用して接続（`reuse: true`） |
| カーネル環境 | Python 3.12.13 / Ubuntu 22.04 / 4 vCPU / 31 GB RAM / `/kaggle/working` 20 GB |
| `GET /gpu` | `Tesla T4` x2、CUDA 12.8、driver 580.159.04、torch 2.10.0+cu128、`torch_cuda: true` |
| C++（CPU）| ローカルの `.cpp` を送って `g++ 11.4` でビルド → 実行まで成功 |
| **CUDA** | `.cu` を送って `nvcc -O2 -arch=sm_75` で **2.7 秒**ビルド → T4 で実行、誤差 0 |
| `/job` | 切り離し起動 → 増分ログ取得 → 終了コード回収まで成功 |

T4 は `sm_75`。GPU を使うには Notebook の Settings → Accelerator を GPU にしてから
セッションを起こすこと（CPU セッションだと `nvidia-smi` が無く `gpus: []` が返る。
これは異常ではなく、そのセッションに GPU が付いていないだけ）。

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
* cpp-httplib の `WebSocketClient` は既定で 30 秒ごとにクライアント ping を投げるが、
  Kaggle のプロキシ越しだとこれで接続が落ちることがあった。Jupyter のカーネル
  チャンネルは ping を要求しないので `set_websocket_ping_interval(0)` で止めてある。
  併せて「切れたら次の呼び出しで張り直す」処理を両実装に入れてある。
* WebSocket を閉じるとき、close フレームを送っただけでは相手が TCP を閉じるまで
  `read()` が返らない。無限に待つと切断処理ごと固まるので、cpp 版は 10 秒だけ待って
  リーダスレッドを手放す（スレッドは接続の shared_ptr を持っているので後から安全に終わる）。
  試験用の偽サーバ側も、本物と同じく WebSocket 終了時に接続を閉じるようにしてある。

---

## keep-alive の状態（2026-08-20）

`--keepalive SECONDS`（既定 240、0 で無効）を両実装に入れた。アイドルが続いたら
カーネルへ `pass` を 1 セル投げるだけの常駐スレッド。`tests/keepalive.py` が
両実装で 6/6（偽 Jupyter 相手、間隔 2 秒で発火と非発火を見る）。

**未検証: 本物の Kaggle で 40 分放置して生き残るか。** カーネル活動が Kaggle の
idle タイマを戻すかどうかがまだ分かっていない。これを 1 回測れば、以下のどちらに
進むかが決まる（詳細は README の「keep-alive の効き方」）:

* 効いた → これで終わり。
* 効かない → Kaggle の活動判定は `www.kaggle.com` 側を見ている。`/batch`（Commit は
  idle タイマ無し）へ寄せる、またはブラウザ側 userscript を併用する。

派生して確認したいこと: セッション回収時に `/kaggle/working` が消えるので、
Notebook の Settings → Persistence を有効にしておく必要がある。

## 残作業

### ⏭ 1. 車番認識の学習を実際に載せる（次の本命）

土台は通ったので、あとは中身。`_lpr_src` / `yolo_lpr_cpp` の学習コードを

```sh
python -m kbridge.cli sync <学習コードのディレクトリ> work/pure
python -m kbridge.cli job start "cd /kaggle/working/work && nvcc -O2 -arch=sm_75 ... && ./train ..." --name lpr
python -m kbridge.cli job log <id> --follow
python -m kbridge.cli get work/best.ckpt ./best.ckpt
```

の形に乗せる。詰まりそうなところ:
* 学習データ（実車ナンバーの画像）の置き場所。Kaggle の Input（Dataset）に上げるのが本筋だが、
  Input の追加は Kaggle UI からしかできない。小さいうちは `/upload` で送ってもよい。
* 9 時間でセッションが切れる。`/job` なら切れてもプロセスは残るが、切れた後は
  kbridge から見に行けないので、**チェックポイントをこまめに `/kaggle/working` へ書く**こと。
* 無料枠は週 30 時間。回しっぱなしにしない。

### ⏭ 2. あると嬉しい（優先度低）

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
