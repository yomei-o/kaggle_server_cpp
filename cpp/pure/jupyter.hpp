// Jupyter Server クライアント（REST + カーネル WebSocket プロトコル 5.3）。
//
// python/kbridge/jupyter.py と同じ手順・同じ結果になるように実装している。
//   1. GET  <base>/                          -> Cookie の _xsrf を取得
//   2. GET  <base>/api/kernels               -> 既存カーネルがあれば再利用
//                                               （Kaggle の GPU を握っているのは Notebook 本体）
//      無ければ POST <base>/api/sessions     -> 新規カーネル
//   3. WSS  <base>/api/kernels/<kid>/channels?session_id=... で常時接続
//   4. execute_request を投げ、parent_header.msg_id で自分宛の出力だけ拾う
//
// サブプロトコル v1.kernel.websocket.jupyter.org は要求しない（バイナリ多重化になるため）。
#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <random>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "httplib.h"
#include "json.hpp"

#include "b64.hpp"
#include "jurl.hpp"

namespace kbridge {

using json = nlohmann::json;

// 上流 (Kaggle) 側のエラー。HTTP 502 相当。
class JupyterError : public std::runtime_error {
 public:
  explicit JupyterError(const std::string &what) : std::runtime_error(what) {}
};

inline double now_seconds() {
  using namespace std::chrono;
  return duration_cast<duration<double>>(steady_clock::now().time_since_epoch())
      .count();
}

inline std::string hex_id(size_t bytes = 16) {
  static std::mt19937_64 rng(
      (uint64_t)std::chrono::system_clock::now().time_since_epoch().count() ^
      (uint64_t)std::hash<std::thread::id>()(std::this_thread::get_id()));
  static std::mutex m;
  static const char *HEX = "0123456789abcdef";
  std::lock_guard<std::mutex> lock(m);
  std::string out;
  for (size_t i = 0; i < bytes * 2; ++i) { out += HEX[rng() & 15]; }
  return out;
}

inline std::string iso_now() {
  auto t = std::chrono::system_clock::to_time_t(std::chrono::system_clock::now());
  std::tm tm {};
#ifdef _WIN32
  gmtime_s(&tm, &t);
#else
  gmtime_r(&t, &tm);
#endif
  char buf[64];
  std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%S", &tm);
  return std::string(buf) + ".000000Z";
}

// /exec の応答そのもの（spec/API.md 2.）
struct ExecResult {
  bool ok = true;
  std::string status = "ok";
  std::string out;      // stdout
  std::string err;      // stderr
  std::string result;
  std::string ename, evalue;
  std::vector<std::string> traceback;
  json execution_count = json();  // null か数値
  double elapsed = 0.0;

  json to_json() const {
    return json{{"ok", ok},         {"status", status},
                {"stdout", out},    {"stderr", err},
                {"result", result}, {"ename", ename},
                {"evalue", evalue}, {"traceback", traceback},
                {"execution_count", execution_count}, {"elapsed", elapsed}};
  }
};

// NDJSON の 1 行に相当するイベント。ストリーミング時に逐次呼ばれる。
using EventFn = std::function<void(const json &)>;

class JupyterClient {
 public:
  explicit JupyterClient(const std::string &url) {
    parse_jupyter_url(url, &base_url_, &token_);
    Url u = split_url(base_url_);
    origin_ = u.origin();
    base_path_ = u.path;
    while (!base_path_.empty() && base_path_.back() == '/') { base_path_.pop_back(); }
    session_id_ = hex_id();
  }

  ~JupyterClient() { disconnect(); }

  // 最後に Kaggle 側へ話しかけた時刻（epoch 秒）。
  // kbridge_server.cpp の keep-alive スレッドがこれを見て発火を決める。
  std::atomic<double> last_activity{now_seconds()};

  std::string safe_base() const { return mask_base(base_url_, token_); }

  json info() const {
    return json{{"base_url", safe_base()},
                {"kernel_id", kernel_id_.empty() ? json() : json(kernel_id_)},
                {"session_id", session_id_},
                {"kernel_name", kernel_name_.empty() ? json() : json(kernel_name_)},
                {"reuse", reused_},
                {"connected", !kernel_id_.empty() && ws_ != nullptr}};
  }

  bool has_kernel() const { return !kernel_id_.empty(); }
  bool connected() const { return !kernel_id_.empty() && ws_ != nullptr; }
  const std::string &kernel_id() const { return kernel_id_; }

