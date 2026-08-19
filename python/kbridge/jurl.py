"""Kaggle "VSCode Compatible URL" / 一般の Jupyter URL を (base_url, token) に分解する。

C++ 版 cpp/pure/jurl.hpp と**同一の規則**で実装すること（tests/test_jurl.py が両方を叩く）。

規則（上から順に判定）:
  1. パスが /k/<id>/<token>/proxy 形式（Kaggle proxy）
       -> base = scheme://host/k/<id>/<token>/proxy   (proxy まで含める)
          token = <token>  （?token= があればそちらを優先）
  2. クエリに token=... がある
       -> base = scheme://host + path から末尾の UI セグメント(lab/tree/notebooks/nbclassic)を除去
          token = クエリの値
  3. それ以外でパスセグメントが 2 つ以上
       -> token = 末尾から 2 番目のセグメント, base = それより前
  4. どれにも当てはまらない -> ValueError

base_url は常に末尾スラッシュ無し。
"""

from urllib.parse import urlparse, parse_qs

_UI_SEGMENTS = ("lab", "tree", "notebooks", "nbclassic", "proxy")


def parse_jupyter_url(url: str):
    if not url or not url.strip():
        raise ValueError("empty url")
    url = url.strip()
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError("url must be http(s): %s" % url)
    if not p.netloc:
        raise ValueError("url has no host: %s" % url)

    origin = "%s://%s" % (p.scheme, p.netloc)
    segs = [s for s in p.path.split("/") if s]
    qtoken = parse_qs(p.query).get("token", [None])[0]

    # 1. Kaggle proxy: /k/<id>/<token>/proxy
    if len(segs) >= 4 and segs[0] == "k" and segs[-1] == "proxy":
        base = origin + "/" + "/".join(segs)
        return base.rstrip("/"), (qtoken or segs[-2])

    # 2. ?token=...
    if qtoken:
        keep = list(segs)
        while keep and keep[-1] in _UI_SEGMENTS:
            keep.pop()
        base = origin + ("/" + "/".join(keep) if keep else "")
        return base.rstrip("/"), qtoken

    # 3. token をパス末尾から 2 番目に持つ形
    if len(segs) >= 2:
        token = segs[-2]
        base = origin + ("/" + "/".join(segs[:-2]) if len(segs) > 2 else "")
        return base.rstrip("/"), token

    raise ValueError("could not find token in url: %s" % url)


def mask_url(url: str) -> str:
    """ログ表示用。token を伏せる（末尾4文字だけ残す）。"""
    try:
        base, token = parse_jupyter_url(url)
    except ValueError:
        return "<unparsable url>"
    return mask_base(base, token)


def mask_base(base: str, token: str) -> str:
    if not token:
        return base
    tail = token[-4:] if len(token) > 4 else "****"
    return base.replace(token, "****" + tail)
