// kbridge CLI（C++ 版）— ローカルサーバ（python 版 / cpp 版のどちらでも）を叩く薄い
// クライアント。python -m kbridge.cli と同じサブコマンド・同じ出力になるようにしてある。
//
//   sh cpp/build/gcc.sh cpp/kbridge_cli.cpp -o kbridge.exe
//
//   kbridge connect "<VSCode Compatible URL>"
//   kbridge gpu
//   kbridge run "nvcc --version"
//   kbridge exec -c "print(1+1)"
//   kbridge sync ./pure work/pure
//   kbridge job start "bash build.sh && ./train" --name lpr
//   kbridge job log <id> --follow
//   kbridge get work/best.ckpt ./best.ckpt
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <thread>
#include <vector>

#include "httplib.h"
#include "json.hpp"

#include "b64.hpp"
#include "jurl.hpp"

using json = nlohmann::json;
namespace fs = std::filesystem;

static std::string g_base = "http://127.0.0.1:8787";
static std::string g_key;

// --------------------------------------------------------------------- 通信

struct Reply {
  int status = 0;
  json body;
  std::string raw;
  bool transport_ok = false;
};

static void split_base(std::string *origin, std::string *prefix) {
  auto u = kbridge::split_url(g_base);
  *origin = u.origin();
  *prefix = u.path;
  while (!prefix->empty() && prefix->back() == '/') { prefix->pop_back(); }
}

static httplib::Headers headers(bool with_body) {
  httplib::Headers h{{"Accept", "application/json"}};
  if (with_body) { h.emplace("Content-Type", "application/json"); }
  if (!g_key.empty()) { h.emplace("X-Bridge-Key", g_key); }
  return h;
}

static Reply call(const std::string &method, const std::string &path,
                  const json *body = nullptr, int timeout_sec = 300) {
  std::string origin, prefix;
  split_base(&origin, &prefix);
  httplib::Client cli(origin);
  cli.set_connection_timeout(15, 0);
  cli.set_read_timeout(timeout_sec, 0);
  cli.set_write_timeout(timeout_sec, 0);
  auto full = prefix + path;
  std::string payload = body ? body->dump() : "";
  httplib::Result res;
  if (method == "GET") {
    res = cli.Get(full, headers(false));
  } else if (method == "POST") {
    res = cli.Post(full, headers(true), payload, "application/json");
  } else if (method == "DELETE") {
    res = cli.Delete(full, headers(true), payload, "application/json");
  }
  Reply r;
  if (!res) {
    std::fprintf(stderr,
                 "error: %s に届かない (%s)。サーバは動いている? "
                 "`kbridge_server.exe --port 8787` で起動する\n",
                 g_base.c_str(), httplib::to_string(res.error()).c_str());
    return r;
  }
  r.transport_ok = true;
  r.status = res->status;
  r.raw = res->body;
  r.body = json::parse(res->body, nullptr, false);
  return r;
}

// NDJSON を人が読める形に落とす。戻り値は end イベント。
static json call_stream(const std::string &path, const json &body,
                        int timeout_sec = 3600) {
  std::string origin, prefix;
  split_base(&origin, &prefix);
  httplib::Client cli(origin);
  cli.set_connection_timeout(15, 0);
  cli.set_read_timeout(timeout_sec, 0);
  cli.set_write_timeout(timeout_sec, 0);

  std::string buf;
  json end;
  auto handle_line = [&](const std::string &line) {
    if (line.empty()) { return; }
    auto ev = json::parse(line, nullptr, false);
    if (ev.is_discarded()) { return; }
    auto t = ev.value("t", "");
    if (t == "out") {
      std::fputs(ev.value("d", "").c_str(), stdout);
      std::fflush(stdout);
    } else if (t == "result") {
      std::printf("%s\n", ev.value("d", "").c_str());
    } else if (t == "error") {
      if (ev.contains("traceback") && ev["traceback"].is_array()) {
        for (const auto &l : ev["traceback"]) {
          std::printf("%s\n", l.get<std::string>().c_str());
        }
      } else {
        std::printf("%s: %s\n", ev.value("ename", "").c_str(),
                    ev.value("evalue", "").c_str());
      }
    } else if (t == "end") {
      end = ev;
    }
  };

  auto res = cli.Post(
      prefix + path, headers(true), body.dump(), "application/json",
      [&](const char *data, size_t len) {
        buf.append(data, len);
        size_t pos;
        while ((pos = buf.find('\n')) != std::string::npos) {
          handle_line(buf.substr(0, pos));
          buf.erase(0, pos + 1);
        }
        return true;
      });
  if (!buf.empty()) { handle_line(buf); }
  if (!res) {
    std::fprintf(stderr, "error: %s に届かない (%s)\n", g_base.c_str(),
                 httplib::to_string(res.error()).c_str());
  }
  return end;
}