  // ------------------------------------------------------------ lifecycle
  json connect(bool new_kernel = false) {
    // 1. XSRF cookie（トップページが 404 でも Cookie は付いてくることがある）
    try {
      http("GET", url_of({}), "");
    } catch (const JupyterError &) {
    }

    // 2. カーネル
    if (!new_kernel) {
      try {
        auto body = http("GET", url_of({"api", "kernels"}), "");
        auto arr = json::parse(body, nullptr, false);
        if (arr.is_array()) {
          for (const auto &k : arr) {
            if (k.value("execution_state", "") != "dead" && k.contains("id")) {
              kernel_id_ = k.value("id", "");
              kernel_name_ = k.value("name", "python3");
              reused_ = true;
              break;
            }
          }
        }
      } catch (const JupyterError &) {
      }
    }
    if (kernel_id_.empty()) {
      json req{{"path", "kbridge-" + session_id_.substr(0, 8) + ".ipynb"},
               {"type", "notebook"},
               {"name", ""},
               {"kernel", {{"name", "python3"}}}};
      auto body = http("POST", url_of({"api", "sessions"}), req.dump());
      auto sess = json::parse(body, nullptr, false);
      if (!sess.is_object() || !sess.contains("kernel")) {
        throw JupyterError("could not create session: " + body.substr(0, 300));
      }
      kernel_id_ = sess["kernel"].value("id", "");
      kernel_name_ = sess["kernel"].value("name", "python3");
      reused_ = false;
    }

    connect_ws();
    return info();
  }

  void ensure() {
    if (kernel_id_.empty()) { throw JupyterError("no session; POST /session first"); }
    if (ws_ == nullptr) { connect_ws(); }
  }

  void disconnect() {
    stop_ = true;
    std::shared_ptr<httplib::ws::WebSocketClient> ws;
    {
      std::lock_guard<std::mutex> lock(ws_mutex_);
      ws = ws_;
      ws_.reset();
    }
    if (ws) { ws->close(); }
    reap_reader();
  }

  // リーダスレッドを畳む。close フレームを送っても相手が TCP を閉じてくれない
  // 場合、read() は読み取りタイムアウトまで返ってこない。そこで無限に待つと
  // ブリッジ全体が止まるので、少しだけ待って駄目なら手放す。リーダは自分で
  // shared_ptr を持っているので、後から安全に終われる。
  void reap_reader() {
    if (!reader_.joinable()) { return; }
    std::unique_lock<std::mutex> lock(done_mutex_);
    bool done = done_cv_.wait_for(lock, std::chrono::seconds(10),
                                  [this] { return reader_done_.load(); });
    lock.unlock();
    if (done) {
      reader_.join();
    } else {
      reader_.detach();
    }
  }

  void shutdown() {
    disconnect();
    if (!kernel_id_.empty()) {
      try {
        http("DELETE", url_of({"api", "kernels", kernel_id_}), "");
      } catch (const JupyterError &) {
      }
      kernel_id_.clear();
    }
  }

  void interrupt() {
    ensure();
    http("POST", url_of({"api", "kernels", kernel_id_, "interrupt"}), "{}");
  }

  std::string restart() {
    ensure();
    disconnect();
    auto body = http("POST", url_of({"api", "kernels", kernel_id_, "restart"}), "{}");
    auto j = json::parse(body, nullptr, false);
    if (j.is_object() && j.contains("id")) { kernel_id_ = j.value("id", kernel_id_); }
    connect_ws();
    return kernel_id_;
  }

