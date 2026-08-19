// kbridge ローカルサーバ（C++ / cpp-httplib 版）。spec/API.md の実装。
//
//   sh cpp/build/gcc.sh cpp/kbridge_server.cpp -o kbridge_server.exe
//   ./kbridge_server.exe --port 8787
//
// python 版（python/kbridge/server.py）と URL・JSON・挙動を合わせること。
// 仕様を変えるときは spec/API.md -> 両実装 -> tests/parity.py の順で直す。
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <deque>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

#include "httplib.h"
#include "json.hpp"

#include "b64.hpp"
#include "batch.hpp"
#include "jupyter.hpp"
#include "jurl.hpp"
#include "ops.hpp"

using kbridge::json;
using kbridge::JupyterClient;
using kbridge::JupyterError;

static const char *IMPL = "cpp";
static const char *VERSION = "0.1.0";

// ---------------------------------------------------------------- 状態

struct State {
  std::unique_ptr<JupyterClient> client;
  std::string url;
  std::mutex lock;   // カーネルは 1 度に 1 実行しか受けない
  double started = kbridge::now_seconds();
  std::string api_key;
  bool insecure = false;
} g;

class SessionMissing : public std::runtime_error {
 public:
  SessionMissing() : std::runtime_error("no session; POST /session first") {}
};

static JupyterClient &need_session() {
  if (!g.client || !g.client->has_kernel()) { throw SessionMissing(); }
  return *g.client;
}

// ---------------------------------------------------------------- 補助

static void send_json(httplib::Response &res, const json &body, int status = 200) {
  res.status = status;
  res.set_content(body.dump(), "application/json; charset=utf-8");
}

static void send_error(httplib::Response &res, const std::string &message,
                       int status) {
  send_json(res, json{{"ok", false}, {"error", message}}, status);
}

static json parse_body(const httplib::Request &req) {
  if (req.body.empty()) { return json::object(); }
  auto j = json::parse(req.body, nullptr, false);
  if (j.is_discarded()) { throw std::invalid_argument("body must be JSON"); }
  if (!j.is_object()) { throw std::invalid_argument("body must be a JSON object"); }
  return j;
}

static std::string safe_path(const std::string &raw) {
  std::string p;
  for (char c : raw) { p += (c == '\\') ? '/' : c; }
  size_t b = 0, e = p.size();
  while (b < e && p[b] == '/') { ++b; }
  while (e > b && p[e - 1] == '/') { --e; }
  p = p.substr(b, e - b);
  for (const auto &seg : kbridge::path_segments(p)) {
    if (seg == "..") { throw std::invalid_argument("path must not contain '..'"); }
  }
  return p;
}

static double exec_timeout(const json &body) {
  double t = body.contains("timeout") && body["timeout"].is_number()
                 ? body["timeout"].get<double>()
                 : 300.0;
  if (!(t > 0 && t <= 43200)) {
    throw std::invalid_argument("timeout must be in (0, 43200]");
  }
  return t;
}

// 例外を spec の HTTP コードへ落とす。全ハンドラをこれで包む。
static void guard(httplib::Response &res, const std::function<void()> &fn) {
  try {
    fn();
  } catch (const SessionMissing &e) {
    send_error(res, e.what(), 409);
  } catch (const JupyterError &e) {
    send_error(res, e.what(), 502);
  } catch (const std::invalid_argument &e) {
    send_error(res, e.what(), 400);
  } catch (const std::exception &e) {
    send_error(res, e.what(), 500);
  }
}

// ---------------------------------------------------------------- NDJSON

// 実行を別スレッドで回し、届いたイベントを NDJSON で流すための受け渡し。
struct Lines {
  std::mutex m;
  std::condition_variable cv;
  std::deque<std::string> q;
  bool done = false;

