# これを読んでいる AI エージェントへ — あなたは GPU を使えます

Claude Code / Codex / その他のコーディングエージェントが、**Kaggle の無料 GPU（Tesla T4 ×2）でビルド・
学習・推論を回すための手順**。ローカルに GPU が無くても、`curl` だけで CUDA が使えます。

このファイルは実測ベースです（2026-08-19 に yolo_lpr_cpp の学習で実際に通した手順そのまま）。

---

## 0. 人間にやってもらうこと（1 分・これだけは自動化できない）

1. kaggle.com で Notebook を開く（GPU を使うなら右パネル Settings → Accelerator を **GPU T4 x2**）
2. メニュー **Run → Kaggle Jupyter Server → Start Session**
3. 出てくる **"VSCode Compatible URL"** をエージェントに渡す

```
https://kkb-production.jupyter-proxy.kaggle.net/k/<id>/<token>/proxy
```

> この URL は**それ自体が認証情報**です。ログに出さない・コミットしない。kbridge は応答でもログでも
> `****` にマスクします。
>
> 前提: GPU を使うには Kaggle アカウントの電話番号認証が必要。無料枠は **週 30 時間**、
> 1 セッション最長 9 時間（CPU のみなら 12 時間）。

## 1. エージェントがやること（3 コマンド）

```sh
git clone https://github.com/yomei-o/kaggle_server_cpp.git && cd kaggle_server_cpp

# サーバを起動（C++ 版。python 版は python -m kbridge.server --port 8787 でも同じ）
sh cpp/build/gcc.sh cpp/kbridge_server.cpp -o kbridge_server.exe   # 初回だけ 20-45 秒
./kbridge_server.exe --port 8787 &

# 繋ぐ
curl -s -X POST localhost:8787/session -H 'Content-Type: application/json' \
  -d '{"url":"<VSCode Compatible URL>"}'
curl -s localhost:8787/gpu
```

`/gpu` の実際の応答（Kaggle 無料枠）:

```json
{"cuda":"12.8","driver":"580.159.04","torch":"2.10.0+cu128","torch_cuda":true,
 "gpus":[{"index":0,"name":"Tesla T4","mem_total_mb":15360},
         {"index":1,"name":"Tesla T4","mem_total_mb":15360}]}
```

## 2. 使い分け（ここを間違えると時間を失う）

| やること | 使う API | 理由 |
|---|---|---|
| 短いコマンド（ビルド、`ls`、確認） | `POST /sh` | 同期。数分までならこれで十分 |
| Python を 1 セル | `POST /exec` | 同上 |
| **学習・長時間の処理** | `POST /job` → `GET /job/{id}/log?offset=N` | **Kaggle 側で切り離して起動**しログをファイルに落とす。ローカルを再起動しても回線が切れてもエージェントが再起動しても、ログと結果は残る |
| ファイル送受 | `POST /upload` / `GET /download` | 大きいものは Kaggle 側で `git clone` するほうが速い |

**`/exec` で 30 分の学習を回さないこと。** HTTP 接続を張ったまま待つので、切れたら結果を失います。

```sh
# 学習を始める（すぐ id が返る）
curl -s -X POST localhost:8787/job -H 'Content-Type: application/json' -d '{
  "name":"train",
  "cmd":"cd /kaggle/working/myrepo && python train.py --steps 4000 --batch 64"}'
# -> {"id":"20260819-043551-train","state":"running","pid":...}

# ログを増分で読む（何度でも、いつでも）
curl -s "localhost:8787/job/20260819-043551-train/log?offset=0"

# 止める / 成果物を取る
curl -s -X POST localhost:8787/job/<id>/kill
curl -s "localhost:8787/download?path=myrepo/models/best.onnx" -o best.onnx
```

## 3. Kaggle 側の環境（2026-08 実測）

| | |
|---|---|
| GPU | Tesla T4 ×2（各 15 GB）、CUDA 12.8、driver 580 |
| CPU | **4 vCPU**（ここが一番の制約。データローダは必ず並列化する） |
| torch | 2.10.0+cu128（CUDA 有効）、torchvision 0.25 |
| ディスク | `/kaggle/working` に 20 GB |
| コンパイラ | g++ 11.4 / nvcc（C++20 は通る） |
| インターネット | **通る**（Notebook の Settings で Internet ON にしてあれば。`git clone`・`pip install` OK） |
| 作業ディレクトリ | `/kaggle/working`（`/kaggle/input` は読み取り専用のデータセット） |

実測した落とし穴:

- **CPU が 4 コアしかない。** GPU 利用率が 28% なら、それは GPU が遅いのではなく**データローダが
  詰まっている**。画像デコードをスレッドプールに投げるだけで数倍変わる。
- **`pip install` で torch を巻き込むな。** 例えば ultralytics を入れるなら
  `pip install --no-deps ultralytics ultralytics-thop py-cpuinfo`。素で入れると torch を差し替えられて
  CUDA が壊れることがある。
- **`git clone` は履歴が重い。** `--depth 1` を付ける（それでも 1.3 GB のリポジトリはある）。
- 生成・前処理を GPU 学習と**同時に**回すなら `nice -n 19` を付ける（4 コアの取り合いになる）。
- 数値計算は fp32 なら CPU と GPU で結果が一致しない。学習の再現性を見たいなら
  「同じ入力で 1 step 目の loss が一致するか」で見る（それ以降は最適化器で発散していく）。

## 4. Kaggle を使わずに試す

無料枠を 1 秒も使わずに全部通せる偽サーバが入っています。エージェントが手順を確かめるならまずこれ。

```sh
sh tests/run_all.sh     # python 版 / cpp 版の e2e（各 37 項目）＋ 2 実装のパリティ（29+11 項目）
```

## 5. 実例（このツールで実際にやった学習）

姉妹リポジトリ [yolo_lpr_cpp](https://github.com/yomei-o/yolo_lpr_cpp)（日本のナンバープレート認識）の
認識器を、この経路で Kaggle GPU で学習しました。手順はそのリポジトリの README「Kaggle の GPU で学習する」
にあります。実測:

- 合成データ生成 32,000 枚: 4 コアで約 15 分（`/job` で投げて放置）
- 認識器の学習 4,000 step（batch 64）: T4 で約 25 分。ローカル CPU の 40 倍以上
- 実データ hold-out の精度が 91.7% → **97.9%**
- ローカルへは ONNX（1.2 MB）を `/download` で回収するだけ

---

要点はひとつだけです: **人間に URL を 1 つもらえば、あとはエージェントが `curl` で GPU を使える。**
長い処理は `/job`、ログは `offset` で増分読み、成果物は `/download`。
