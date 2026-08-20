# kaggle_server_cpp — Kaggle の GPU をローカルの HTTP API で使う（kbridge）

*日本語 / [English README](README.en.md)*

Kaggle Notebook が出す **"VSCode Compatible URL"**（＝ Kaggle がホストしている Jupyter Server の
エンドポイント）を、VS Code の Jupyter 拡張の代わりに**自前のローカルサーバ**から叩く。
これで Claude Code や `curl` から、Kaggle の無料 GPU（T4×2 / P100）でコードをビルド・実行・学習できる。

```
Claude Code / curl / kbridge CLI
        │  HTTP  127.0.0.1:8787          ← これが本リポジトリの成果物（ローカルサーバ）
        ▼
   kbridge ローカルサーバ   ── python 版 (FastAPI) と cpp 版 (cpp-httplib) の対等な 2 実装
        │  HTTPS REST + WSS（Jupyter 5.3 プロトコルを自前実装）
        ▼
   Kaggle Jupyter Server（GPU セッション）
```

**設計の芯: Python と C++ が対等。** 同じ URL・同じ JSON・同じ挙動を両言語で実装し、
`tests/parity.py` で差分ゼロを縛る。仕様は [`spec/API.md`](spec/API.md) が正。
片方にしかない機能は作らない（姉妹リポジトリ
[yolo_lpr_cpp](https://github.com/yomei-o/yolo_lpr_cpp) と同じ方針）。

現在の状態と残作業は [RESUME.md](RESUME.md)。

**AI エージェントに読ませるなら → [FOR_AGENTS.md](FOR_AGENTS.md)**（人間が URL を 1 つ渡すだけで、
エージェントが `curl` だけで T4 を使えるようになるまでの最短手順。実測値と落とし穴つき）。

---

## 1. 使い始める（5 分）

### 1.0 先に用意するもの（認証まわり）

必要な資格情報は用途によって 2 つに分かれる。**主経路（Jupyter Server）に API トークンは要らない。**

| したいこと | 要るもの | 取り方 |
|---|---|---|
| Jupyter Server 経由で実行（kbridge の主経路） | **API トークンは不要**。VSCode Compatible URL 自体に認証トークンが入っている | 1.1 の手順 |
| バッチ投入（`/batch/*` = `kaggle kernels push`） | Kaggle API トークン | 下記 |

前提: **GPU を使うにはアカウントの電話番号認証が要る**（未認証だと Notebook の
Accelerator と Internet が選べない）。kaggle.com → 右上のアイコン → Settings → Phone verification。
Notebook 右パネルの Settings → Accelerator で `GPU T4 x2` か `P100` を選ぶ。
無料枠は **週 30 時間**、1 セッションは最長 9 時間（CPU のみなら 12 時間）。

API トークンを取る（バッチを使うときだけ）:

1. kaggle.com → 右上のアイコン → **Settings** → **API** → **Create New Token**
2. 形式が 2 つある。**新形式（`KGAT_` で始まる）は環境変数でしか使えない**:

```sh
# 新形式
export KAGGLE_API_TOKEN=KGAT_xxxxxxxxxxxxxxxxxxxx     # Windows: setx KAGGLE_API_TOKEN KGAT_...

# 旧形式（kaggle.json がダウンロードされる場合）
mkdir -p ~/.kaggle && mv ~/Downloads/kaggle.json ~/.kaggle/
chmod 600 ~/.kaggle/kaggle.json
```

3. 確認: `kaggle kernels list -m` が通れば OK（`pip install kaggle` が要る）。

kbridge はトークンを読まないし保存もしない。`/batch/*` は PATH の `kaggle` コマンドを
呼ぶだけなので、認証は CLI 側の作法にそのまま従う。

### 1.1 Kaggle 側でセッションを起こして URL をもらう

1. Kaggle で Notebook を開く（GPU を使うなら Settings → Accelerator を GPU にする）
2. メニューの **Run → Kaggle Jupyter Server → Start Session**
3. 出てくる **"VSCode Compatible URL"** をコピーする。形はこう:

```
https://kkb-production.jupyter-proxy.kaggle.net/k/<id>/<token>/proxy
```

> この URL は**そのまま認証情報**（パスに token が入っている）。人に見せない・コミットしない。
> kbridge は応答にもログにも token を出さない（`****` でマスクする）。
> この手順だけは自動化できないので手でやる。

### 1.2 ローカルサーバを起動する

```sh
# python 版
pip install -r python/requirements.txt
cd python && python -m kbridge.server --port 8787

# cpp 版（ビルドは 2. を参照）
./kbridge_server.exe --port 8787
```

### 1.3 つないで動かす

```sh
cd python
python -m kbridge.cli connect "https://kkb-production.jupyter-proxy.kaggle.net/k/xxx/yyy/proxy"
python -m kbridge.cli gpu
#   {"ok": true, "gpus": [{"index": 0, "name": "Tesla T4", "mem_total_mb": 15360, ...}], ...}

python -m kbridge.cli run "nvcc --version"
python -m kbridge.cli exec -c "import torch; print(torch.cuda.get_device_name(0))"
```

`connect` で渡した URL は `.kbridge.json` に保存される（`.gitignore` 済み）。以後は
サーバ起動時に自動で読む。環境変数 `KAGGLE_JUPYTER_URL` でもよい。

---

## 2. ビルド（C++ 版）

外部ライブラリのインストールは要らない。必要なものは全部 `cpp/third_party/` に入っている
（cpp-httplib 0.46.1 / nlohmann-json 3.11.3 / mbedTLS 3.6.4 ヘッダオンリー）。

```sh
sh cpp/build/gcc.sh cpp/kbridge_server.cpp -o kbridge_server.exe   # mingw (w64devkit)
sh cpp/build/cc.sh  cpp/kbridge_server.cpp -o kbridge_server.exe   # MSVC (vcvars 不要)
```

1 つの翻訳単位に mbedTLS 全体が入るのでビルドは 20〜45 秒かかる。

ツールチェーンが壊れていないかは、まずこれで確かめる:

```sh
sh cpp/build/testcert/gen.sh                                       # 自己署名証明書を作る
sh cpp/build/gcc.sh cpp/build/ssl_smoke.cpp -o ssl_smoke.exe && ./ssl_smoke.exe
#   [ OK ] https client -> www.kaggle.com (verify on, system roots)
#   [ OK ] https server <- https client (verify on)
#   [ OK ] https client rejects untrusted cert
#   [ OK ] wss websocket echo (verify on)
#   [ OK ] http server <- http client (loopback)
#   [ OK ] json roundtrip
```

---

## 3. できること（`spec/API.md` の要約）

CLI と REST は 1 対 1 に対応している。エージェントから使うなら `curl` で直に叩いてよい。

| したいこと | CLI | REST |
|---|---|---|
| 状態を見る | `kbridge.cli status` | `GET /healthz` |
| つなぐ / 切る | `connect <url>` | `POST /session` / `DELETE /session` |
| GPU を見る | `gpu` | `GET /gpu` |
| Python を 1 セル実行 | `exec -c "..."` / `exec f.py` | `POST /exec`, `POST /exec/stream` |
| シェルを実行 | `run "nvcc -O2 a.cu -o a"` | `POST /sh`, `POST /sh/stream` |
| ファイルを送る / 取る | `put a.cu src/a.cu` / `get out/w.bin ./w.bin` | `POST /upload` / `GET /download` |
| ディレクトリを丸ごと送る | `sync ./pure work/pure` | （`/upload` の繰り返し） |
| 一覧 / 作成 / 削除 | `ls src` | `GET /ls`, `POST /mkdir`, `POST /rm` |
| **長時間の学習を回す** | `job start "..." --name lpr` | `POST /job` |
| 学習ログを追う | `job log <id> --follow` | `GET /job/{id}/log?offset=N` |
| 学習を止める | `job kill <id>` | `POST /job/{id}/kill` |
| バッチ投入（kaggle CLI） | `batch push <dir>` | `POST /batch/push` ほか |

### 学習は `/exec` ではなく `/job` を使う

`/exec` は HTTP 接続を張ったまま結果を待つので、9〜12 時間の学習には向かない
（ローカルの再起動・回線断・エージェントの再起動でそのまま結果を失う）。
`/job` は Kaggle 側でプロセスを**切り離して**起動し、ログをファイルに落とすので、
呼ぶ側はいつでも増分だけ読みに来られる。

```sh
# 1. コードを送る
python -m kbridge.cli sync ./pure work/pure

# 2. ビルドして学習を始める（すぐ id が返る）
python -m kbridge.cli job start \
  "nvcc -O2 -arch=sm_75 work/pure/dtrain_lpr.cu -o work/train && work/train --epochs 50" \
  --name lpr-train
#   {"id": "20260819-114500-lpr-train", "state": "running", "pid": 1234, ...}

# 3. ログを追う（何度でも、いつでも、途中からでも）
python -m kbridge.cli job log 20260819-114500-lpr-train --follow

# 4. 成果物を回収する
python -m kbridge.cli get work/best.ckpt ./best.ckpt
```

### 実行の中身は Kaggle 側の 1 ファイルに集約してある

`/sh` `/gpu` `/job` の実体は [`kaggle/kbagent.py`](kaggle/kbagent.py)。ローカルサーバは
これをカーネルへ注入して呼ぶだけ。**python 版も cpp 版も同じファイルを注入する**ので、
2 実装の間で挙動がずれない。

---

## 4. Kaggle を使わずに試験する

`tests/fake_jupyter.py` が Kaggle と**同じ URL 形・同じ REST・同じカーネル WebSocket** を
喋る偽サーバなので、GPU 無料枠を 1 秒も使わずに全部通せる。

```sh
sh tests/run_all.sh              # 下の 6 つを順に回す

python tests/e2e.py              # python 版サーバを起動して 37 項目
python tests/e2e.py --impl cpp   # cpp 版サーバで同じ 37 項目
python tests/parity.py           # 両サーバの応答が一致するか（29 項目）
python tests/cli_parity.py       # 両 CLI の出力と終了コードが一致するか（11 項目）
python tests/keepalive.py                # keep-alive が発火するか（6 項目）
python tests/keepalive.py --impl cpp     # cpp 版で同じ 6 項目
```

`tests/parity.py` は「カーネルへ送るコード文字列」も突き合わせる。ここが一致していれば、
Kaggle 側の挙動は `kaggle/kbagent.py` 1 つに収束する（実測でバイト単位一致）。

偽サーバを手で立てることもできる:

```sh
python tests/fake_jupyter.py --port 8899
#   fake jupyter on http://127.0.0.1:8899/k/fake/TESTTOKEN/proxy
```

---

## 5. keep-alive（セッションが 30 分で切れる件）

ローカルサーバは既定で、**最後にカーネルへ話しかけてから 240 秒経つと `pass` を
1 セル実行する**。`--keepalive 0` で無効、`--keepalive 600` などで間隔変更。

```sh
./kbridge_server.exe --port 8787                  # 既定 240 秒
python -m kbridge.server --port 8787 --keepalive 0  # 無効
#   keepalive: every 240s when idle
#   keepalive #1 after 240s idle: ok               <- 発火するとこれが出る
```

なぜ要るか: Kaggle の interactive セッションには idle タイマがある（実測 30 分前後。
公式値の報告は 20 分 / 40 分 / 1 時間とばらつく）。`/job` で切り離した学習を回している
間、呼ぶ側がログを見に来なければ **Kaggle へは 1 バイトも流れない**。それで回収される。
WebSocket のプロトコル ping では代用できない（Kaggle のプロキシ手前で終わるので上流の
帳簿に効かない。そもそも既定の 30 秒 ping は接続を落とすので切ってある）。

### keep-alive の効き方 — まだ検証していない

**カーネル活動だけで Kaggle の idle タイマが戻るかは未確認。** 偽 Jupyter 相手に
「アイドルが続けば execute_request が飛ぶ」ことは `tests/keepalive.py` で確認済みだが、
本物で 40 分放置して生き残るかは測っていない。効かなかった場合の次の手は 2 つ:

* 既知の回避策は[ブラウザ側の DOM クリック](https://greasyfork.org/en/scripts/504382-keep-kaggle-notebook-alive/code)
  で、4〜6 分ごとに Add cell → Run current cell → Cut cell を押す。セル実行だけでなく
  **ノートブック文書の更新**も一緒にやっているのが引っかかる点。Kaggle の活動判定が
  `www.kaggle.com` 側（ログイン Cookie が必要な経路）を見ているなら、jupyter-proxy
  越しの実行では届かない。
* [Commit（Save & Run All）には idle タイマが無い](https://www.kaggle.com/general/232625)。
  ブラウザを閉じても走る。数時間の計算はこちらが本筋で、`/batch/push` がその入口。

あわせて確認すべき落とし穴: セッションが回収されると `/kaggle/working` の中身も消える。
Notebook の Settings → **Persistence** を有効にしていないと、こまめに書いた
チェックポイントも道連れになる。

## 5. 気をつけること

* **URL は認証情報**。`.kbridge.json` は `.gitignore` 済み。ログや issue に貼らない。
* **`DELETE /session` は Kaggle 側のカーネルを落とす。** 既定では Notebook 本体の
  カーネルを再利用しているので、これを叩くと**その Notebook の実行状態（読み込んだ
  データやメモリ上の変数）が消える**。単に kbridge を止めたいだけならサーバを終了すれば
  よく、`DELETE /session` を叩く必要はない。
* サーバは既定で `127.0.0.1` にしか bind しない。それ以外に出すなら `--api-key K` を付けて
  `X-Bridge-Key` を必須にする。
* Kaggle のセッションはブラウザのタブを閉じると止まることがある。`/job` で切り離してあれば
  プロセスは生き延びるが、セッションが落ちた後は kbridge から見に行けない
  （再度 Start Session → `connect` すればログを読み直せる）。
* GPU 無料枠は週 30 時間程度。`/job` を投げっぱなしにして忘れないこと。
* Input（データセット・モデル）の追加は Kaggle UI からしかできない。セッション開始前に揃える。

---

## 6. ライセンス

本体は [LICENSE](LICENSE)。同梱ライブラリは [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)、
同梱物に加えた変更は [cpp/third_party/PATCHES.md](cpp/third_party/PATCHES.md) を参照。
