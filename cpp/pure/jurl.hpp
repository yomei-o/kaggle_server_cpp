// Kaggle "VSCode Compatible URL" / 一般の Jupyter URL を (base_url, token) に分解する。
//
// python/kbridge/jurl.py と**同一の規則**で実装すること（tests/parity.py が両方を叩く）。
// 規則（上から順に判定）:
//   1. パスが /k/<id>/<token>/proxy 形式（Kaggle proxy）
//        -> base = scheme://host/k/<id>/<token>/proxy   (proxy まで含める)
//           token = <token>  （?token= があればそちらを優先）
//   2. クエリに token=... がある
//        -> base = scheme://host + path から末尾の UI セグメント(lab/tree/notebooks/
//           nbclassic/proxy)を除去、token = クエリの値
//   3. それ以外でパスセグメントが 2 つ以上
//        -> token = 末尾から 2 番目のセグメント, base = それより前
//   4. どれにも当てはまらない -> 例外
//
// base_url は常に末尾スラッシュ無し。
#pragma once

#include <stdexcept>
#include <string>
#include <vector>

namespace kbridge {

struct Url {
  std::string scheme;   // "https"
  std::string host;     // "kkb-production.jupyter-proxy.kaggle.net"（ポート込み）
  std::string path;     // "/k/xxx/yyy/proxy"
  std::string query;    // "token=..."（? は含まない）

  std::string origin() const { return scheme + "://" + host; }
};

inline Url split_url(const std::string &url) {
  Url u;
  auto s = url;
  // 前後の空白を落とす
  auto b = s.find_first_not_of(" \t\r\n");
  auto e = s.find_last_not_of(" \t\r\n");
  if (b == std::string::npos) { throw std::invalid_argument("empty url"); }
  s = s.substr(b, e - b + 1);

  auto scheme_end = s.find("://");
  if (scheme_end == std::string::npos) {
    throw std::invalid_argument("url must be http(s): " + s);
  }
  u.scheme = s.substr(0, scheme_end);
  if (u.scheme != "http" && u.scheme != "https") {
    throw std::invalid_argument("url must be http(s): " + s);
  }
  auto rest = s.substr(scheme_end + 3);

  auto q = rest.find('?');
  if (q != std::string::npos) {
    u.query = rest.substr(q + 1);
    rest = rest.substr(0, q);
  }
  auto frag = u.query.find('#');
  if (frag != std::string::npos) { u.query = u.query.substr(0, frag); }

  auto slash = rest.find('/');
  if (slash == std::string::npos) {
    u.host = rest;
    u.path = "";
  } else {
    u.host = rest.substr(0, slash);
    u.path = rest.substr(slash);
  }
  if (u.host.empty()) { throw std::invalid_argument("url has no host: " + s); }
  return u;
}

inline std::vector<std::string> path_segments(const std::string &path) {
  std::vector<std::string> out;
  std::string cur;
  for (char c : path) {
    if (c == '/') {
      if (!cur.empty()) { out.push_back(cur); }
      cur.clear();
    } else {
      cur.push_back(c);
    }
  }
  if (!cur.empty()) { out.push_back(cur); }
  return out;
}

inline std::string query_value(const std::string &query, const std::string &key) {
  size_t pos = 0;
  while (pos <= query.size()) {
    auto amp = query.find('&', pos);
    auto part = query.substr(pos, amp == std::string::npos ? std::string::npos
                                                           : amp - pos);
    auto eq = part.find('=');
    if (eq != std::string::npos && part.substr(0, eq) == key) {
      return part.substr(eq + 1);
    }
    if (amp == std::string::npos) { break; }
    pos = amp + 1;
  }
  return "";
}

inline bool is_ui_segment(const std::string &s) {
  return s == "lab" || s == "tree" || s == "notebooks" || s == "nbclassic" ||
         s == "proxy";
}

inline std::string join_segments(const std::string &origin,
                                 const std::vector<std::string> &segs,
                                 size_t count) {
  std::string out = origin;
  for (size_t i = 0; i < count && i < segs.size(); ++i) {
    out += "/";
    out += segs[i];
  }
  while (!out.empty() && out.back() == '/') { out.pop_back(); }
  return out;
}

// base_url と token を返す。
inline void parse_jupyter_url(const std::string &url, std::string *base_url,
                              std::string *token) {
  Url u = split_url(url);
  auto segs = path_segments(u.path);
  auto qtoken = query_value(u.query, "token");

  // 1. Kaggle proxy: /k/<id>/<token>/proxy
  if (segs.size() >= 4 && segs.front() == "k" && segs.back() == "proxy") {
    *base_url = join_segments(u.origin(), segs, segs.size());
    *token = qtoken.empty() ? segs[segs.size() - 2] : qtoken;
    return;
  }

  // 2. ?token=...
  if (!qtoken.empty()) {
    size_t keep = segs.size();
    while (keep > 0 && is_ui_segment(segs[keep - 1])) { --keep; }
    *base_url = join_segments(u.origin(), segs, keep);
    *token = qtoken;
    return;
  }

  // 3. token をパス末尾から 2 番目に持つ形
  if (segs.size() >= 2) {
    *token = segs[segs.size() - 2];
    *base_url = join_segments(u.origin(), segs, segs.size() - 2);
    return;
  }

  throw std::invalid_argument("could not find token in url: " + url);
}

// ログ・応答表示用。token を伏せる（末尾 4 文字だけ残す）。
inline std::string mask_base(const std::string &base, const std::string &token) {
  if (token.empty()) { return base; }
  std::string tail = token.size() > 4 ? token.substr(token.size() - 4) : "****";
  std::string masked = "****" + tail;
  std::string out = base;
  for (size_t pos = out.find(token); pos != std::string::npos;
       pos = out.find(token, pos + masked.size())) {
    out.replace(pos, token.size(), masked);
  }
  return out;
}

}  // namespace kbridge
