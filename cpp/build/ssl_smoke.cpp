// ツールチェーン検証用スモーク。kbridge 本体を書く前に、この 4 つが通ることを確かめる。
//   1. HTTPS クライアント（外部サイト・システムルート証明書で検証あり）
//   2. HTTPS サーバ（自己署名）を立てて、自分のクライアントから検証ありで叩く
//   3. HTTP サーバ（kbridge のローカル API はこちら）
//   4. nlohmann/json の往復
//
// ビルド:  sh cpp/build/gcc.sh cpp/build/ssl_smoke.cpp -o ssl_smoke.exe
// 実行  :  ./ssl_smoke.exe
//
// httplib 0.46.1 は mbedTLS をネイティブに扱える（CPPHTTPLIB_MBEDTLS_SUPPORT）。
// しかも <mbedtlspp.hpp>（ヘッダオンリー版 mbedTLS）があればそれを使うようになっているので、
// OpenSSL も OpenSSL 互換シムも要らない。
#include <chrono>
#include <cstdio>
#include <string>
#include <thread>

#include "httplib.h"
#include "json.hpp"

static int failures = 0;

static void check(bool cond, const char *what, const std::string &detail = "") {
  std::printf("%s %s%s%s\n", cond ? "[ OK ]" : "[FAIL]", what,
              detail.empty() ? "" : "  -> ", detail.c_str());
  if (!cond) { failures++; }
}

int main() {
  std::printf("httplib %s / json %d.%d.%d / mbedtls %s\n", CPPHTTPLIB_VERSION,
              NLOHMANN_JSON_VERSION_MAJOR, NLOHMANN_JSON_VERSION_MINOR,
              NLOHMANN_JSON_VERSION_PATCH, MBEDTLS_VERSION_STRING);

  // --- 1. HTTPS クライアント: 証明書検証あり ---------------------------------
  {
    httplib::SSLClient cli("www.kaggle.com", 443);
    cli.enable_server_certificate_verification(true);
    cli.set_connection_timeout(20, 0);
    auto res = cli.Get("/");
    check(res && (res->status == 200 || res->status == 301 || res->status == 302), "https client -> www.kaggle.com (verify on, system roots)",
          res ? ("status " + std::to_string(res->status))
              : ("error " + std::to_string((int)res.error())));
  }

  // --- 2. HTTPS サーバ（自己署名）+ 自作クライアントで検証あり ---------------
  {
    const char *crt = "cpp/build/testcert/server.crt";
    const char *key = "cpp/build/testcert/server.key";
    httplib::SSLServer svr(crt, key);
    if (!svr.is_valid()) {
      check(false, "https server: certificate load", "cannot read server.crt/key");
    } else {
      svr.Get("/ping", [](const httplib::Request &, httplib::Response &res) {
        res.set_content(R"({"ok":true,"from":"ssl-server"})", "application/json");
      });
      // Kaggle のカーネルチャンネルと同じ「TLS 上の WebSocket」を自前で往復させる
      svr.WebSocket("/echo", [](const httplib::Request &, httplib::ws::WebSocket &ws) {
        std::string msg;
        while (ws.read(msg) == httplib::ws::Text) {
          if (!ws.send("echo:" + msg)) { break; }
        }
      });
      std::thread th([&] { svr.listen("127.0.0.1", 8443); });
      svr.wait_until_ready();

      httplib::SSLClient cli("localhost", 8443);
      cli.set_ca_cert_path(crt);                       // 自己署名を CA として信頼
      cli.enable_server_certificate_verification(true);  // 証明書＋ホスト名を検証
      cli.set_connection_timeout(10, 0);
      auto res = cli.Get("/ping");
      check(res && res->status == 200, "https server <- https client (verify on)",
            res ? res->body : ("error " + std::to_string((int)res.error())));

      // 検証を有効にしたまま CA を教えない場合は必ず落ちること（検証が効いている証拠）
      httplib::SSLClient bad("localhost", 8443);
      bad.enable_server_certificate_verification(true);
      bad.set_connection_timeout(10, 0);
      auto bres = bad.Get("/ping");
      check(!bres, "https client rejects untrusted cert",
            bres ? "接続できてしまった（検証が効いていない）"
                 : "error " + std::to_string((int)bres.error()));

      // wss:// で WebSocket 往復（カーネルチャンネルと同じ経路）
      {
        httplib::ws::WebSocketClient wsc("wss://localhost:8443/echo");
        wsc.set_ca_cert_path(crt);
        wsc.enable_server_certificate_verification(true);
        wsc.set_connection_timeout(10, 0);
        wsc.set_read_timeout(10, 0);
        std::string got;
        bool ok = wsc.connect() && wsc.send("hello") &&
                  wsc.read(got) == httplib::ws::Text && got == "echo:hello";
        check(ok, "wss websocket echo (verify on)", got.empty() ? "no message" : got);
        wsc.close();
      }

      svr.stop();
      th.join();
    }
  }

  // --- 3. 平文 HTTP サーバ（kbridge のローカル API はこちら） ----------------
  {
    httplib::Server svr;
    svr.Get("/healthz", [](const httplib::Request &, httplib::Response &res) {
      res.set_content(R"({"ok":true,"impl":"cpp"})", "application/json");
    });
    std::thread th([&] { svr.listen("127.0.0.1", 8788); });
    svr.wait_until_ready();

    httplib::Client cli("127.0.0.1", 8788);
    cli.set_connection_timeout(10, 0);
    auto res = cli.Get("/healthz");
    check(res && res->status == 200, "http server <- http client (loopback)",
          res ? res->body : "no response");

    svr.stop();
    th.join();
  }

  // --- 4. JSON --------------------------------------------------------------
  {
    auto j = nlohmann::json::parse(R"({"ok":true,"n":[1,2,3],"s":"日本語"})");
    check(j["n"][2] == 3 && j["s"] == "日本語", "json roundtrip", j.dump());
  }

  std::printf("\n%s (%d failure%s)\n", failures ? "SMOKE FAILED" : "SMOKE OK",
              failures, failures == 1 ? "" : "s");
  return failures ? 1 : 0;
}