  // -------------------------------------------------------------- execute
  ExecResult execute(const std::string &code, double timeout = 300.0,
                     const EventFn &on_event = nullptr) {
    last_activity = now_seconds();
    ensure();
    auto msg_id = hex_id();
    json msg{
        {"header",
         {{"msg_id", msg_id}, {"username", "kbridge"}, {"session", session_id_},
          {"date", iso_now()}, {"msg_type", "execute_request"}, {"version", "5.3"}}},
        {"parent_header", json::object()},
        {"metadata", json::object()},
        {"content",
         {{"code", code}, {"silent", false}, {"store_history", true},
          {"user_expressions", json::object()}, {"allow_stdin", false},
          {"stop_on_error", true}}},
        {"channel", "shell"},
        {"buffers", json::array()}};

    auto chan = std::make_shared<Chan>();
    {
      std::lock_guard<std::mutex> lock(subs_mutex_);
      subs_[msg_id] = chan;
    }
    struct Unsub {
      JupyterClient *self;
      std::string id;
      ~Unsub() {
        std::lock_guard<std::mutex> lock(self->subs_mutex_);
        self->subs_.erase(id);
      }
    } unsub{this, msg_id};

    ExecResult r;
    double started = now_seconds();
    double deadline = started + timeout;
    bool got_reply = false, got_idle = false, interrupted = false;

    // 送れなかったら 1 度だけ張り直して投げ直す（セッションが長いと、
    // 上流の都合で黙って切れていることがある）。
    if (!send_ws(msg.dump())) {
      reconnect_ws();
      if (!send_ws(msg.dump())) {
        throw JupyterError("websocket not connected: " +
                           (ws_error_.empty() ? "send failed" : ws_error_));
      }
    }

    while (true) {
      if (now_seconds() >= deadline) {
        if (interrupted) { break; }
        // 一度だけ割り込みを投げ、後片付けの出力を 10 秒だけ待つ
        interrupted = true;
        r.status = "timeout";
        r.ok = false;
        deadline = now_seconds() + 10.0;
        try {
          interrupt();
        } catch (const JupyterError &) {
        }
        continue;
      }

      json ev;
      if (!chan->pop(ev, std::min(deadline - now_seconds(), 1.0))) {
        if (chan->closed) {
          r.status = "abort";
          r.ok = false;
          r.evalue = ws_error_.empty() ? "websocket closed" : ws_error_;
          break;
        }
        continue;
      }

      std::string mtype = ev.value("msg_type", "");
      if (mtype.empty() && ev.contains("header")) {
        mtype = ev["header"].value("msg_type", "");
      }
      const json &content =
          ev.contains("content") && ev["content"].is_object() ? ev["content"] : empty_;

      if (mtype == "stream") {
        auto name = content.value("name", "stdout");
        auto text = content.value("text", "");
        if (name == "stderr") { r.err += text; } else { r.out += text; }
        if (on_event && !text.empty()) {
          on_event(json{{"t", "out"}, {"stream", name}, {"d", text}});
        }
      } else if (mtype == "execute_result" || mtype == "display_data") {
        std::string text;
        if (content.contains("data") && content["data"].is_object()) {
          text = content["data"].value("text/plain", "");
        }
        if (!text.empty()) {
          r.result += r.result.empty() ? text : "\n" + text;
          if (on_event) { on_event(json{{"t", "result"}, {"d", text}}); }
        }
      } else if (mtype == "error") {
        r.ename = content.value("ename", "");
        r.evalue = content.value("evalue", "");
        r.traceback = content.value("traceback", std::vector<std::string>{});
        if (r.status == "ok") {
          r.status = "error";
          r.ok = false;
        }
        if (on_event) {
          on_event(json{{"t", "error"}, {"ename", r.ename}, {"evalue", r.evalue},
                        {"traceback", r.traceback}});
        }
      } else if (mtype == "execute_reply") {
        got_reply = true;
        if (content.contains("execution_count")) {
          r.execution_count = content["execution_count"];
        }
        if (content.value("status", "") == "error" && r.status == "ok") {
          r.status = "error";
          r.ok = false;
          r.ename = content.value("ename", r.ename);
          r.evalue = content.value("evalue", r.evalue);
          r.traceback = content.value("traceback", r.traceback);
        }
      } else if (mtype == "status") {
        if (content.value("execution_state", "") == "idle") { got_idle = true; }
      }
      if (got_reply && got_idle) { break; }
    }

    r.elapsed = std::round((now_seconds() - started) * 1000.0) / 1000.0;
    last_activity = now_seconds();
    return r;
  }

