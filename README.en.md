# kaggle_server_cpp — Use Kaggle's GPUs through a local HTTP API (kbridge)

*[日本語版 README](README.md) / English*

A Kaggle Notebook can hand you a **"VSCode Compatible URL"** — the endpoint of the Jupyter Server
that Kaggle hosts for your session. Instead of pointing VS Code's Jupyter extension at it, kbridge
points **your own local server** at it. That lets Claude Code, `curl`, or anything else that speaks
HTTP build, run, and train on Kaggle's free GPUs (T4×2 / P100).

```
Claude Code / curl / kbridge CLI
        │  HTTP  127.0.0.1:8787          ← this repository (the local server)
        ▼
   kbridge local server   ── two peer implementations: python (FastAPI) and cpp (cpp-httplib)
        │  HTTPS REST + WSS (Jupyter 5.3 protocol, implemented from scratch)
        ▼
   Kaggle Jupyter Server (GPU session)
```

**The core design rule: Python and C++ are peers.** Same URLs, same JSON, same behavior in both
languages, with `tests/parity.py` enforcing zero divergence. [`spec/API.md`](spec/API.md) is the
authority. No feature exists in only one of the two (same policy as the sister repository
[yolo_lpr_cpp](https://github.com/yomei-o/yolo_lpr_cpp)).

Current status and remaining work: [RESUME.md](RESUME.md) (Japanese).

---

## 1. Getting started (5 minutes)

### 1.0 What you need first (credentials)

There are two separate credentials, depending on what you want to do.
**The main path (Jupyter Server) needs no API token.**

| What you want | What you need | How |
|---|---|---|
| Run via Jupyter Server (kbridge's main path) | **No API token.** The VSCode Compatible URL itself carries the auth token | Step 1.1 |
| Batch submission (`/batch/*` = `kaggle kernels push`) | Kaggle API token | Below |

Prerequisite: **your account must be phone-verified to use a GPU** (without it, a Notebook's
Accelerator and Internet options are unavailable). kaggle.com → icon in the top right → Settings →
Phone verification. Then in the Notebook's right panel, Settings → Accelerator, pick `GPU T4 x2`
or `P100`. The free quota is **30 hours per week**; one session runs at most 9 hours (12 hours
CPU-only).

Getting an API token (only needed for batch):

1. kaggle.com → icon in the top right → **Settings** → **API** → **Create New Token**
2. There are two formats. **The new format (starting with `KGAT_`) can only be used via an
   environment variable**:

```sh
# new format
export KAGGLE_API_TOKEN=KGAT_xxxxxxxxxxxxxxxxxxxx     # Windows: setx KAGGLE_API_TOKEN KGAT_...

# old format (a kaggle.json is downloaded)
mkdir -p ~/.kaggle && mv ~/Downloads/kaggle.json ~/.kaggle/
chmod 600 ~/.kaggle/kaggle.json
```

3. Verify: `kaggle kernels list -m` should succeed (requires `pip install kaggle`).

kbridge neither reads nor stores your token. `/batch/*` just shells out to the `kaggle` command on
your PATH, so authentication follows that CLI's own conventions.

### 1.1 Start a session on Kaggle and get the URL

1. Open a Notebook on Kaggle (for GPU, set Settings → Accelerator to a GPU)
2. Menu: **Run → Kaggle Jupyter Server → Start Session**
3. Copy the **"VSCode Compatible URL"** that appears. It looks like this:

```
https://kkb-production.jupyter-proxy.kaggle.net/k/<id>/<token>/proxy
```

> This URL **is** the credential — the token sits in the path. Don't show it to anyone, don't commit
> it. kbridge never emits the token in responses or logs (it masks it as `****`).
> This step is the one thing that can't be automated; do it by hand.

### 1.2 Start the local server

```sh
# python
pip install -r python/requirements.txt
cd python && python -m kbridge.server --port 8787

# cpp (see section 2 for building)
./kbridge_server.exe --port 8787
```

### 1.3 Connect and run something

```sh
cd python
python -m kbridge.cli connect "https://kkb-production.jupyter-proxy.kaggle.net/k/xxx/yyy/proxy"
python -m kbridge.cli gpu
#   {"ok": true, "gpus": [{"index": 0, "name": "Tesla T4", "mem_total_mb": 15360, ...}], ...}

python -m kbridge.cli run "nvcc --version"
python -m kbridge.cli exec -c "import torch; print(torch.cuda.get_device_name(0))"
```

The URL you pass to `connect` is saved to `.kbridge.json` (already in `.gitignore`), and the server
picks it up automatically on later starts. The environment variable `KAGGLE_JUPYTER_URL` works too.

---

## 2. Building (C++)

No external libraries to install — everything needed is vendored under `cpp/third_party/`
(cpp-httplib 0.46.1 / nlohmann-json 3.11.3 / mbedTLS 3.6.4, header-only).

```sh
sh cpp/build/gcc.sh cpp/kbridge_server.cpp -o kbridge_server.exe   # mingw (w64devkit)
sh cpp/build/cc.sh  cpp/kbridge_server.cpp -o kbridge_server.exe   # MSVC (no vcvars needed)
```

All of mbedTLS lands in a single translation unit, so a build takes 20–45 seconds.

To check that your toolchain is intact, start here:

```sh
sh cpp/build/testcert/gen.sh                                       # generate a self-signed cert
sh cpp/build/gcc.sh cpp/build/ssl_smoke.cpp -o ssl_smoke.exe && ./ssl_smoke.exe
#   [ OK ] https client -> www.kaggle.com (verify on, system roots)
#   [ OK ] https server <- https client (verify on)
#   [ OK ] https client rejects untrusted cert
#   [ OK ] wss websocket echo (verify on)
#   [ OK ] http server <- http client (loopback)
#   [ OK ] json roundtrip
```

---

## 3. What it can do (summary of `spec/API.md`)

The CLI and the REST API map one-to-one. From an agent, calling `curl` directly is perfectly fine.

| What you want | CLI | REST |
|---|---|---|
| Check status | `kbridge.cli status` | `GET /healthz` |
| Connect / disconnect | `connect <url>` | `POST /session` / `DELETE /session` |
| Inspect GPUs | `gpu` | `GET /gpu` |
| Run one Python cell | `exec -c "..."` / `exec f.py` | `POST /exec`, `POST /exec/stream` |
| Run a shell command | `run "nvcc -O2 a.cu -o a"` | `POST /sh`, `POST /sh/stream` |
| Send / fetch a file | `put a.cu src/a.cu` / `get out/w.bin ./w.bin` | `POST /upload` / `GET /download` |
| Send a whole directory | `sync ./pure work/pure` | (repeated `/upload`) |
| List / create / remove | `ls src` | `GET /ls`, `POST /mkdir`, `POST /rm` |
| **Run long training** | `job start "..." --name lpr` | `POST /job` |
| Follow training logs | `job log <id> --follow` | `GET /job/{id}/log?offset=N` |
| Stop training | `job kill <id>` | `POST /job/{id}/kill` |
| Batch submission (kaggle CLI) | `batch push <dir>` | `POST /batch/push`, etc. |

### For training, use `/job`, not `/exec`

`/exec` holds the HTTP connection open while waiting for the result, which is a poor fit for a 9–12
hour training run (a local restart, a dropped connection, or an agent restart loses the result).
`/job` starts the process **detached** on the Kaggle side and writes logs to a file, so the caller
can come back at any time and read only the new bytes.

```sh
# 1. send the code
python -m kbridge.cli sync ./pure work/pure

# 2. build and start training (returns an id immediately)
python -m kbridge.cli job start \
  "nvcc -O2 -arch=sm_75 work/pure/dtrain_lpr.cu -o work/train && work/train --epochs 50" \
  --name lpr-train
#   {"id": "20260819-114500-lpr-train", "state": "running", "pid": 1234, ...}

# 3. follow the log (as often as you like, whenever, resuming from anywhere)
python -m kbridge.cli job log 20260819-114500-lpr-train --follow

# 4. collect the artifacts
python -m kbridge.cli get work/best.ckpt ./best.ckpt
```

### All the remote-side logic lives in one file on Kaggle

`/sh`, `/gpu`, and `/job` are implemented in [`kaggle/kbagent.py`](kaggle/kbagent.py). The local
server just injects it into the kernel and calls it. **Both the python and cpp servers inject the
exact same file**, so the two implementations can't drift apart.

---

## 4. Testing without Kaggle

`tests/fake_jupyter.py` is a fake server that speaks **the same URL shape, the same REST, and the
same kernel WebSocket** as Kaggle, so the whole suite runs without spending a single second of the
free GPU quota.

```sh
sh tests/run_all.sh              # runs the four below in order

python tests/e2e.py              # starts the python server, 37 checks
python tests/e2e.py --impl cpp   # same 37 checks against the cpp server
python tests/parity.py           # do both servers respond identically? (29 checks)
python tests/cli_parity.py       # do both CLIs match in output and exit code? (11 checks)
```

`tests/parity.py` also diffs the *code strings sent to the kernel*. As long as those match, behavior
on the Kaggle side converges on the single `kaggle/kbagent.py` (byte-for-byte identical in practice).

You can also bring the fake server up by hand:

```sh
python tests/fake_jupyter.py --port 8899
#   fake jupyter on http://127.0.0.1:8899/k/fake/TESTTOKEN/proxy
```

---

## 5. Things to watch out for

* **The URL is a credential.** `.kbridge.json` is in `.gitignore`. Don't paste it into logs or issues.
* **`DELETE /session` kills the kernel on the Kaggle side.** By default kbridge reuses the
  Notebook's own kernel, so calling this **wipes that Notebook's execution state** (loaded data,
  in-memory variables). If you just want to stop kbridge, exit the server — there's no need to call
  `DELETE /session`.
* The server binds to `127.0.0.1` only by default. To expose it anywhere else, pass `--api-key K`
  so that `X-Bridge-Key` becomes mandatory.
* A Kaggle session can stop when you close the browser tab. A process detached via `/job` survives,
  but once the session is gone kbridge can't reach it (Start Session again, then `connect`, and you
  can re-read the logs).
* The free GPU quota is about 30 hours per week. Don't fire off a `/job` and forget about it.
* Inputs (datasets, models) can only be attached from the Kaggle UI. Get them in place before
  starting the session.

---

## 6. License

The project itself: [LICENSE](LICENSE). Bundled libraries: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
Changes made to the bundled sources: [cpp/third_party/PATCHES.md](cpp/third_party/PATCHES.md).
