# spec/API.md — kbridge ローカルサーバ REST 仕様（v1）

この文書が**正**。`python/`（FastAPI）と `cpp/`（cpp-httplib）は**同じ URL・同じ JSON・同じ挙動**を
実装し、`tests/parity.py` で差分ゼロを縛る。片方にしかない機能は作らない。

```
Claude Code / curl / kbridge CLI
        |  HTTP 127.0.0.1:8787              <-- 本仕様
        v
  kbridge ローカルサーバ (python 版 / cpp 版)
        |  HTTPS REST + WSS (Jupyter 5.3 プロトコル)
        v
  Kaggle Jupyter Server (GPU: T4 x2 / P100)
```

## 0. 共通事項

* すべてのレスポンスは `application/json; charset=utf-8`（例外: `GET /download?raw=1` と
  NDJSON ストリーム）。
* すべてのレスポンスは最上位に `ok` (bool) を持つ。失敗時は `ok:false` と `error` (string) を持つ。
* HTTP ステータス: 成功 200 / 引数不正 400 / セッション未確立 409 / 上流(Kaggle)エラー 502 /
  タイムアウト 504。`ok:false` の本文は必ず返す。
* バインド既定は `127.0.0.1:8787`。`--api-key K` を付けた場合のみ `X-Bridge-Key: K` を必須にする。
* パスは Kaggle 側 Jupyter の contents ルート（= `/kaggle/working`）からの相対。`..` は 400。
* 時間の単位は秒 (float)。
* トークンは**応答にもログにも出さない**（`base_url` は `****` でマスクして返す）。

### keep-alive（サーバ起動オプション）

`--keepalive SECONDS`（既定 240、`0` で無効）。**最後にカーネルへ話しかけてから
SECONDS 秒経ったら `pass` を 1 セル実行する**だけの常駐スレッド。REST 面には出ない
（エンドポイントは増やさない）。発火するたび stdout に
`keepalive #<n> after <idle>s idle: <status>` を 1 行出す。

* 進行中の実行があるときは打たない（それ自体が活動なので）。待ち行列も作らない。
* Kaggle の interactive セッションには idle タイマがある（実測 30 分前後）。`/job` で
  切り離した学習を回している間、呼ぶ側がログを見に来なければ Kaggle へは 1 バイトも
  流れず、セッションを回収される。これを防ぐのが目的。
* WebSocket のプロトコル ping では代用できない（Kaggle のプロキシ手前で終わる）。
* **カーネル活動だけで Kaggle の idle 回収を避けられる（2026-08-20 実機で確認）。**
  本物の T4 x2 セッションでブラウザのタブを閉じたまま 57.5 分放置し、`pass` 以外を
  流さずに生き残った（カーネル pid・VM uptime・名前空間まで同一）。詳細は README の
  「keep-alive の効き方」。

## 1. セッション

### `GET /healthz`
```json
{"ok":true,"impl":"python","version":"0.1.0","connected":true,"kernel_id":"a1b2...","uptime":12.3}
```
`impl` は `"python"` か `"cpp"`。これ以外は両実装で完全一致する。

### `POST /session`
```json
{"url":"https://kkb-production.jupyter-proxy.kaggle.net/k/<id>/<token>/proxy"}
```
* `url` 省略時は環境変数 `KAGGLE_JUPYTER_URL`、次に `.kbridge.json` の `url` を使う。
* 既存カーネルがあれば再利用する（`reuse:true`）。Kaggle の GPU を握っているのは Notebook 本体の
  カーネルなので、**再利用が既定**。`{"new_kernel":true}` で新規作成もできる。
* 応答:
```json
{"ok":true,"base_url":"https://.../k/<id>/****abcd/proxy","kernel_id":"...",
 "session_id":"...","kernel_name":"python3","reuse":false}
```

### `GET /session`  … 現在の接続情報（未確立なら 409）
### `DELETE /session` … カーネルを shutdown して切断 `{"ok":true}`
既定では Notebook 本体のカーネルを再利用しているため、これは **その Notebook の
実行状態を消す**。ブリッジを止めたいだけならサーバを終了すればよい。
### `POST /interrupt` … 実行中セルに割り込み `{"ok":true}`
### `POST /restart` … カーネル再起動 `{"ok":true,"kernel_id":"..."}`

## 2. 実行（対話・短時間向け）

### `POST /exec`
```json
{"code":"import torch; print(torch.cuda.get_device_name(0))","timeout":300}
```
応答（`status` は `ok` / `error` / `timeout` / `abort`）:
```json
{"ok":true,"status":"ok","stdout":"Tesla T4\n","stderr":"","result":"",
 "ename":"","evalue":"","traceback":[],"execution_count":3,"elapsed":0.42}
```
* `result` は `execute_result` / `display_data` の `text/plain`。
* `timeout` 既定 300、最大 43200。超過時は上流へ interrupt を投げ `status:"timeout"`, HTTP 504。

### `POST /exec/stream`
本文は `/exec` と同じ。応答は `application/x-ndjson` の**チャンク転送**。1 行 1 JSON:
```
{"t":"start"}
{"t":"out","stream":"stdout","d":"Epoch 1 loss 12.5\n"}
{"t":"out","stream":"stderr","d":"..."}
{"t":"result","d":"<matplotlib...>"}
{"t":"error","ename":"RuntimeError","evalue":"CUDA OOM","traceback":["..."]}
{"t":"end","status":"ok","execution_count":3,"elapsed":812.4}
```
`t:"end"` は必ず最後に 1 回だけ出る。

