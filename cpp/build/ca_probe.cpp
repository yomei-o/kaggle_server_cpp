// Windows のシステムルートストアから mbedTLS が何枚読めていて、実際にどのホストの検証が
// 通るのかを調べるための調査用プログラム（恒久的なテストではない）。
#include <cstdio>
#include <string>
#include <windows.h>
#include <wincrypt.h>

#include <mbedtlspp.hpp>

int main() {
  mbedtls_x509_crt chain;
  mbedtls_x509_crt_init(&chain);

  int total = 0, ok = 0;
  static const wchar_t *stores[] = {L"ROOT", L"CA"};
  for (auto name : stores) {
    HCERTSTORE h = CertOpenSystemStoreW(0, name);
    if (!h) { std::printf("store %ls: cannot open\n", name); continue; }
    int s_total = 0, s_ok = 0;
    PCCERT_CONTEXT c = nullptr;
    while ((c = CertEnumCertificatesInStore(h, c)) != nullptr) {
      s_total++;
      if (mbedtls_x509_crt_parse_der(&chain, c->pbCertEncoded, c->cbCertEncoded) == 0)
        s_ok++;
    }
    CertCloseStore(h, 0);
    std::printf("store %ls: %d certs, %d parsed by mbedtls\n", name, s_total, s_ok);
    total += s_total;
    ok += s_ok;
  }
  std::printf("total %d certs, %d parsed\n", total, ok);

  const char *hosts[] = {"example.com", "www.kaggle.com",
                         "kkb-production.jupyter-proxy.kaggle.net",
                         "raw.githubusercontent.com"};
  for (auto host : hosts) {
    mbedtls_net_context net;
    mbedtls_ssl_context ssl;
    mbedtls_ssl_config conf;
    mbedtls_entropy_context entropy;
    mbedtls_ctr_drbg_context drbg;
    mbedtls_net_init(&net);
    mbedtls_ssl_init(&ssl);
    mbedtls_ssl_config_init(&conf);
    mbedtls_entropy_init(&entropy);
    mbedtls_ctr_drbg_init(&drbg);
    const char *pers = "ca_probe";
    mbedtls_ctr_drbg_seed(&drbg, mbedtls_entropy_func, &entropy,
                          (const unsigned char *)pers, strlen(pers));
    mbedtls_ssl_config_defaults(&conf, MBEDTLS_SSL_IS_CLIENT,
                                MBEDTLS_SSL_TRANSPORT_STREAM,
                                MBEDTLS_SSL_PRESET_DEFAULT);
    mbedtls_ssl_conf_authmode(&conf, MBEDTLS_SSL_VERIFY_REQUIRED);
    mbedtls_ssl_conf_ca_chain(&conf, &chain, nullptr);
    mbedtls_ssl_conf_rng(&conf, mbedtls_ctr_drbg_random, &drbg);
    mbedtls_ssl_setup(&ssl, &conf);
    mbedtls_ssl_set_hostname(&ssl, host);

    int ret = mbedtls_net_connect(&net, host, "443", MBEDTLS_NET_PROTO_TCP);
    if (ret != 0) {
      std::printf("%-45s connect failed %d\n", host, ret);
    } else {
      mbedtls_ssl_set_bio(&ssl, &net, mbedtls_net_send, mbedtls_net_recv, nullptr);
      ret = mbedtls_ssl_handshake(&ssl);
      uint32_t flags = mbedtls_ssl_get_verify_result(&ssl);
      char buf[512] = {0};
      if (flags) mbedtls_x509_crt_verify_info(buf, sizeof(buf), "    ", flags);
      std::printf("%-45s handshake %d, verify flags 0x%08x\n%s", host, ret,
                  (unsigned)flags, flags ? buf : "");
    }
    mbedtls_ssl_free(&ssl);
    mbedtls_ssl_config_free(&conf);
    mbedtls_ctr_drbg_free(&drbg);
    mbedtls_entropy_free(&entropy);
    mbedtls_net_free(&net);
  }

  mbedtls_x509_crt_free(&chain);
  return 0;
}