static int show(const Reply &r) {
  if (!r.transport_ok) { return 2; }
  if (r.body.is_discarded()) {
    std::printf("%s\n", r.raw.c_str());
    return r.status >= 400 ? 1 : 0;
  }
  std::printf("%s\n", r.body.dump(2).c_str());
  if (r.body.is_object() && r.body.contains("ok") && r.body["ok"] == false) {
    return 1;
  }
  return r.status >= 400 ? 1 : 0;
}

// --------------------------------------------------------------------- 引数

struct Args {
  std::vector<std::string> pos;
  std::map<std::string, std::string> opt;
  bool has(const std::string &k) const { return opt.count(k) > 0; }
  std::string get(const std::string &k, const std::string &dflt = "") const {
    auto it = opt.find(k);
    return it == opt.end() ? dflt : it->second;
  }
  double num(const std::string &k, double dflt) const {
    auto it = opt.find(k);
    return it == opt.end() ? dflt : std::atof(it->second.c_str());
  }
};

// 値を取らないフラグはここに書く
static bool is_flag(const std::string &k) {
  return k == "--follow" || k == "-f" || k == "--quiet" || k == "-q" ||
         k == "--python" || k == "--new-kernel" || k == "--no-save";
}

static Args parse_args(int argc, char **argv, int from) {
  Args a;
  for (int i = from; i < argc; ++i) {
    std::string s = argv[i];
    if (s.rfind("--", 0) == 0 || (s.size() == 2 && s[0] == '-')) {
      if (is_flag(s)) {
        a.opt[s] = "1";
      } else if (i + 1 < argc) {
        a.opt[s] = argv[++i];
      } else {
        a.opt[s] = "";
      }
    } else {
      a.pos.push_back(s);
    }
  }
  return a;
}

static std::string read_file(const std::string &path) {
  if (path == "-") {
    return std::string((std::istreambuf_iterator<char>(std::cin)),
                       std::istreambuf_iterator<char>());
  }
  std::ifstream f(path, std::ios::binary);
  if (!f.good()) { throw std::runtime_error("cannot read " + path); }
  return std::string((std::istreambuf_iterator<char>(f)),
                     std::istreambuf_iterator<char>());
}

// ------------------------------------------------------------------- 各コマンド

static int cmd_connect(const Args &a) {
  json body = json::object();
  if (!a.pos.empty()) { body["url"] = a.pos[0]; }
  if (a.has("--new-kernel")) { body["new_kernel"] = true; }
  auto r = call("POST", "/session", &body, 300);
  if (r.transport_ok && r.status == 200 && !a.pos.empty() && !a.has("--no-save")) {
    std::ofstream f(".kbridge.json", std::ios::binary);
    f << json{{"url", a.pos[0]}}.dump(2);
    std::fprintf(stderr, "saved .kbridge.json (トークンを含む。.gitignore 済み)\n");
  }
  return show(r);
}

static int cmd_exec(const Args &a) {
  std::string code;
  if (a.has("-c")) {
    code = a.get("-c");
  } else if (!a.pos.empty()) {
    code = read_file(a.pos[0]);
  } else {
    std::fprintf(stderr, "error: -c CODE か file を指定する\n");
    return 2;
  }
  json body{{"code", code}, {"timeout", a.num("--timeout", 300)}};
  if (a.has("-q") || a.has("--quiet")) {
    return show(call("POST", "/exec", &body, (int)a.num("--timeout", 300) + 30));
  }
  auto end = call_stream("/exec/stream", body);
  return end.is_object() && end.value("status", "") == "ok" ? 0 : 1;
}

