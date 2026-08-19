# spec/API.md — kbridge ローカルサーバ REST 仕様（v1）

この文書が**正**。`python/`（FastAPI）と `cpp/`（cpp-httplib）は**同じ URL・同じ JSON・同じ挙動**を
実装し、`tests/parity.sh` で差分ゼロを縛る。片方にしかない機能は作らない。

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

* すべてのレスポンスは `application/json; charset=utf-8`（例外: `GET /download?raw=1`）。
* すべてのレスポンスは最上位に `ok` (bool) を持つ。失敗時は `ok:false` と `error` (string) を持つ。
* HTTP ステータス: 成功 200 / 引数不正 400 / セッション未確立 409 / 上流(Kaggle)エラー 502 /
  タイムアウト 504。`ok:false` の本文は必ず返す。
* バインド既定は `127.0.0.1:8787`。`--api-key K` を付けた場合のみ `X-Bridge-Key: K` を必須にする。
* パスは Kaggle 側 Jupyter の contents ルート（= `/kaggle/working`）からの相対。`..` は 400。
* 時間の単位は秒 (float)。

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
* 既存カーネルがあれば再利用（`reuse:true`）、なければ `POST /api/sessions` で新規作成。
* 応答:
```json
{"ok":true,"base_url":"https://.../k/<id>/<token>/proxy","kernel_id":"...",
 "session_id":"...","kernel_name":"python3","reuse":false}
```
* `token` は応答に**含めない**（ログにも出さない。マスクは `<id>` と末尾4文字のみ）。

### `GET /session`  … 現在の接続情報（未確立なら 409）
### `DELETE /session` … カーネルを shutdown して切断 `{"ok":true}`
### `POST /interrupt` … 実行中セルに割り込み `{"ok":true}`
### `POST /restart` … カーネル再起動 `{"ok":true,"kernel_id":"..."}`

## 2. 実行

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
{"t":"start","execution_count":null}
{"t":"out","stream":"stdout","d":"Epoch 1 loss 12.5\n"}
{"t":"out","stream":"stderr","d":"..."}
{"t":"result","d":"<matplotlib...>"}
{"t":"error","ename":"RuntimeError","evalue":"CUDA OOM","traceback":["..."]}
{"t":"end","status":"ok","execution_count":3,"elapsed":812.4}
```
`t:"end"` は必ず最後に 1 回だけ出る。学習ログの追跡はこれを使う。

### `POST /sh`
```json
{"cmd":"nvcc --version","timeout":300,"cwd":"/kaggle/working"}
```
シェル実行。応答は `/exec` と同形＋ `"exit_code":0`。
実体は `subprocess` 相当のコードをカーネルへ送るので、終了コードが正しく取れる。
ストリーミング版は `POST /sh/stream`（NDJSON、`/exec/stream` と同形式）。

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

## 4. バッチ（kaggle CLI フォールバック）

Jupyter セッションは対話向けで、長時間学習（9〜12h）は `kaggle kernels push` のバッチが向く。
`kaggle` CLI が PATH に無い場合は `ok:false, error:"kaggle CLI not found"`。

* `POST /batch/push`   `{"dir":"notebooks/lpr"}` → `kaggle kernels push -p <dir>`
* `GET  /batch/status?id=user/kernel` → `{"ok":true,"status":"running"|"complete"|"error","raw":"..."}`
* `POST /batch/output` `{"id":"user/kernel","dir":"downloads/lpr"}` → 出力一式を取得

## 5. パリティ規約

`tests/parity.sh` は python 版と cpp 版を別ポートで起動し、同じリクエスト列を投げて
**`impl` フィールドを除く JSON が完全一致**することを確認する。数値の揺れる
`elapsed` / `uptime` / `execution_count` / `kernel_id` / `session_id` はキーの有無と型のみ比較する。
