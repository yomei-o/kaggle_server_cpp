"""kbagent — Kaggle 側（カーネルの中）で動く kbridge のヘルパ。

ローカルの kbridge サーバ（python 版 / cpp 版のどちらも）がこのファイルを
`/kaggle/working/.kbridge/kbagent.py` にアップロードし、カーネル上で

    import kbagent; print(json.dumps(kbagent.start(cmd="...", name="lpr-train")))

のように呼ぶ。**ジョブの意味論はこのファイル 1 つにしかない**ので、2 実装間で
挙動がずれない（spec/API.md 6. パリティ規約）。

中身は 3 つ:
  * ジョブ  — 切り離したプロセスで長時間の学習を回し、ログをファイルに落とす
  * シェル  — 1 発のコマンドを終了コード付きで実行する（/sh）
  * GPU     — nvidia-smi と torch から GPU の状態を取る（/gpu）

なぜセルを直接回さずプロセスを切り離すのか:
Jupyter の実行はセルを投げた側が結果を待つ形なので、9〜12 時間の学習だと
ローカルの再起動・回線断・エージェントの再起動でそのまま結果を失う。ここでは
setsid でプロセスグループを分けて起動し、ログをファイルに落とすので、
呼ぶ側はいつでも「増分だけ」読みに来られる。

各関数は **JSON 文字列を 1 行で print できる形（dict）で返す**。呼び出し側は
`print(json.dumps(kbagent.xxx(...)))` の標準出力を読む。
"""

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time

VERSION = "0.1.0"   # ローカル側はこの値を見て、古ければ上書きアップロードする

# 本番は Kaggle（Linux）でしか動かないが、tests/fake_jupyter.py で
# Windows のローカル試験にも同じファイルを掛けたいので、
# プロセスの切り離し方と bash の場所だけ環境を見て変える。
POSIX = os.name == "posix"
BASH = shutil.which("bash") or "/bin/bash"
STDBUF = shutil.which("stdbuf")

ROOT = os.environ.get("KBRIDGE_ROOT", "/kaggle/working/.kbridge")
JOBS = os.path.join(ROOT, "jobs")


def _ensure():
    os.makedirs(JOBS, exist_ok=True)


def _meta_path(job_id):
    return os.path.join(JOBS, job_id + ".json")


def _log_path(job_id):
    return os.path.join(JOBS, job_id + ".log")


def _exit_path(job_id):
    return os.path.join(JOBS, job_id + ".exit")


def _read_meta(job_id):
    with open(_meta_path(job_id), "r", encoding="utf-8") as f:
        return json.load(f)


