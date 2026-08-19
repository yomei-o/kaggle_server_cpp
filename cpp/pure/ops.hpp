// kbridge の「操作」層 — Jupyter クライアントの上に /sh, /gpu, /job を組み立てる。
//
// Kaggle 側の実体は kaggle/kbagent.py 1 ファイル。ここはそれを
//   * 必要なら（未注入 or 版ズレ）カーネルへ注入し、
//   * `kbagent.xxx(**引数)` を実行し、
//   * 標準出力の JSON を拾う
// だけの薄い層。python/kbridge/ops.py と**同じコード文字列**を作ること。
// 引数を JSON(base64) で運ぶのは、両言語でエスケープの流儀を合わせずに
// 完全に同じ 1 行を作るため。
#pragma once

#include <algorithm>
#include <fstream>
#include <string>

#include "json.hpp"

#include "b64.hpp"
#include "jupyter.hpp"

namespace kbridge {
namespace ops {

inline const char *AGENT_VERSION = "0.1.0";
inline const char *EXIT_MARKER = "__KBRIDGE_EXIT__";

inline const char *CHECK_CODE =
    "import sys, json\n"
    "v = ''\n"
    "try:\n"
    "    import kbagent\n"
    "    v = getattr(kbagent, 'VERSION', '')\n"
    "except Exception:\n"
    "    pass\n"
    "print(json.dumps({'version': v}))\n";

// kaggle/kbagent.py の在り処。--agent で上書きできる。
inline std::string &agent_path() {
  static std::string p;
  return p;
}

inline std::string find_agent_path() {
  if (!agent_path().empty()) { return agent_path(); }
  // 実行ディレクトリからの相対で探す（リポジトリのどこから起動しても拾えるように）
  static const char *candidates[] = {"kaggle/kbagent.py", "../kaggle/kbagent.py",
                                     "../../kaggle/kbagent.py"};
  for (auto c : candidates) {
    std::ifstream f(c, std::ios::binary);
    if (f.good()) { return c; }
  }
  return "kaggle/kbagent.py";
}

inline std::string agent_source() {
  auto path = find_agent_path();
  std::ifstream f(path, std::ios::binary);
  if (!f.good()) {
    throw JupyterError("cannot read " + path +
                       " (--agent <path> で場所を指定できる)");
  }
  return std::string((std::istreambuf_iterator<char>(f)),
                     std::istreambuf_iterator<char>());
}

inline std::string inject_code() {
  return std::string(
             "import sys, types, json, base64\n"
             "m = types.ModuleType('kbagent')\n"
             "exec(compile(base64.b64decode('") +
         b64_encode(agent_source()) +
         "').decode('utf-8'), 'kbagent.py', 'exec'), m.__dict__)\n"
         "sys.modules['kbagent'] = m\n"
         "print(json.dumps({'version': m.VERSION, 'root': m.ROOT}))\n";
}

// 引数を JSON にして base64 で運ぶ。キー順は sort、区切りは詰める
// （python 側の json.dumps(sort_keys=True, separators=(",", ":")) と一致させる）。
inline std::string args_b64(const json &kwargs) { return b64_encode(kwargs.dump()); }

inline std::string call_code(const std::string &func, const json &kwargs) {
  return "import json, base64\n"
         "import kbagent\n"
         "_a = json.loads(base64.b64decode('" +
         args_b64(kwargs) +
         "').decode('utf-8'))\n"
         "print(json.dumps(kbagent." +
         func + "(**_a)))\n";
}

inline std::string sh_code(const std::string &cmd, const std::string &cwd,
                           double timeout) {
  json a{{"cmd", cmd}, {"cwd", cwd}, {"timeout", timeout}};
  return "import json, base64\n"
         "import kbagent\n"
         "_a = json.loads(base64.b64decode('" +
         args_b64(a) +
         "').decode('utf-8'))\n"
         "kbagent.sh(**_a)\n";
}

// カーネルに kbagent が正しい版で載っている状態にする。
// 毎回注入すると往復が増えるので、まず版を訊いて、違うときだけ送る。
inline bool ensure_agent(JupyterClient &cli, bool force = false) {
  if (!force) {
    try {
      auto j = cli.execute_json(CHECK_CODE, 60);
      if (j.value("version", "") == AGENT_VERSION) { return false; }
    } catch (const JupyterError &) {
    }
  }
  auto j = cli.execute_json(inject_code(), 120);
  auto got = j.value("version", "");
  if (got != AGENT_VERSION) {
    throw JupyterError("kbagent version mismatch after inject: " + got);
  }
  return true;
}

inline json call(JupyterClient &cli, const std::string &func, const json &kwargs,
                 double timeout = 120.0) {
  ensure_agent(cli);
  return cli.execute_json(call_code(func, kwargs), timeout);
}

// ----------------------------------------------------------------------- /sh

// kbagent.sh が最後に出す __KBRIDGE_EXIT__ 行を本文から切り離す。
inline void split_marker(const std::string &text, std::string *body, json *marker) {
  *marker = json();
  auto idx = text.rfind(EXIT_MARKER);
  if (idx == std::string::npos) {
    *body = text;
    return;
  }
  *body = text.substr(0, idx);
  while (!body->empty() && body->back() == '\n') { body->pop_back(); }
  auto tail = text.substr(idx + std::strlen(EXIT_MARKER));
  auto nl = tail.find('\n');
  if (nl != std::string::npos) { tail = tail.substr(0, nl); }
  auto b = tail.find_first_not_of(" \t\r");
  if (b == std::string::npos) { return; }
  auto j = json::parse(tail.substr(b), nullptr, false);
  if (!j.is_discarded()) { *marker = j; }
}

inline json sh(JupyterClient &cli, const std::string &cmd, const std::string &cwd,
               double timeout) {
  ensure_agent(cli);
  // カーネル側のタイムアウトは少し短くして、こちらが切る前に終了コードを書かせる
  double inner = std::max(timeout - 5.0, 1.0);
  auto r = cli.execute(sh_code(cmd, cwd, inner), timeout);
  std::string body;
  json marker;
  split_marker(r.out, &body, &marker);
  r.out = body;
  auto out = r.to_json();
  out["stdout"] = body;
  out["exit_code"] =
      marker.is_object() && marker.contains("exit_code") ? marker["exit_code"] : json();
  if (marker.is_object() && marker.value("timeout", false)) {
    out["status"] = "timeout";
    out["ok"] = false;
  } else if (out["status"] == "ok" && !out["exit_code"].is_null() &&
             out["exit_code"] != 0) {
    out["ok"] = false;
    out["status"] = "error";
    out["evalue"] = "command exited with " + out["exit_code"].dump();
  }
  return out;
}

// /sh/stream 用。マーカー行だけを本文から取り除きながら流す。
// マーカーはチャンクの切れ目をまたぐことがあるので、末尾を持ち越して判定する。
class ShellStream {
 public:
  explicit ShellStream(EventFn emit) : emit_(std::move(emit)) {}