static int cmd_run(const Args &a) {
  if (a.pos.empty()) {
    std::fprintf(stderr, "error: 実行するコマンドを指定する\n");
    return 2;
  }
  json body{{"cmd", a.pos[0]},
            {"timeout", a.num("--timeout", 300)},
            {"cwd", a.get("--cwd", "/kaggle/working")}};
  if (a.has("-q") || a.has("--quiet")) {
    return show(call("POST", "/sh", &body, (int)a.num("--timeout", 300) + 30));
  }
  auto end = call_stream("/sh/stream", body);
  if (end.is_object() && end.contains("exit_code") && !end["exit_code"].is_null() &&
      end["exit_code"] != 0) {
    std::fprintf(stderr, "exit code %s\n", end["exit_code"].dump().c_str());
  }
  return end.is_object() && end.value("status", "") == "ok" ? 0 : 1;
}

static int cmd_put(const Args &a) {
  if (a.pos.empty()) {
    std::fprintf(stderr, "error: kbridge put <local> [remote]\n");
    return 2;
  }
  auto data = read_file(a.pos[0]);
  std::string remote = a.pos.size() > 1 ? a.pos[1]
                                        : fs::path(a.pos[0]).filename().string();
  json body{{"path", remote}, {"content_b64", kbridge::b64_encode(data)}};
  return show(call("POST", "/upload", &body, 600));
}

static int cmd_get(const Args &a) {
  if (a.pos.empty()) {
    std::fprintf(stderr, "error: kbridge get <remote> [local]\n");
    return 2;
  }
  auto r = call("GET", "/download?path=" + kbridge::encode_query(a.pos[0]), nullptr,
                600);
  if (!r.transport_ok || r.body.is_discarded() || !r.body.value("ok", false)) {
    return show(r);
  }
  auto data = kbridge::b64_decode(r.body.value("content_b64", ""));
  std::string local = a.pos.size() > 1 ? a.pos[1]
                                       : fs::path(a.pos[0]).filename().string();
  auto parent = fs::path(local).parent_path();
  if (!parent.empty()) { fs::create_directories(parent); }
  std::ofstream f(local, std::ios::binary);
  f.write(data.data(), (std::streamsize)data.size());
  std::printf("%s\n", json{{"ok", true}, {"path", r.body.value("path", "")},
                           {"local", local}, {"size", data.size()}}
                          .dump(2)
                          .c_str());
  return 0;
}

static int cmd_sync(const Args &a) {
  if (a.pos.empty()) {
    std::fprintf(stderr, "error: kbridge sync <localdir> [remotedir]\n");
    return 2;
  }
  fs::path root = fs::absolute(a.pos[0]);
  if (!fs::is_directory(root)) {
    std::fprintf(stderr, "error: no such directory: %s\n", a.pos[0].c_str());
    return 2;
  }
  std::string remote_root = a.pos.size() > 1 ? a.pos[1] : "";
  while (!remote_root.empty() && remote_root.back() == '/') { remote_root.pop_back(); }
  const std::vector<std::string> skip{".git", "__pycache__", ".venv", "node_modules",
                                      "build", "scratch"};
  auto max_bytes = (size_t)a.num("--max-bytes", 32.0 * 1024 * 1024);

  size_t files = 0, bytes = 0;
  std::vector<fs::path> all;
  for (auto it = fs::recursive_directory_iterator(root);
       it != fs::recursive_directory_iterator(); ++it) {
    if (it->is_directory()) {
      auto name = it->path().filename().string();
      if (std::find(skip.begin(), skip.end(), name) != skip.end()) {
        it.disable_recursion_pending();
      }
      continue;
    }
    all.push_back(it->path());
  }
  std::sort(all.begin(), all.end());
  for (const auto &p : all) {
    auto size = fs::file_size(p);
    if (size > max_bytes) {
      std::fprintf(stderr, "skip (too big): %s\n", p.string().c_str());
      continue;
    }
    auto rel = fs::relative(p, root).generic_string();
    auto remote = remote_root.empty() ? rel : remote_root + "/" + rel;
    auto data = read_file(p.string());
    json body{{"path", remote}, {"content_b64", kbridge::b64_encode(data)}};
    auto r = call("POST", "/upload", &body, 600);
    if (!r.transport_ok || !r.body.value("ok", false)) { return show(r); }
    files++;
    bytes += data.size();
    std::fprintf(stderr, "sent %s (%zu bytes)\n", remote.c_str(), data.size());
  }
  std::printf("%s\n", json{{"ok", true}, {"files", files}, {"bytes", bytes},
                           {"remote", remote_root}}
                          .dump(2)
                          .c_str());
  return 0;
}