  // 最後の 1 行に JSON を print するコードを実行し、その JSON を返す。
  json execute_json(const std::string &code, double timeout = 120.0) {
    auto r = execute(code, timeout);
    if (r.status != "ok") {
      throw JupyterError(!r.evalue.empty()
                             ? r.evalue
                             : (!r.err.empty() ? r.err
                                               : "kernel returned status=" + r.status));
    }
    // 後ろから見て最初に JSON として読める行を採用する
    std::vector<std::string> lines;
    std::string cur;
    for (char c : r.out) {
      if (c == '\n') {
        lines.push_back(cur);
        cur.clear();
      } else if (c != '\r') {
        cur += c;
      }
    }
    if (!cur.empty()) { lines.push_back(cur); }
    for (auto it = lines.rbegin(); it != lines.rend(); ++it) {
      auto s = trim(*it);
      if (s.empty() || (s[0] != '{' && s[0] != '[')) { continue; }
      auto j = json::parse(s, nullptr, false);
      if (!j.is_discarded()) { return j; }
    }
    auto tail = r.out.size() > 400 ? r.out.substr(r.out.size() - 400) : r.out;
    throw JupyterError("no JSON on stdout: " + tail);
  }

  // ------------------------------------------------------------- contents
  json ls(const std::string &path) {
    auto body = http("GET", contents_url(path, "content=1"), "");
    auto j = json::parse(body, nullptr, false);
    json entries = json::array();
    if (j.is_object() && j.contains("content") && j["content"].is_array()) {
      for (const auto &e : j["content"]) {
        entries.push_back(json{{"name", e.value("name", "")},
                               {"path", e.value("path", "")},
                               {"type", e.value("type", "")},
                               {"size", e.contains("size") ? e["size"] : json()}});
      }
    }
    std::sort(entries.begin(), entries.end(), [](const json &a, const json &b) {
      bool ad = a.value("type", "") == "directory";
      bool bd = b.value("type", "") == "directory";
      if (ad != bd) { return ad; }
      return a.value("name", "") < b.value("name", "");
    });
    return json{{"path", strip_slashes(path)}, {"entries", entries}};
  }

  std::string get_file(const std::string &path) {
    auto body = http("GET", contents_url(path, "content=1&format=base64"), "");
    auto j = json::parse(body, nullptr, false);
    if (!j.is_object() || j.value("type", "") == "directory") {
      throw JupyterError("not a file: " + path);
    }
    return b64_decode(j.value("content", ""));
  }

  size_t put_file(const std::string &path, const std::string &data) {
    auto parent = parent_of(strip_slashes(path));
    if (!parent.empty()) { mkdirs(parent); }
    json body{{"type", "file"}, {"format", "base64"}, {"content", b64_encode(data)}};
    http("PUT", contents_url(path, ""), body.dump(), 600);
    return data.size();
  }

  void mkdir(const std::string &path) {
    auto p = strip_slashes(path);
    if (p.empty()) { return; }
    http("PUT", contents_url(p, ""), json{{"type", "directory"}}.dump());
  }

  void mkdirs(const std::string &path) {
    std::string acc;
    for (const auto &seg : path_segments(strip_slashes(path))) {
      acc += acc.empty() ? seg : "/" + seg;
      try {
        mkdir(acc);
      } catch (const JupyterError &) {
        // 既にある
      }
    }
  }

  void rm(const std::string &path) { http("DELETE", contents_url(path, ""), ""); }

 private:
  // ------------------------------------------------------------- 受信待ち
  struct Chan {
    std::mutex m;
    std::condition_variable cv;
    std::deque<json> q;
    bool closed = false;

    void push(json v) {
      {
        std::lock_guard<std::mutex> lock(m);
        q.push_back(std::move(v));
      }
      cv.notify_all();
    }
    void close() {
      {
        std::lock_guard<std::mutex> lock(m);
        closed = true;
      }
      cv.notify_all();
    }
    bool pop(json &out, double seconds) {
      std::unique_lock<std::mutex> lock(m);
      if (seconds > 0) {
        cv.wait_for(lock, std::chrono::milliseconds((long long)(seconds * 1000)),
                    [&] { return !q.empty() || closed; });
      }
      if (q.empty()) { return false; }
      out = std::move(q.front());
      q.pop_front();
      return true;
    }
  };

  // ----------------------------------------------------------------- HTTP
  std::string url_of(const std::vector<std::string> &parts,
                     const std::string &extra_query = "") const {
    std::string path = base_path_;
    for (const auto &p : parts) {
      auto s = strip_slashes(p);
      if (!s.empty()) { path += "/" + s; }
    }
    if (path.empty()) { path = "/"; }
    std::string q = extra_query;
    if (!token_.empty()) {
      if (!q.empty()) { q += "&"; }
      q += "token=" + encode_query(token_);
    }
    return q.empty() ? path : path + "?" + q;
  }