  void feed(const json &ev) {
    if (ev.value("t", "") != "out") {
      emit_(ev);
      return;
    }
    hold_ += ev.value("d", "");
    if (done_) {
      take_marker();
      return;
    }
    auto idx = hold_.find(EXIT_MARKER);
    if (idx != std::string::npos) {
      auto head = hold_.substr(0, idx);
      hold_ = hold_.substr(idx);
      done_ = true;
      if (!head.empty()) {
        emit_(json{{"t", "out"}, {"stream", ev.value("stream", "stdout")},
                   {"d", head}});
      }
      take_marker();
      return;
    }
    // マーカーの断片が末尾に残っている可能性がある分だけ手元に残す
    size_t keep = std::strlen(EXIT_MARKER) - 1;
    if (hold_.size() > keep) {
      auto out = hold_.substr(0, hold_.size() - keep);
      hold_ = hold_.substr(hold_.size() - keep);
      emit_(json{{"t", "out"}, {"stream", ev.value("stream", "stdout")}, {"d", out}});
    }
  }

  void flush() {
    if (!done_ && !hold_.empty()) {
      emit_(json{{"t", "out"}, {"stream", "stdout"}, {"d", hold_}});
      hold_.clear();
    }
  }

  const json &marker() const { return marker_; }

 private:
  void take_marker() {
    auto tail = hold_.substr(std::min(hold_.size(), std::strlen(EXIT_MARKER)));
    if (tail.find('\n') == std::string::npos) {
      auto t = tail;
      while (!t.empty() && (t.back() == ' ' || t.back() == '\r')) { t.pop_back(); }
      if (t.empty() || t.back() != '}') { return; }
    }
    auto nl = tail.find('\n');
    if (nl != std::string::npos) { tail = tail.substr(0, nl); }
    auto b = tail.find_first_not_of(" \t\r");
    if (b == std::string::npos) { return; }
    auto j = json::parse(tail.substr(b), nullptr, false);
    if (!j.is_discarded()) { marker_ = j; }
  }

  EventFn emit_;
  std::string hold_;
  json marker_;
  bool done_ = false;
};

// ---------------------------------------------------------------------- /gpu

inline json gpu(JupyterClient &cli, double timeout = 120.0) {
  return call(cli, "gpu", json::object(), timeout);
}

// ---------------------------------------------------------------------- /job

inline json job_start(JupyterClient &cli, const json &cmd, const json &code,
                      const std::string &name, const std::string &cwd,
                      const json &env, double timeout = 120.0) {
  json a{{"cmd", cmd}, {"code", code}, {"name", name}, {"cwd", cwd}, {"env", env}};
  return call(cli, "start", a, timeout);
}

inline json job_list(JupyterClient &cli, double timeout = 120.0) {
  return call(cli, "ls", json::object(), timeout);
}

inline json job_status(JupyterClient &cli, const std::string &id,
                       double timeout = 120.0) {
  return call(cli, "status", json{{"job_id", id}}, timeout);
}

inline json job_log(JupyterClient &cli, const std::string &id, long long offset,
                    long long max_bytes, double timeout = 120.0) {
  return call(cli, "log",
              json{{"job_id", id}, {"offset", offset}, {"max_bytes", max_bytes}},
              timeout);
}

inline json job_kill(JupyterClient &cli, const std::string &id,
                     double timeout = 120.0) {
  return call(cli, "kill", json{{"job_id", id}}, timeout);
}

inline json job_rm(JupyterClient &cli, const std::string &id, double timeout = 120.0) {
  return call(cli, "rm", json{{"job_id", id}}, timeout);
}

}  // namespace ops
}  // namespace kbridge