static int cmd_job(const Args &a) {
  if (a.pos.empty()) {
    std::fprintf(stderr, "error: kbridge job <start|list|status|log|kill|rm> ...\n");
    return 2;
  }
  auto sub = a.pos[0];
  if (sub == "start") {
    if (a.pos.size() < 2) {
      std::fprintf(stderr, "error: kbridge job start <cmd> [--name N] [--python]\n");
      return 2;
    }
    json body{{"name", a.get("--name", "job")},
              {"cwd", a.get("--cwd", "/kaggle/working")}};
    if (a.has("--python")) {
      body["code"] = read_file(a.pos[1]);
    } else {
      body["cmd"] = a.pos[1];
    }
    return show(call("POST", "/job", &body, 300));
  }
  if (sub == "list") { return show(call("GET", "/job")); }
  if (a.pos.size() < 2) {
    std::fprintf(stderr, "error: job %s <id>\n", sub.c_str());
    return 2;
  }
  auto id = a.pos[1];
  if (sub == "status") { return show(call("GET", "/job/" + id)); }
  if (sub == "kill") {
    json empty = json::object();
    return show(call("POST", "/job/" + id + "/kill", &empty));
  }
  if (sub == "rm") { return show(call("DELETE", "/job/" + id)); }
  if (sub == "log") {
    long long offset = (long long)a.num("--offset", 0);
    long long max_bytes = (long long)a.num("--max-bytes", 65536);
    bool follow = a.has("-f") || a.has("--follow");
    double interval = a.num("--interval", 5.0);
    while (true) {
      auto r = call("GET", "/job/" + id + "/log?offset=" + std::to_string(offset) +
                               "&max=" + std::to_string(max_bytes));
      if (!r.transport_ok || r.body.is_discarded() || !r.body.value("ok", false)) {
        return show(r);
      }
      auto data = r.body.value("data", "");
      if (!data.empty()) {
        std::fputs(data.c_str(), stdout);
        std::fflush(stdout);
      }
      offset = r.body.value("next_offset", offset);
      if (!follow) { break; }
      auto state = r.body.value("state", "");
      if (state != "running" && offset >= r.body.value("log_size", (long long)0)) {
        std::fprintf(stderr, "\n--- %s (exit を確認するには job status) ---\n",
                     state.c_str());
        break;
      }
      std::this_thread::sleep_for(
          std::chrono::milliseconds((long long)(interval * 1000)));
    }
    return 0;
  }
  std::fprintf(stderr, "unknown job subcommand: %s\n", sub.c_str());
  return 2;
}

static int cmd_batch(const Args &a) {
  if (a.pos.empty()) {
    std::fprintf(stderr, "error: kbridge batch <push|status|output|pull> ...\n");
    return 2;
  }
  auto sub = a.pos[0];
  if (sub == "push" && a.pos.size() >= 2) {
    json body{{"dir", a.pos[1]}};
    return show(call("POST", "/batch/push", &body, 900));
  }
  if (sub == "status" && a.pos.size() >= 2) {
    return show(call("GET", "/batch/status?id=" + kbridge::encode_query(a.pos[1]),
                     nullptr, 600));
  }
  if ((sub == "output" || sub == "pull") && a.pos.size() >= 3) {
    json body{{"id", a.pos[1]}, {"dir", a.pos[2]}};
    return show(call("POST", "/batch/" + sub, &body, 1800));
  }
  std::fprintf(stderr, "error: 引数が足りない\n");
  return 2;
}

