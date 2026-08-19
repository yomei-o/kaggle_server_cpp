// base64 と、URL のパーセントエンコード。
// httplib の detail:: に似たものはあるが、内部 API に寄りかからないよう自前で持つ。
#pragma once

#include <cstdint>
#include <string>

namespace kbridge {

inline std::string b64_encode(const std::string &in) {
  static const char *T =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  std::string out;
  out.reserve((in.size() + 2) / 3 * 4);
  size_t i = 0;
  for (; i + 2 < in.size(); i += 3) {
    uint32_t v = (uint8_t)in[i] << 16 | (uint8_t)in[i + 1] << 8 | (uint8_t)in[i + 2];
    out += T[(v >> 18) & 63];
    out += T[(v >> 12) & 63];
    out += T[(v >> 6) & 63];
    out += T[v & 63];
  }
  if (i + 1 == in.size()) {
    uint32_t v = (uint32_t)((uint8_t)in[i]) << 16;
    out += T[(v >> 18) & 63];
    out += T[(v >> 12) & 63];
    out += "==";
  } else if (i + 2 == in.size()) {
    uint32_t v = (uint32_t)((uint8_t)in[i]) << 16 | (uint32_t)((uint8_t)in[i + 1]) << 8;
    out += T[(v >> 18) & 63];
    out += T[(v >> 12) & 63];
    out += T[(v >> 6) & 63];
    out += '=';
  }
  return out;
}

inline int b64_value(char c) {
  if (c >= 'A' && c <= 'Z') { return c - 'A'; }
  if (c >= 'a' && c <= 'z') { return c - 'a' + 26; }
  if (c >= '0' && c <= '9') { return c - '0' + 52; }
  if (c == '+') { return 62; }
  if (c == '/') { return 63; }
  return -1;  // '=' と空白を含む、無視すべき文字
}

inline std::string b64_decode(const std::string &in) {
  std::string out;
  out.reserve(in.size() / 4 * 3);
  uint32_t buf = 0;
  int bits = 0;
  for (char c : in) {
    int v = b64_value(c);
    if (v < 0) { continue; }
    buf = (buf << 6) | (uint32_t)v;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out += (char)((buf >> bits) & 0xFF);
    }
  }
  return out;
}

// パス用のパーセントエンコード。'/' は残す（contents API のパスに使う）。
inline std::string encode_path(const std::string &in) {
  static const char *HEX = "0123456789ABCDEF";
  std::string out;
  for (unsigned char c : in) {
    if (isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~' || c == '/') {
      out += (char)c;
    } else {
      out += '%';
      out += HEX[c >> 4];
      out += HEX[c & 15];
    }
  }
  return out;
}

// クエリ値用。'/' も含めて全部エスケープする。
inline std::string encode_query(const std::string &in) {
  static const char *HEX = "0123456789ABCDEF";
  std::string out;
  for (unsigned char c : in) {
    if (isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') {
      out += (char)c;
    } else {
      out += '%';
      out += HEX[c >> 4];
      out += HEX[c & 15];
    }
  }
  return out;
}

inline std::string decode_query(const std::string &in) {
  std::string out;
  for (size_t i = 0; i < in.size(); ++i) {
    if (in[i] == '+') {
      out += ' ';
    } else if (in[i] == '%' && i + 2 < in.size()) {
      auto hex = [](char c) -> int {
        if (c >= '0' && c <= '9') { return c - '0'; }
        if (c >= 'a' && c <= 'f') { return c - 'a' + 10; }
        if (c >= 'A' && c <= 'F') { return c - 'A' + 10; }
        return -1;
      };
      int hi = hex(in[i + 1]), lo = hex(in[i + 2]);
      if (hi >= 0 && lo >= 0) {
        out += (char)(hi * 16 + lo);
        i += 2;
        continue;
      }
      out += in[i];
    } else {
      out += in[i];
    }
  }
  return out;
}

}  // namespace kbridge
