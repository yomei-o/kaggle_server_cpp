# To the AI agent reading this — you can use a GPU

How a coding agent (Claude Code, Codex, anything that can run `curl`) gets **CUDA on Kaggle's free
Tesla T4 x2**: build, train, evaluate, and pull the artefacts back — with no local GPU.

Everything here is measured, not guessed: it is the exact path used on 2026-08-19 to train the
recogniser in [yolo_lpr_cpp](https://github.com/yomei-o/yolo_lpr_cpp).

*[日本語版 / Japanese](FOR_AGENTS.md)*

---

## 0. What a human has to do (one minute — this part cannot be automated)

1. Open a Notebook on kaggle.com (for GPU: right panel → Settings → Accelerator → **GPU T4 x2**)
2. Menu **Run → Kaggle Jupyter Server → Start Session**
3. Hand the agent the **"VSCode Compatible URL"**:

```
https://kkb-production.jupyter-proxy.kaggle.net/k/<id>/<token>/proxy
```

> That URL **is** the credential. Never log it, never commit it. kbridge masks it as `****`
> everywhere.
>
> Prerequisites: Kaggle needs a phone-verified account before it offers GPU/Internet. Free quota is
> **30 h/week**, one session runs at most 9 h (12 h CPU-only).

## 1. What the agent does (three commands)

```sh
git clone https://github.com/yomei-o/kaggle_server_cpp.git && cd kaggle_server_cpp

# start the local bridge (C++ build; the Python one is `python -m kbridge.server --port 8787`)
sh cpp/build/gcc.sh cpp/kbridge_server.cpp -o kbridge_server.exe   # 20-45 s, first time only
./kbridge_server.exe --port 8787 &

curl -s -X POST localhost:8787/session -H 'Content-Type: application/json' \
  -d '{"url":"<VSCode Compatible URL>"}'
curl -s localhost:8787/gpu
```

Real response from Kaggle's free tier:

```json
{"cuda":"12.8","driver":"580.159.04","torch":"2.10.0+cu128","torch_cuda":true,
 "gpus":[{"index":0,"name":"Tesla T4","mem_total_mb":15360},
         {"index":1,"name":"Tesla T4","mem_total_mb":15360}]}
```

## 2. Which endpoint to use (getting this wrong costs you hours)

| Task | Endpoint | Why |
|---|---|---|
| Short commands (build, `ls`, checks) | `POST /sh` | synchronous; fine up to a few minutes |
| One Python cell | `POST /exec` | same |
| **Training / anything long** | `POST /job` then `GET /job/{id}/log?offset=N` | the process is **detached on the Kaggle side** and logs to a file. Your machine can reboot, your connection can drop, *you* can restart — the log and the result survive |
| Files | `POST /upload` / `GET /download` | for anything big, `git clone` on the Kaggle side instead |

**Do not run a 30-minute training through `/exec`.** It holds the HTTP connection open; if it drops,
the result is gone.

```sh
curl -s -X POST localhost:8787/job -H 'Content-Type: application/json' -d '{
  "name":"train",
  "cmd":"cd /kaggle/working/myrepo && python train.py --steps 4000 --batch 64"}'
# -> {"id":"20260819-043551-train","state":"running","pid":...}

curl -s "localhost:8787/job/20260819-043551-train/log?offset=0"
curl -s -X POST localhost:8787/job/<id>/kill
curl -s "localhost:8787/download?path=myrepo/models/best.onnx" -o best.onnx
```

## 3. What the Kaggle side actually is (measured, 2026-08)

| | |
|---|---|
| GPU | Tesla T4 x2 (15 GB each), CUDA 12.8, driver 580 |
| CPU | **4 vCPU** — this, not the GPU, is your bottleneck |
| torch | 2.10.0+cu128 (CUDA on), torchvision 0.25 |
| Disk | 20 GB under `/kaggle/working` |
| Toolchain | g++ 11.4, nvcc; C++20 compiles |
| Internet | **yes**, if the Notebook has Internet enabled (`git clone`, `pip install` both work) |
| Workdir | `/kaggle/working` (`/kaggle/input` is read-only datasets) |

Traps that cost real time:

- **Only 4 cores.** GPU utilisation at 28% does not mean the GPU is slow — it means your data loader
  is starving it. Moving image decoding into a thread pool changed throughput several-fold.
- **Don't let `pip` touch torch.** To add ultralytics:
  `pip install --no-deps ultralytics ultralytics-thop py-cpuinfo`. A plain install can swap torch and
  break CUDA.
- **`git clone --depth 1`.** History is heavy (one dataset repo here is 1.3 GB even shallow).
- Running generation *while* training? Prefix it with `nice -n 19`; four cores go fast.
- fp32 results never match bit-for-bit between CPU and GPU. To check a port, compare the **first**
  step's loss on identical input; after that the optimiser amplifies the difference.

## 4. Try it without spending quota

A fake Jupyter server that speaks the same URL shape, REST and kernel WebSocket ships with the repo,
so an agent can rehearse the whole flow without burning a second of the free tier:

```sh
sh tests/run_all.sh    # e2e for both the Python and C++ servers, plus their parity suites
```

## 5. A worked example

The recogniser of [yolo_lpr_cpp](https://github.com/yomei-o/yolo_lpr_cpp) (Japanese licence plates)
was trained through exactly this path; its README has the commands. Measured:

- generating 32,000 synthetic crops: ~15 min on the 4 cores, fired off as a `/job`
- 4,000 training steps at batch 64: ~25 min on one T4 — over 40x the local CPU
- real hold-out accuracy 91.7% → **97.9%**
- the artefact pulled home is a 1.2 MB ONNX via `/download`

---

One sentence: **get one URL from a human, and your agent has a GPU.** Long work goes through `/job`,
logs are read incrementally by `offset`, artefacts come back with `/download`.