  void push(const json &ev) {
    {
      std::lock_guard<std::mutex> lock(m);
      q.push_back(ev.dump() + "\n");
    }
    cv.notify_all();
  }
  void finish() {
    {
      std::lock_guard<std::mutex> lock(m);
      done = true;
    }
    cv.notify_all();
  }
  bool pop(std::string &out) {
    std::unique_lock<std::mutex> lock(m);
    cv.wait(lock, [&] { return !q.empty() || done; });
    if (q.empty()) { return false; }
    out = std::move(q.front());
    q.pop_front();
    return true;
  }
};

// code を実行して NDJSON を返す。shell=true なら __KBRIDGE_EXIT__ 行を剥がす。
static void stream_exec(httplib::Response &res, const std::string &code,
                        double timeout, bool shell) {
  need_session();  // ここで 409 を出しておく（ストリーム開始後には出せない）
  auto lines = std::make_shared<Lines>();

  std::thread([lines, code, timeout, shell] {
    std::lock_guard<std::mutex> kernel_lock(g.lock);
    json end;
    try {
      auto &cli = *g.client;
      std::unique_ptr<kbridge::ops::ShellStream> sink;
      kbridge::EventFn emit = [lines](const json &ev) { lines->push(ev); };
      kbridge::EventFn feed = emit;
      if (shell) {
        sink = std::make_unique<kbridge::ops::ShellStream>(emit);
        auto raw = sink.get();
        feed = [raw](const json &ev) { raw->feed(ev); };
      }
      auto r = cli.execute(code, timeout, feed);
      json exit_code;
      if (sink) {
        sink->flush();
        const auto &marker = sink->marker();
        if (marker.is_object()) {
          if (marker.contains("exit_code")) { exit_code = marker["exit_code"]; }
          if (marker.value("timeout", false)) {
            r.status = "timeout";
            r.ok = false;
          }
        }
        if (r.status == "ok" && !exit_code.is_null() && exit_code != 0) {
          r.status = "error";
          r.ok = false;
        }
      }
      end = json{{"t", "end"}, {"status", r.status},
                 {"execution_count", r.execution_count}, {"elapsed", r.elapsed}};
      if (sink) { end["exit_code"] = exit_code; }
    } catch (const std::exception &e) {
      end = json{{"t", "end"}, {"status", "abort"}, {"error", e.what()},
                 {"execution_count", json()}, {"elapsed", 0.0}};
    }
    lines->push(end);
    lines->finish();
  }).detach();

  res.set_chunked_content_provider(
      "application/x-ndjson",
      [lines, first = std::make_shared<bool>(true)](
          size_t, httplib::DataSink &sink) mutable {
        if (*first) {
          *first = false;
          auto s = json{{"t", "start"}}.dump() + "\n";
          sink.write(s.data(), s.size());
          return true;
        }
        std::string line;
        if (!lines->pop(line)) {
          sink.done();
          return true;
        }
        sink.write(line.data(), line.size());
        return true;
      });
}

// ---------------------------------------------------------------- 既定 URL

static std::string default_url() {
  if (const char *env = std::getenv("KAGGLE_JUPYTER_URL")) {
    if (*env) { return env; }
  }
  // python 版 (python/kbridge/server.py の default_url) と同じ順で同じ場所を見る
  std::vector<std::string> paths{".kbridge.json"};
  for (const char *home : {"USERPROFILE", "HOME"}) {
    if (const char *h = std::getenv(home)) {
      if (*h) {
        paths.push_back(std::string(h) + "/.kbridge.json");
        break;
      }
    }
  }
  for (const auto &path : paths) {
    std::ifstream f(path, std::ios::binary);
    if (!f.good()) { continue; }
    std::string body((std::istreambuf_iterator<char>(f)),
                     std::istreambuf_iterator<char>());
    auto j = json::parse(body, nullptr, false);
    if (j.is_object() && j.contains("url")) { return j.value("url", ""); }
  }
  return "";
}

// ---------------------------------------------------------------- ルート