  std::string contents_url(const std::string &path,
                           const std::string &extra_query) const {
    auto p = strip_slashes(path);
    std::string full = base_path_ + "/api/contents";
    if (!p.empty()) { full += "/" + encode_path(p); }
    std::string q = extra_query;
    if (!token_.empty()) {
      if (!q.empty()) { q += "&"; }
      q += "token=" + encode_query(token_);
    }
    return q.empty() ? full : full + "?" + q;
  }

  httplib::Headers headers(bool with_json_body) const {
    httplib::Headers h{
        {"User-Agent",
         "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:135.0) Gecko/20100101 "
         "Firefox/135.0"},
        {"Accept", "application/json"}};
    if (!token_.empty()) { h.emplace("Authorization", "token " + token_); }
    if (!xsrf_.empty()) { h.emplace("X-XSRFToken", xsrf_); }
    auto c = cookie_header();
    if (!c.empty()) { h.emplace("Cookie", c); }
    if (with_json_body) { h.emplace("Content-Type", "application/json"); }
    return h;
  }

  std::string cookie_header() const {
    std::string out;
    for (const auto &kv : cookies_) {
      if (!out.empty()) { out += "; "; }
      out += kv.first + "=" + kv.second;
    }
    return out;
  }

  void absorb_cookies(const httplib::Headers &h) {
    auto range = h.equal_range("Set-Cookie");
    for (auto it = range.first; it != range.second; ++it) {
      auto kv = it->second.substr(0, it->second.find(';'));
      auto eq = kv.find('=');
      if (eq == std::string::npos) { continue; }
      auto k = trim(kv.substr(0, eq));
      auto v = trim(kv.substr(eq + 1));
      cookies_[k] = v;
      if (k == "_xsrf") { xsrf_ = v; }
    }
  }

  std::string http(const std::string &method, const std::string &path,
                   const std::string &body, int timeout_sec = 60) {
    httplib::Client cli(origin_);
    cli.set_connection_timeout(30, 0);
    cli.set_read_timeout(timeout_sec, 0);
    cli.set_write_timeout(timeout_sec, 0);
    cli.set_follow_location(false);
#ifdef CPPHTTPLIB_SSL_ENABLED
    cli.enable_server_certificate_verification(!insecure_);
#endif
    auto h = headers(!body.empty());
    httplib::Result res;
    if (method == "GET") {
      res = cli.Get(path, h);
    } else if (method == "POST") {
      res = cli.Post(path, h, body, "application/json");
    } else if (method == "PUT") {
      res = cli.Put(path, h, body, "application/json");
    } else if (method == "DELETE") {
      res = cli.Delete(path, h, body, "application/json");
    } else {
      throw JupyterError("unsupported method " + method);
    }
    if (!res) {
      throw JupyterError(method + " " + mask_base(path, token_) + " -> " +
                         httplib::to_string(res.error()));
    }
    absorb_cookies(res->headers);
    if (res->status >= 400) {
      auto detail = res->body.size() > 500 ? res->body.substr(0, 500) : res->body;
      throw JupyterError(method + " " + mask_base(path, token_) + " -> HTTP " +
                         std::to_string(res->status) + ": " + detail);
    }
    return res->body;
  }

  // ------------------------------------------------------------ WebSocket
  std::string ws_url() const {
    auto scheme = origin_.rfind("https://", 0) == 0 ? std::string("wss://")
                                                    : std::string("ws://");
    auto host = origin_.substr(origin_.find("://") + 3);
    return scheme + host + base_path_ + "/api/kernels/" + kernel_id_ +
           "/channels?session_id=" + encode_query(session_id_);
  }

  bool send_ws(const std::string &payload) {
    std::lock_guard<std::mutex> lock(ws_mutex_);
    return ws_ && ws_->send(payload);
  }

  void reconnect_ws() {
    disconnect();
    stop_ = false;
    connect_ws();
  }

