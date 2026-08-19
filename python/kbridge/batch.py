"""バッチ実行（kaggle CLI のフォールバック）。

Jupyter セッションはブラウザで開いている間だけの対話用で、切れれば実行も止まる。
確実に 9〜12 時間回したいときは `kaggle kernels push` で投げっぱなしにするほうが向く。

認証は kaggle CLI 側に任せる（新形式の環境変数 KAGGLE_API_TOKEN=KGAT_... か
従来の ~/.kaggle/kaggle.json）。kbridge はトークンを読まないし持たない。

C++ 版 cpp/pure/batch.hpp も同じコマンド列・同じ応答を返す。
"""

import json
import os
import shutil
import subprocess


class BatchUnavailable(Exception):
    """kaggle CLI が無い。エラーではなく ok:false として返す。"""


def kaggle_path():
    return shutil.which("kaggle")


def _run(args, cwd=None, timeout=600.0):
    exe = kaggle_path()
    if not exe:
        raise BatchUnavailable("kaggle CLI not found (pip install kaggle)")
    p = subprocess.run([exe] + list(args), cwd=cwd, capture_output=True,
                       text=True, errors="replace", timeout=timeout)
    return {"exit_code": p.returncode,
            "raw": ((p.stdout or "") + (p.stderr or "")).strip()}


def push(directory, timeout=600.0):
    if not os.path.isdir(directory):
        return {"ok": False, "error": "no such directory: %s" % directory}
    r = _run(["kernels", "push", "-p", directory], timeout=timeout)
    r["ok"] = r["exit_code"] == 0
    r["dir"] = directory
    if not r["ok"]:
        r["error"] = r["raw"][-500:] or "kaggle kernels push failed"
    return r


def status(kernel_id, timeout=300.0):
    r = _run(["kernels", "status", kernel_id], timeout=timeout)
    raw = r["raw"]
    state = "unknown"
    for word in ("complete", "running", "error", "cancelAcknowledged", "queued"):
        if word.lower() in raw.lower():
            state = "complete" if word == "complete" else word
            break
    r["ok"] = r["exit_code"] == 0
    r["id"] = kernel_id
    r["state"] = state
    if not r["ok"]:
        r["error"] = raw[-500:] or "kaggle kernels status failed"
    return r


def output(kernel_id, directory, timeout=1800.0):
    os.makedirs(directory, exist_ok=True)
    r = _run(["kernels", "output", kernel_id, "-p", directory], timeout=timeout)
    r["ok"] = r["exit_code"] == 0
    r["id"] = kernel_id
    r["dir"] = directory
    r["files"] = sorted(os.listdir(directory)) if os.path.isdir(directory) else []
    if not r["ok"]:
        r["error"] = r["raw"][-500:] or "kaggle kernels output failed"
    return r


def pull(kernel_id, directory, timeout=600.0):
    """記事の /kaggle-kernel-pull 相当。metadata も取り、run_on_push を false にする。"""
    os.makedirs(directory, exist_ok=True)
    r = _run(["kernels", "pull", kernel_id, "-p", directory, "--metadata"],
             timeout=timeout)
    r["ok"] = r["exit_code"] == 0
    r["id"] = kernel_id
    r["dir"] = directory
    meta_path = os.path.join(directory, "kernel-metadata.json")
    if r["ok"] and os.path.exists(meta_path):
        # push のたびに勝手に実行されると無料枠を溶かすので、既定で止めておく
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        if meta.get("run_on_push") is not False:
            meta["run_on_push"] = False
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
            r["run_on_push_set_false"] = True
        r["sources"] = {k: meta.get(k, []) for k in
                        ("dataset_sources", "kernel_sources",
                         "competition_sources", "model_sources")}
    r["files"] = sorted(os.listdir(directory)) if os.path.isdir(directory) else []
    if not r["ok"]:
        r["error"] = r["raw"][-500:] or "kaggle kernels pull failed"
    return r