static void install_routes(httplib::Server &svr) {
  // 認証（--api-key を付けたときだけ）
  svr.set_pre_routing_handler([](const httplib::Request &req,
                                 httplib::Response &res) {
    if (!g.api_key.empty() &&
        req.get_header_value("X-Bridge-Key") != g.api_key) {
      send_error(res, "bad or missing X-Bridge-Key", 401);
      return httplib::Server::HandlerResponse::Handled;
    }
    return httplib::Server::HandlerResponse::Unhandled;
  });

  // --- セッション ---------------------------------------------------------
  svr.Get("/healthz", [](const httplib::Request &, httplib::Response &res) {
    json body{{"ok", true}, {"impl", IMPL}, {"version", VERSION},
              {"connected", g.client && g.client->connected()},
              {"kernel_id", g.client && g.client->has_kernel()
                                ? json(g.client->kernel_id())
                                : json()},
              {"uptime", std::round((kbridge::now_seconds() - g.started) * 1000) / 1000}};
    send_json(res, body);
  });

  svr.Post("/session", [](const httplib::Request &req, httplib::Response &res) {
    guard(res, [&] {
      auto body = parse_body(req);
      std::string url = body.value("url", "");
      if (url.empty()) { url = default_url(); }
      if (url.empty()) {
        throw std::invalid_argument(
            "no url: give {\"url\": ...}, or set KAGGLE_JUPYTER_URL, "
            "or put it in .kbridge.json");
      }
      auto client = std::make_unique<JupyterClient>(url);
      client->set_insecure(g.insecure);
      auto info = client->connect(body.value("new_kernel", false));
      {
        std::lock_guard<std::mutex> lock(g.lock);
        if (g.client) { g.client->disconnect(); }
        g.client = std::move(client);
        g.url = url;
      }
      info["ok"] = true;
      send_json(res, info);
    });
  });

  svr.Get("/session", [](const httplib::Request &, httplib::Response &res) {
    guard(res, [&] {
      auto info = need_session().info();
      info["ok"] = true;
      send_json(res, info);
    });
  });

  svr.Delete("/session", [](const httplib::Request &, httplib::Response &res) {
    guard(res, [&] {
      need_session();
      std::lock_guard<std::mutex> lock(g.lock);
      g.client->shutdown();
      g.client.reset();
      send_json(res, json{{"ok", true}});
    });
  });

  svr.Post("/interrupt", [](const httplib::Request &, httplib::Response &res) {
    guard(res, [&] {
      need_session().interrupt();
      send_json(res, json{{"ok", true}});
    });
  });

  svr.Post("/restart", [](const httplib::Request &, httplib::Response &res) {
    guard(res, [&] {
      auto &cli = need_session();
      std::lock_guard<std::mutex> lock(g.lock);
      send_json(res, json{{"ok", true}, {"kernel_id", cli.restart()}});
    });
  });

  // --- 実行 ---------------------------------------------------------------
  svr.Post("/exec", [](const httplib::Request &req, httplib::Response &res) {
    guard(res, [&] {
      auto body = parse_body(req);
      auto code = body.value("code", "");
      if (code.empty()) { throw std::invalid_argument("code is required"); }
      auto &cli = need_session();
      std::lock_guard<std::mutex> lock(g.lock);
      auto r = cli.execute(code, exec_timeout(body));
      send_json(res, r.to_json(), r.status == "timeout" ? 504 : 200);
    });
  });

  svr.Post("/exec/stream", [](const httplib::Request &req, httplib::Response &res) {
    guard(res, [&] {
      auto body = parse_body(req);
      auto code = body.value("code", "");
      if (code.empty()) { throw std::invalid_argument("code is required"); }
      stream_exec(res, code, exec_timeout(body), false);
    });
  });

  svr.Post("/sh", [](const httplib::Request &req, httplib::Response &res) {
    guard(res, [&] {
      auto body = parse_body(req);
      auto cmd = body.value("cmd", "");
      if (cmd.empty()) { throw std::invalid_argument("cmd is required"); }
      auto &cli = need_session();
      auto t = exec_timeout(body);
      std::lock_guard<std::mutex> lock(g.lock);
      auto r = kbridge::ops::sh(cli, cmd, body.value("cwd", "/kaggle/working"), t);
      send_json(res, r, r.value("status", "") == "timeout" ? 504 : 200);
    });
  });

  svr.Post("/sh/stream", [](const httplib::Request &req, httplib::Response &res) {
    guard(res, [&] {
      auto body = parse_body(req);
      auto cmd = body.value("cmd", "");
      if (cmd.empty()) { throw std::invalid_argument("cmd is required"); }
      auto &cli = need_session();
      auto t = exec_timeout(body);
      {
        std::lock_guard<std::mutex> lock(g.lock);
        kbridge::ops::ensure_agent(cli);
      }
      auto code = kbridge::ops::sh_code(cmd, body.value("cwd", "/kaggle/working"),
                                       std::max(t - 5.0, 1.0));
      stream_exec(res, code, t, true);
    });
  });

  svr.Get("/gpu", [](const httplib::Request &, httplib::Response &res) {
    guard(res, [&] {
      auto &cli = need_session();
      std::lock_guard<std::mutex> lock(g.lock);
      auto r = kbridge::ops::gpu(cli);
      r["ok"] = true;
      send_json(res, r);
    });
  });

  // --- ファイル -----------------------------------------------------------
  svr.Post("/upload", [](const httplib::Request &req, httplib::Response &res) {
    guard(res, [&] {
      auto body = parse_body(req);
      auto path = safe_path(body.value("path", ""));
      if (path.empty()) { throw std::invalid_argument("path is required"); }
      bool has_text = body.contains("text"), has_b64 = body.contains("content_b64");
      if (has_text == has_b64) {
        throw std::invalid_argument("give exactly one of text or content_b64");
      }
      std::string data = has_text ? body.value("text", "")
                                  : kbridge::b64_decode(body.value("content_b64", ""));
      auto &cli = need_session();
      std::lock_guard<std::mutex> lock(g.lock);
      auto size = cli.put_file(path, data);
      send_json(res, json{{"ok", true}, {"path", path}, {"size", size}});
    });
  });

  svr.Get("/download", [](const httplib::Request &req, httplib::Response &res) {
    guard(res, [&] {
      auto path = safe_path(req.get_param_value("path"));
      auto &cli = need_session();
      std::string data;
      {
        std::lock_guard<std::mutex> lock(g.lock);
        data = cli.get_file(path);
      }
      if (req.has_param("raw") && req.get_param_value("raw") != "0") {
        res.set_content(data, "application/octet-stream");
        return;
      }
      send_json(res, json{{"ok", true}, {"path", path}, {"size", data.size()},
                          {"content_b64", kbridge::b64_encode(data)}});
    });
  });

  svr.Get("/ls", [](const httplib::Request &req, httplib::Response &res) {
    guard(res, [&] {
      auto path = safe_path(req.has_param("path") ? req.get_param_value("path") : "");
      auto &cli = need_session();
      std::lock_guard<std::mutex> lock(g.lock);
      auto r = cli.ls(path);
      r["ok"] = true;
      send_json(res, r);
    });
  });

  svr.Post("/mkdir", [](const httplib::Request &req, httplib::Response &res) {
    guard(res, [&] {
      auto body = parse_body(req);
      auto path = safe_path(body.value("path", ""));
      if (path.empty()) { throw std::invalid_argument("path is required"); }
      auto &cli = need_session();
      std::lock_guard<std::mutex> lock(g.lock);
      cli.mkdirs(path);
      send_json(res, json{{"ok", true}, {"path", path}});
    });
  });

  svr.Post("/rm", [](const httplib::Request &req, httplib::Response &res) {
    guard(res, [&] {
      auto body = parse_body(req);
      auto path = safe_path(body.value("path", ""));
      if (path.empty()) { throw std::invalid_argument("path is required"); }
      auto &cli = need_session();
      std::lock_guard<std::mutex> lock(g.lock);
      cli.rm(path);
      send_json(res, json{{"ok", true}, {"path", path}});
    });
  });

  // --- ジョブ -------------------------------------------------------------
  svr.Post("/job", [](const httplib::Request &req, httplib::Response &res) {
    guard(res, [&] {
      auto body = parse_body(req);
      json cmd = body.contains("cmd") ? body["cmd"] : json();
      json code = body.contains("code") ? body["code"] : json();
      if (cmd.is_null() == code.is_null()) {
        throw std::invalid_argument("give exactly one of cmd or code");
      }
      auto &cli = need_session();
      std::lock_guard<std::mutex> lock(g.lock);
      auto r = kbridge::ops::job_start(
          cli, cmd, code, body.value("name", "job"),
          body.value("cwd", "/kaggle/working"),
          body.contains("env") ? body["env"] : json());
      r["ok"] = true;
      send_json(res, r);
    });
  });

  svr.Get("/job", [](const httplib::Request &, httplib::Response &res) {
    guard(res, [&] {
      auto &cli = need_session();
      std::lock_guard<std::mutex> lock(g.lock);
      send_json(res, json{{"ok", true}, {"jobs", kbridge::ops::job_list(cli)}});
    });
  });

  svr.Get(R"(/job/([^/]+)/log)", [](const httplib::Request &req,
                                    httplib::Response &res) {
    guard(res, [&] {
      auto &cli = need_session();
      long long offset = req.has_param("offset")
                             ? std::stoll(req.get_param_value("offset"))
                             : 0;
      long long max_bytes =
          req.has_param("max") ? std::stoll(req.get_param_value("max")) : 65536;
      std::lock_guard<std::mutex> lock(g.lock);
      auto r = kbridge::ops::job_log(cli, req.matches[1], offset, max_bytes);
      r["ok"] = true;
      send_json(res, r);
    });
  });

  svr.Post(R"(/job/([^/]+)/kill)", [](const httplib::Request &req,
                                      httplib::Response &res) {
    guard(res, [&] {
      auto &cli = need_session();
      std::lock_guard<std::mutex> lock(g.lock);
      auto r = kbridge::ops::job_kill(cli, req.matches[1]);
      r["ok"] = true;
      send_json(res, r);
    });
  });

  svr.Get(R"(/job/([^/]+))", [](const httplib::Request &req,
                                httplib::Response &res) {
    guard(res, [&] {
      auto &cli = need_session();
      std::lock_guard<std::mutex> lock(g.lock);
      auto r = kbridge::ops::job_status(cli, req.matches[1]);
      r["ok"] = true;
      send_json(res, r);
    });
  });

  svr.Delete(R"(/job/([^/]+))", [](const httplib::Request &req,
                                   httplib::Response &res) {
    guard(res, [&] {
      auto &cli = need_session();
      std::lock_guard<std::mutex> lock(g.lock);
      auto r = kbridge::ops::job_rm(cli, req.matches[1]);
      r["ok"] = true;
      send_json(res, r);
    });
  });

  // --- バッチ -------------------------------------------------------------
  svr.Post("/batch/push", [](const httplib::Request &req, httplib::Response &res) {
    guard(res, [&] {
      auto body = parse_body(req);
      auto dir = body.value("dir", "");
      if (dir.empty()) { throw std::invalid_argument("dir is required"); }
      send_json(res, kbridge::batch::push(dir));
    });
  });

  svr.Get("/batch/status", [](const httplib::Request &req, httplib::Response &res) {
    guard(res, [&] {
      send_json(res, kbridge::batch::status(req.get_param_value("id")));
    });
  });

  svr.Post("/batch/output", [](const httplib::Request &req, httplib::Response &res) {
    guard(res, [&] {
      auto body = parse_body(req);
      if (body.value("id", "").empty() || body.value("dir", "").empty()) {
        throw std::invalid_argument("id and dir are required");
      }
      send_json(res, kbridge::batch::output(body["id"], body["dir"]));
    });
  });

  svr.Post("/batch/pull", [](const httplib::Request &req, httplib::Response &res) {
    guard(res, [&] {
      auto body = parse_body(req);
      if (body.value("id", "").empty() || body.value("dir", "").empty()) {
        throw std::invalid_argument("id and dir are required");
      }
      send_json(res, kbridge::batch::pull(body["id"], body["dir"]));
    });
  });

  svr.set_exception_handler([](const httplib::Request &, httplib::Response &res,
                               std::exception_ptr ep) {
    std::string what = "internal error";
    try {
      std::rethrow_exception(ep);
    } catch (const std::exception &e) {
      what = e.what();
    } catch (...) {
    }
    send_error(res, what, 500);
  });
}