  void connect_ws() {
    {
      std::lock_guard<std::mutex> lock(ws_mutex_);
      if (ws_) { return; }
    }
    reap_reader();   // ロックを持たずに畳む
    std::lock_guard<std::mutex> lock(ws_mutex_);
    if (ws_) { return; }
    httplib::Headers h{{"Origin", origin_},
                       {"Cache-Control", "no-cache"},
                       {"Pragma", "no-cache"}};
    if (!token_.empty()) { h.emplace("Authorization", "token " + token_); }
    auto c = cookie_header();
    if (!c.empty()) { h.emplace("Cookie", c); }

    auto ws = std::make_shared<httplib::ws::WebSocketClient>(ws_url(), h);
    ws->set_connection_timeout(30, 0);
    // Jupyter のカーネルチャンネルはクライアントからの ping を必要としない。
    // 既定のまま 30 秒ごとに ping を投げると Kaggle のプロキシ越しで接続が
    // 落ちることがあったので止める（python 版も ping は投げていない）。
    ws->set_websocket_ping_interval(0);
    // ping を止めた分、無通信で切られないよう読み取りは長めに待つ。
    ws->set_read_timeout(3600, 0);
#ifdef CPPHTTPLIB_SSL_ENABLED
    ws->enable_server_certificate_verification(!insecure_);
#endif
    if (!ws->connect()) {
      throw JupyterError("websocket connect failed: " + mask_base(ws_url(), token_));
    }
    ws_ = ws;
    ws_error_.clear();
    stop_ = false;
    reader_done_ = false;
    reader_ = std::thread([this, ws] { reader_loop(ws); });
  }

  void reader_loop(std::shared_ptr<httplib::ws::WebSocketClient> ws) {
    std::string msg;
    while (!stop_) {
      auto rr = ws->read(msg);
      if (rr == httplib::ws::Fail) {
        if (!stop_) { ws_error_ = "websocket closed"; }
        break;
      }
      if (rr != httplib::ws::Text) { continue; }
      auto obj = json::parse(msg, nullptr, false);
      if (obj.is_discarded() || !obj.is_object()) { continue; }
      std::string parent;
      if (obj.contains("parent_header") && obj["parent_header"].is_object()) {
        parent = obj["parent_header"].value("msg_id", "");
      }
      if (parent.empty()) { continue; }
      std::shared_ptr<Chan> chan;
      {
        std::lock_guard<std::mutex> lock(subs_mutex_);
        auto it = subs_.find(parent);
        if (it != subs_.end()) { chan = it->second; }
      }
      if (chan) { chan->push(std::move(obj)); }
    }
    // 接続は死んだので手放す。こうしておけば次の ensure() で張り直される
    // （python 版 _reader_loop と同じ後始末。これが無いと一度切れたきり
    //  "websocket not connected" を返し続ける）。
    {
      std::lock_guard<std::mutex> lock(ws_mutex_);
      if (ws_ == ws) { ws_.reset(); }
    }
    // 待っている実行を起こす
    {
      std::lock_guard<std::mutex> lock(subs_mutex_);
      for (auto &kv : subs_) { kv.second->close(); }
    }
    {
      std::lock_guard<std::mutex> lock(done_mutex_);
      reader_done_ = true;
    }
    done_cv_.notify_all();
  }

  // ---------------------------------------------------------------- utils
  static std::string trim(const std::string &s) {
    auto b = s.find_first_not_of(" \t\r\n");
    if (b == std::string::npos) { return ""; }
    auto e = s.find_last_not_of(" \t\r\n");
    return s.substr(b, e - b + 1);
  }

  static std::string strip_slashes(const std::string &s) {
    size_t b = 0, e = s.size();
    while (b < e && (s[b] == '/' || s[b] == '\\')) { ++b; }
    while (e > b && (s[e - 1] == '/' || s[e - 1] == '\\')) { --e; }
    return s.substr(b, e - b);
  }

  static std::string parent_of(const std::string &path) {
    auto pos = path.find_last_of('/');
    return pos == std::string::npos ? "" : path.substr(0, pos);
  }

  std::string base_url_, token_, origin_, base_path_;
  std::string session_id_, kernel_id_, kernel_name_;
  bool reused_ = false;
  bool insecure_ = false;
  std::string xsrf_;
  std::map<std::string, std::string> cookies_;

  std::shared_ptr<httplib::ws::WebSocketClient> ws_;
  std::mutex ws_mutex_;
  std::thread reader_;
  std::atomic<bool> stop_{false};
  std::atomic<bool> reader_done_{true};
  std::mutex done_mutex_;
  std::condition_variable done_cv_;
  std::string ws_error_;

  std::map<std::string, std::shared_ptr<Chan>> subs_;
  std::mutex subs_mutex_;
  json empty_ = json::object();

 public:
  void set_insecure(bool v) { insecure_ = v; }
};

}  // namespace kbridge
