// バッチ実行（kaggle CLI のフォールバック）。python/kbridge/batch.py と同じ応答を返す。
//
// 認証は kaggle CLI 側に任せる（KAGGLE_API_TOKEN=KGAT_... か ~/.kaggle/kaggle.json）。
// kbridge はトークンを読まないし持たない。
#pragma once

#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <string>
#include <vector>

#include "json.hpp"

#ifdef _WIN32
#include <direct.h>
#define KB_POPEN _popen
#define KB_PCLOSE _pclose
#else
#include <sys/stat.h>
#define KB_POPEN popen
#define KB_PCLOSE pclose
#endif

namespace kbridge {
namespace batch {

using json = nlohmann::json;

inline bool have_kaggle() {
  static int cached = -1;
  if (cached >= 0) { return cached != 0; }
#ifdef _WIN32
  const char *probe = "kaggle --version >NUL 2>&1";
#else
  const char *probe = "kaggle --version >/dev/null 2>&1";
#endif
  cached = std::system(probe) == 0 ? 1 : 0;
  return cached != 0;
}

inline bool is_dir(const std::string &path) {
#ifdef _WIN32
  struct _stat st {};
  return _stat(path.c_str(), &st) == 0 && (st.st_mode & _S_IFDIR);
#else
  struct stat st {};
  return stat(path.c_str(), &st) == 0 && S_ISDIR(st.st_mode);
#endif
}

inline void make_dir(const std::string &path) {
#ifdef _WIN32
  _mkdir(path.c_str());
#else
  mkdir(path.c_str(), 0755);
#endif
}

// シェルに渡す 1 引数を安全に囲む。
inline std::string quote(const std::string &s) {
#ifdef _WIN32
  std::string out = "\"";
  for (char c : s) {
    if (c == '"') { out += '\\'; }
    out += c;
  }
  return out + "\"";
#else
  std::string out = "'";
  for (char c : s) {
    if (c == '\'') {
      out += "'\\''";
    } else {
      out += c;
    }
  }
  return out + "'";
#endif
}

struct Ran {
  int exit_code = -1;
  std::string raw;
};

inline Ran run(const std::vector<std::string> &args) {
  std::string cmd = "kaggle";
  for (const auto &a : args) { cmd += " " + quote(a); }
  cmd += " 2>&1";
  Ran r;
  FILE *p = KB_POPEN(cmd.c_str(), "r");
  if (!p) {
    r.raw = "cannot start kaggle CLI";
    return r;
  }
  char buf[4096];
  while (fgets(buf, sizeof(buf), p)) { r.raw += buf; }
  r.exit_code = KB_PCLOSE(p);
  while (!r.raw.empty() && (r.raw.back() == '\n' || r.raw.back() == '\r')) {
    r.raw.pop_back();
  }
  return r;
}

inline json unavailable() {
  return json{{"ok", false}, {"error", "kaggle CLI not found (pip install kaggle)"}};
}

inline std::string tail(const std::string &s, size_t n) {
  return s.size() > n ? s.substr(s.size() - n) : s;
}

inline json push(const std::string &dir) {
  // 引数の検証を CLI の有無より先にやる（python 版と同じ順番。順番が違うと
  // 「CLI も無いし dir も無い」ときに返るメッセージがずれる — tests/parity.py が拾った）
  if (!is_dir(dir)) {
    return json{{"ok", false}, {"error", "no such directory: " + dir}};
  }
  if (!have_kaggle()) { return unavailable(); }
  auto r = run({"kernels", "push", "-p", dir});
  json out{{"ok", r.exit_code == 0}, {"exit_code", r.exit_code},
           {"raw", r.raw}, {"dir", dir}};
  if (r.exit_code != 0) {
    out["error"] = r.raw.empty() ? "kaggle kernels push failed" : tail(r.raw, 500);
  }
  return out;
}

inline json status(const std::string &id) {
  if (!have_kaggle()) { return unavailable(); }
  auto r = run({"kernels", "status", id});
  std::string lower;
  for (char c : r.raw) { lower += (char)tolower((unsigned char)c); }
  std::string state = "unknown";
  for (const char *w : {"complete", "running", "error", "cancelacknowledged",
                        "queued"}) {
    if (lower.find(w) != std::string::npos) {
      state = w;
      break;
    }
  }
  json out{{"ok", r.exit_code == 0}, {"exit_code", r.exit_code}, {"raw", r.raw},
           {"id", id}, {"state", state}};
  if (r.exit_code != 0) {
    out["error"] = r.raw.empty() ? "kaggle kernels status failed" : tail(r.raw, 500);
  }
  return out;
}

inline json output(const std::string &id, const std::string &dir) {
  if (!have_kaggle()) { return unavailable(); }
  make_dir(dir);
  auto r = run({"kernels", "output", id, "-p", dir});
  json out{{"ok", r.exit_code == 0}, {"exit_code", r.exit_code}, {"raw", r.raw},
           {"id", id}, {"dir", dir}};
  if (r.exit_code != 0) {
    out["error"] = r.raw.empty() ? "kaggle kernels output failed" : tail(r.raw, 500);
  }
  return out;
}

inline json pull(const std::string &id, const std::string &dir) {
  if (!have_kaggle()) { return unavailable(); }
  make_dir(dir);
  auto r = run({"kernels", "pull", id, "-p", dir, "--metadata"});
  json out{{"ok", r.exit_code == 0}, {"exit_code", r.exit_code}, {"raw", r.raw},
           {"id", id}, {"dir", dir}};
  auto meta_path = dir + "/kernel-metadata.json";
  std::ifstream f(meta_path, std::ios::binary);
  if (r.exit_code == 0 && f.good()) {
    std::string body((std::istreambuf_iterator<char>(f)),
                     std::istreambuf_iterator<char>());
    f.close();
    auto meta = json::parse(body, nullptr, false);
    if (meta.is_object()) {
      // push のたびに勝手に実行されると無料枠を溶かすので、既定で止めておく
      if (!(meta.contains("run_on_push") && meta["run_on_push"].is_boolean() &&
            meta["run_on_push"] == false)) {
        meta["run_on_push"] = false;
        std::ofstream o(meta_path, std::ios::binary);
        o << meta.dump(2);
        out["run_on_push_set_false"] = true;
      }
      json sources = json::object();
      for (const char *k : {"dataset_sources", "kernel_sources",
                            "competition_sources", "model_sources"}) {
        sources[k] = meta.contains(k) ? meta[k] : json::array();
      }
      out["sources"] = sources;
    }
  }
  if (r.exit_code != 0) {
    out["error"] = r.raw.empty() ? "kaggle kernels pull failed" : tail(r.raw, 500);
  }
  return out;
}

}  // namespace batch
}  // namespace kbridge