### `POST /sh`
```json
{"cmd":"nvcc --version","timeout":300,"cwd":"/kaggle/working"}
```
シェル実行。応答は `/exec` と同形＋ `"exit_code":0`。ストリーミング版は `POST /sh/stream`。

### `GET /gpu`
```json
{"ok":true,"gpus":[{"index":0,"name":"Tesla T4","mem_total_mb":15360,"mem_used_mb":0,
                    "util_pct":0,"temp_c":34}],
 "driver":"550.90.07","cuda":"12.4","torch":"2.4.0+cu121","torch_cuda":true,"raw":"..."}
```
GPU が無い（CPU セッション）場合は `gpus:[]`, `ok:true`。

## 3. ファイル

### `POST /upload`
```json
{"path":"src/train.cpp","text":"int main(){}"}
{"path":"data/a.png","content_b64":"iVBORw0K..."}
```
`text` か `content_b64` のどちらか一方（両方/どちらも無しは 400）。中間ディレクトリは自動作成。
応答 `{"ok":true,"path":"src/train.cpp","size":12}`。

### `GET /download?path=P[&raw=1]`
* 既定: `{"ok":true,"path":"P","size":123,"content_b64":"..."}`
* `raw=1`: `application/octet-stream` で生バイト。

### `GET /ls?path=P`
```json
{"ok":true,"path":"","entries":[{"name":"src","path":"src","type":"directory","size":null},
                                {"name":"a.png","path":"a.png","type":"file","size":1234}]}
```
### `POST /mkdir` `{"path":"src"}` / `POST /rm` `{"path":"src/train.cpp"}`

## 4. ジョブ（長時間学習向け・**学習はこれを使う**）

`/exec` は HTTP 接続を張ったまま待つので、9〜12 時間の学習には向かない（ローカルの再起動・
回線断・エージェントの再起動で結果を失う）。ジョブ API は Kaggle 側でプロセスを**切り離して**
起動し、ログをファイルに落とす。呼ぶ側は好きなときに増分だけ読みにいけばよい。

実体は Kaggle 側で動くヘルパ `kaggle/kbagent.py`。ローカルサーバはこれを**ファイルとして
置かず、カーネルの `sys.modules` へ直接注入して**呼ぶ（置き場所が環境で変わらないため）。
python 版と cpp 版はどちらも同じファイルを同じ手順で注入する。注入・呼び出しに使う
コード文字列は**両実装でバイト単位に一致**しており、`tests/parity.py` がそれを縛っている。
そのため、ジョブ・シェル・GPU 判定の意味論は `kbagent.py` 1 か所にしかない。

### `POST /job`
```json
{"cmd":"nvcc -O2 train.cu -o train && ./train --epochs 50","name":"lpr-train","cwd":"/kaggle/working"}
```
* `cmd`（シェル）か `code`（Python）のどちらか一方。
* 応答: `{"ok":true,"id":"20260819-114500-lpr-train","pid":1234,"log":".kbridge/jobs/<id>.log"}`

### `GET /job` … 一覧
```json
{"ok":true,"jobs":[{"id":"...","name":"lpr-train","state":"running","pid":1234,
                    "started":1755600000.0,"ended":null,"exit_code":null,"log_size":81920}]}
```
`state` は `running` / `done` / `failed` / `killed` / `lost`（プロセスが消えたが終了コード未記録）。

### `GET /job/{id}` … 単体（上と同じ 1 件）
### `GET /job/{id}/log?offset=N&max=M`
```json
{"ok":true,"id":"...","offset":81920,"next_offset":90112,"eof":false,
 "state":"running","data":"Epoch 12 loss 3.21\n..."}
```
* `offset` 既定 0、`max` 既定 65536。`data` は UTF-8 文字列（不正バイトは置換）。
* 学習の追跡は「`next_offset` を持って定期的に叩き直す」だけでよい。
### `POST /job/{id}/kill` … プロセスグループごと停止 `{"ok":true,"state":"killed"}`
### `DELETE /job/{id}` … 終わったジョブのメタ・ログを消す（実行中なら 502）

## 5. バッチ（kaggle CLI フォールバック）

Jupyter セッションは対話向けで、セッションが切れると実行も止まる。確実に 9〜12 時間回したい
場合は `kaggle kernels push` のバッチが向く。`kaggle` CLI が PATH に無い場合は
`ok:false, error:"kaggle CLI not found"` を返す（HTTP 200）。

* `POST /batch/push`   `{"dir":"notebooks/lpr"}` → `kaggle kernels push -p <dir>`
* `GET  /batch/status?id=user/kernel` → `{"ok":true,"state":"running"|"complete"|"error","raw":"..."}`
* `POST /batch/output` `{"id":"user/kernel","dir":"downloads/lpr"}` → 出力一式を取得
* `POST /batch/pull`   `{"id":"user/kernel","dir":"local/lpr"}` → コードと metadata を取得し、
  `kernel-metadata.json` に `run_on_push: false` を書き込む（push のたびに勝手に実行させない）

## 6. パリティ規約

`tests/parity.py` は python 版と cpp 版を別ポートで起動し、同じリクエスト列を投げて
**`impl` フィールドを除く JSON が完全一致**することを確認する。実行ごとに変わる
`elapsed` / `uptime` / `execution_count` / `kernel_id` / `session_id` / `pid` / `id` /
`started` / `ended` はキーの有無と型だけ比較する。

`tests/cli_parity.py` は同じことを CLI に対してやる（標準出力と終了コードの一致）。
両 CLI とも、失敗も `ok:false` の JSON として**標準出力**に出し、終了コード 1 を返す
（エラーだけ標準エラーに逃がさない。エージェントから使うとき成功と失敗を同じ形で拾えるように）。