static int cmd_serve(int argc, char **argv, int from) {
  // サーバは別の実行ファイル。ここからそのまま起動して、使い勝手を python 版
  // （python -m kbridge.cli serve）と揃える。
  auto exe = fs::path(argv[0]).parent_path() / "kbridge_server.exe";
  if (!fs::exists(exe)) { exe = "kbridge_server.exe"; }
  std::string cmd = "\"" + exe.string() + "\"";
  for (int i = from; i < argc; ++i) { cmd += std::string(" ") + argv[i]; }
#ifdef _WIN32
  cmd = "\"" + cmd + "\"";
#endif
  return std::system(cmd.c_str());
}

static void usage() {
  std::printf(
      "kbridge (cpp) — Kaggle Jupyter Server ブリッジの CLI\n"
      "  kbridge [--base URL] [--api-key K] <command> ...\n\n"
      "  serve [...]                   ローカルサーバを起動する\n"
      "  connect <url>                 Kaggle の VSCode Compatible URL に接続する\n"
      "  status                        サーバと接続の状態\n"
      "  gpu                           GPU の状態\n"
      "  exec -c CODE | <file>         Python を 1 セル実行する\n"
      "  run <cmd> [--cwd D]           シェルコマンドを実行する\n"
      "  ls [path]                     Kaggle 側のファイル一覧\n"
      "  put <local> [remote]          ファイルを送る\n"
      "  get <remote> [local]          ファイルを取る\n"
      "  sync <localdir> [remotedir]   ディレクトリを丸ごと送る\n"
      "  job start <cmd> [--name N]    長時間ジョブを始める（学習はこれ）\n"
      "  job list | status <id> | kill <id> | rm <id>\n"
      "  job log <id> [-f]             ログを追う\n"
      "  batch push <dir> | status <id> | output <id> <dir> | pull <id> <dir>\n");
}

int main(int argc, char **argv) {
  if (const char *b = std::getenv("KBRIDGE_BASE")) { g_base = b; }
  if (const char *k = std::getenv("KBRIDGE_API_KEY")) { g_key = k; }

  int i = 1;
  while (i < argc) {
    std::string s = argv[i];
    if (s == "--base" && i + 1 < argc) {
      g_base = argv[i + 1];
      i += 2;
    } else if (s == "--api-key" && i + 1 < argc) {
      g_key = argv[i + 1];
      i += 2;
    } else {
      break;
    }
  }
  if (i >= argc) {
    usage();
    return 2;
  }
  std::string cmd = argv[i];
  auto a = parse_args(argc, argv, i + 1);

  try {
    if (cmd == "serve") { return cmd_serve(argc, argv, i + 1); }
    if (cmd == "connect") { return cmd_connect(a); }
    if (cmd == "status") { return show(call("GET", "/healthz", nullptr, 30)); }
    if (cmd == "gpu") { return show(call("GET", "/gpu", nullptr, 300)); }
    if (cmd == "exec") { return cmd_exec(a); }
    if (cmd == "run") { return cmd_run(a); }
    if (cmd == "ls") {
      auto p = a.pos.empty() ? "" : a.pos[0];
      return show(call("GET", "/ls?path=" + kbridge::encode_query(p)));
    }
    if (cmd == "put") { return cmd_put(a); }
    if (cmd == "get") { return cmd_get(a); }
    if (cmd == "sync") { return cmd_sync(a); }
    if (cmd == "job") { return cmd_job(a); }
    if (cmd == "batch") { return cmd_batch(a); }
    if (cmd == "-h" || cmd == "--help" || cmd == "help") {
      usage();
      return 0;
    }
  } catch (const std::exception &e) {
    std::fprintf(stderr, "error: %s\n", e.what());
    return 2;
  }
  std::fprintf(stderr, "unknown command: %s\n", cmd.c_str());
  usage();
  return 2;
}
