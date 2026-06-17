"""Nansen REST API ラッパ（apiKey ヘッダ）。MCP より広い機能を使う。"""
import time
import json
import requests

import config

_session = requests.Session()
_session.headers.update({
    "apiKey": config.NANSEN_API_KEY,
    "Content-Type": "application/json",
})


def _post(path, payload):
    """POST {base}{path}。429/5xx はバックオフ。エラー時は {'_error':..} を返す。"""
    url = config.NANSEN_BASE + path
    for attempt in range(config.MAX_RETRIES):
        try:
            r = _session.post(url, data=json.dumps(payload), timeout=40)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep((2 ** attempt) * 0.6)
                continue
            time.sleep(config.NANSEN_SLEEP)
            if r.status_code >= 400:
                return {"_error": r.status_code, "_body": r.text[:300]}
            return r.json()
        except requests.RequestException:
            if attempt == config.MAX_RETRIES - 1:
                return {"_error": "request_exception"}
            time.sleep((2 ** attempt) * 0.6)
    return {"_error": "max_retries"}


def perp_leaderboard(date_from, date_to, page=1, per_page=100):
    """Hyperliquid perp リーダーボード（ラベル付き）。MCPでは403だがRESTは通る。"""
    return _post("/perp-leaderboard", {
        "date": {"from": date_from, "to": date_to},
        "pagination": {"page": page, "per_page": per_page},
    })


def address_labels(address, chain):
    return _post("/profiler/address/labels", {"address": address, "chain": chain})


def related_wallets(address, chain):
    """関連ウォレット（First Funder = 資金源 等）。"""
    return _post("/profiler/address/related-wallets",
                 {"address": address, "chain": chain})


def counterparties(address, chain, date_from, date_to):
    return _post("/profiler/address/counterparties", {
        "address": address, "chain": chain,
        "date": {"from": date_from, "to": date_to},
    })