def _write_meta(meta):
    tmp = _meta_path(meta["id"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f)
    os.replace(tmp, _meta_path(meta["id"]))


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _slug(name):
    keep = "-_."
    s = "".join(c if (c.isalnum() or c in keep) else "-" for c in (name or "job"))
    return s.strip("-") or "job"


def start(cmd=None, code=None, name="job", cwd="/kaggle/working", env=None):
    """ジョブを起動する。cmd（シェル）か code（Python）のどちらか一方を渡す。

    プロセスグループを分けて起動するので、カーネルを再起動しても走り続ける。
    終了コードはラッパが `<id>.exit` に書く（プロセスが消えてから状態が分かる）。
    """
    _ensure()
    if (cmd is None) == (code is None):
        raise ValueError("give exactly one of cmd= or code=")

    job_id = "%s-%s" % (time.strftime("%Y%m%d-%H%M%S", time.localtime()), _slug(name))
    # 同一秒に複数投げられても衝突しないようにする
    n = 0
    while os.path.exists(_meta_path(job_id)):
        n += 1
        job_id = "%s-%s-%d" % (time.strftime("%Y%m%d-%H%M%S", time.localtime()),
                               _slug(name), n)

    log = _log_path(job_id)
    exitf = _exit_path(job_id)

    if code is not None:
        script = os.path.join(JOBS, job_id + ".py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(code)
        inner = "python -u %s" % shlex.quote(script)
    else:
        inner = cmd

    # ラッパ: 実体を回して、終了コードを必ず記録する。
    # 行バッファ(stdbuf -oL -eL)にしておかないと、ログが数分単位で固まって見える。
    runner = os.path.join(JOBS, job_id + ".sh")
    with open(runner, "w", encoding="utf-8") as f:
        f.write("#!/bin/bash\n")
        f.write("cd %s\n" % shlex.quote(cwd))
        f.write("%s%s -c %s > %s 2>&1\n"
                % ("stdbuf -oL -eL " if STDBUF else "",
                   shlex.quote(BASH), shlex.quote(inner), shlex.quote(log)))
        f.write("echo $? > %s\n" % shlex.quote(exitf))
    os.chmod(runner, 0o755)

    open(log, "wb").close()
    run_env = dict(os.environ)
    if env:
        run_env.update({str(k): str(v) for k, v in env.items()})

    detach = {"start_new_session": True} if POSIX else {
        # Windows でも同じ意味（親が死んでも道連れにしない）にしておく
        "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)}
    p = subprocess.Popen(
        [BASH, runner],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, cwd=cwd, env=run_env, **detach)

    meta = {"id": job_id, "name": name, "cmd": cmd, "has_code": code is not None,
            "cwd": cwd, "pid": p.pid, "started": time.time(), "ended": None,
            "exit_code": None, "log": os.path.relpath(log, os.path.dirname(ROOT))}
    _write_meta(meta)
    return status(job_id)


def _refresh(meta):
    """終了コードファイルとプロセスの生死から state を決める。"""
    job_id = meta["id"]
    if meta.get("exit_code") is None and os.path.exists(_exit_path(job_id)):
        try:
            with open(_exit_path(job_id)) as f:
                meta["exit_code"] = int(f.read().strip() or "-1")
            meta["ended"] = os.path.getmtime(_exit_path(job_id))
            _write_meta(meta)
        except (ValueError, OSError):
            pass

    if meta.get("killed"):
        state = "killed"
    elif meta.get("exit_code") is None:
        state = "running" if _alive(meta["pid"]) else "lost"
    else:
        state = "done" if meta["exit_code"] == 0 else "failed"

    log = _log_path(job_id)
    out = dict(meta)
    out["state"] = state
    out["log_size"] = os.path.getsize(log) if os.path.exists(log) else 0
    out.pop("killed", None)
    return out


def status(job_id):
    _ensure()
    return _refresh(_read_meta(job_id))


def ls():
    _ensure()
    out = []
    for fn in sorted(os.listdir(JOBS)):
        if fn.endswith(".json"):
            try:
                out.append(_refresh(_read_meta(fn[:-5])))
            except (OSError, ValueError):
                pass
    return out


def log(job_id, offset=0, max_bytes=65536):
    """ログの増分を返す。next_offset を持って呼び直せば続きが読める。"""
    _ensure()
    st = _refresh(_read_meta(job_id))
    path = _log_path(job_id)
    size = os.path.getsize(path) if os.path.exists(path) else 0
    offset = max(0, min(int(offset), size))
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(int(max_bytes))
    nxt = offset + len(data)
    return {"id": job_id, "offset": offset, "next_offset": nxt,
            "eof": nxt >= size and st["state"] != "running",
            "state": st["state"], "log_size": size,
            "data": data.decode("utf-8", "replace")}


def kill(job_id, sig=signal.SIGTERM):
    _ensure()
    meta = _read_meta(job_id)
    try:
        if POSIX:
            os.killpg(os.getpgid(meta["pid"]), sig)  # ラッパごと・子プロセスごと止める
        else:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(meta["pid"])],
                           capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass
    meta["killed"] = True
    _write_meta(meta)
    return _refresh(meta)


def rm(job_id):
    """メタ・ログ・ラッパを消す（走っているジョブは消さない）。"""
    _ensure()
    st = _refresh(_read_meta(job_id))
    if st["state"] == "running":
        raise RuntimeError("job is running; kill it first: %s" % job_id)
    for p in (_meta_path(job_id), _log_path(job_id), _exit_path(job_id),
              os.path.join(JOBS, job_id + ".sh"), os.path.join(JOBS, job_id + ".py")):
        try:
            os.remove(p)
        except OSError:
            pass
    return {"id": job_id, "removed": True}


# ---------------------------------------------------------------- シェル (/sh)

EXIT_MARKER = "__KBRIDGE_EXIT__"


def sh(cmd, cwd="/kaggle/working", timeout=None):
    """コマンドを実行し、出力をそのまま標準出力へ流したうえで、最後に

        __KBRIDGE_EXIT__ {"exit_code": 0, "timeout": false}

    の 1 行を出す。呼び出し側はこの最終行を剥がして exit_code を取る。
    出力を素通しするので、/sh/stream でも同じコードがそのまま使える。
    """
    timed_out = False
    p = subprocess.Popen([BASH, "-lc", cmd], cwd=cwd,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, bufsize=1,
                         universal_newlines=True, errors="replace")
    try:
        for line in p.stdout:
            print(line, end="", flush=True)
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        p.kill()
        p.wait()
    finally:
        try:
            p.stdout.close()
        except Exception:
            pass
    print("\n%s %s" % (EXIT_MARKER,
                       json.dumps({"exit_code": p.returncode, "timeout": timed_out})),
          flush=True)
    return p.returncode


# -------------------------------------------------------------------- GPU (/gpu)

def gpu():
    """nvidia-smi と torch から GPU の状態を取る。GPU 無しでも例外にしない。"""
    out = {"gpus": [], "driver": "", "cuda": "", "torch": "", "torch_cuda": False,
           "raw": ""}
    q = ("index,name,memory.total,memory.used,utilization.gpu,temperature.gpu,"
         "driver_version")
    try:
        raw = subprocess.run(
            ["nvidia-smi", "--query-gpu=" + q, "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=30)
        out["raw"] = (raw.stdout or "") + (raw.stderr or "")
        for line in (raw.stdout or "").strip().splitlines():
            f = [c.strip() for c in line.split(",")]
            if len(f) < 7:
                continue
            out["gpus"].append({"index": int(f[0]), "name": f[1],
                                "mem_total_mb": int(float(f[2])),
                                "mem_used_mb": int(float(f[3])),
                                "util_pct": int(float(f[4])),
                                "temp_c": int(float(f[5]))})
            out["driver"] = f[6]
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        out["raw"] = "nvidia-smi: %s" % e

    try:
        v = subprocess.run(["nvcc", "--version"], capture_output=True, text=True,
                           timeout=30).stdout
        m = re.search(r"release ([0-9.]+)", v or "")
        if m:
            out["cuda"] = m.group(1)
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        import torch
        out["torch"] = torch.__version__
        out["torch_cuda"] = bool(torch.cuda.is_available())
        if not out["cuda"]:
            out["cuda"] = getattr(torch.version, "cuda", "") or ""
    except Exception:
        pass
    return out


if __name__ == "__main__":  # 手で動かして確かめる用
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "gpu":
        print(json.dumps(gpu(), indent=2))
    else:
        print(json.dumps(ls() if len(sys.argv) < 2 else status(sys.argv[1]), indent=2))