// ---------------------------------------------------------------- main

int main(int argc, char **argv) {
  std::string host = "127.0.0.1";
  int port = 8787;
  std::string url;

  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    auto next = [&](const char *name) -> std::string {
      if (i + 1 >= argc) {
        std::fprintf(stderr, "%s needs a value\n", name);
        std::exit(2);
      }
      return argv[++i];
    };
    if (a == "--host") {
      host = next("--host");
    } else if (a == "--port") {
      port = std::stoi(next("--port"));
    } else if (a == "--api-key") {
      g.api_key = next("--api-key");
    } else if (a == "--url") {
      url = next("--url");
    } else if (a == "--agent") {
      kbridge::ops::agent_path() = next("--agent");
    } else if (a == "--insecure") {
      g.insecure = true;
    } else if (a == "--dump-codegen") {
      // カーネルへ送るコードをそのまま出す。tests/parity.py が python 版の
      // ops.py と 1 文字単位で突き合わせるために使う。
      json out{{"inject", kbridge::ops::inject_code()},
               {"call", kbridge::ops::call_code(
                            "log", json{{"job_id", "x"}, {"offset", 0},
                                        {"max_bytes", 65536}})},
               {"sh", kbridge::ops::sh_code("echo hi", "/kaggle/working", 295.0)}};
      std::printf("%s", out.dump().c_str());
      return 0;
    } else if (a == "-h" || a == "--help") {
      std::printf(
          "kbridge_server (cpp) %s\n"
          "  --host H        bind するアドレス（既定 127.0.0.1）\n"
          "  --port P        ポート（既定 8787）\n"
          "  --api-key K     X-Bridge-Key を必須にする\n"
          "  --url U         起動時に接続する VSCode Compatible URL\n"
          "  --agent PATH    kaggle/kbagent.py の場所\n"
          "  --insecure      サーバ証明書を検証しない（試験用）\n",
          VERSION);
      return 0;
    } else {
      std::fprintf(stderr, "unknown option: %s\n", a.c_str());
      return 2;
    }
  }

  if (g.api_key.empty()) {
    if (const char *k = std::getenv("KBRIDGE_API_KEY")) { g.api_key = k; }
  }
  if (url.empty()) { url = default_url(); }
  if (!url.empty()) {
    try {
      auto client = std::make_unique<JupyterClient>(url);
      client->set_insecure(g.insecure);
      auto info = client->connect();
      g.client = std::move(client);
      g.url = url;
      std::printf("connected: %s kernel=%s (reuse=%s)\n",
                  info.value("base_url", "").c_str(),
                  info.value("kernel_id", "").c_str(),
                  info.value("reuse", false) ? "true" : "false");
    } catch (const std::exception &e) {
      // 起動は続ける。後から POST /session できる
      std::printf("startup connect failed: %s\n", e.what());
    }
  }
  if (host != "127.0.0.1" && host != "localhost" && host != "::1" &&
      g.api_key.empty()) {
    std::printf("warning: loopback 以外に bind するなら --api-key を付けること\n");
  }

  httplib::Server svr;
  svr.set_payload_max_length(512ull * 1024 * 1024);
  svr.set_read_timeout(3600, 0);
  svr.set_write_timeout(3600, 0);
  install_routes(svr);

  std::printf("kbridge (cpp) listening on http://%s:%d\n", host.c_str(), port);
  std::fflush(stdout);
  if (!svr.listen(host, port)) {
    std::fprintf(stderr, "cannot bind %s:%d\n", host.c_str(), port);
    return 1;
  }
  return 0;
}
