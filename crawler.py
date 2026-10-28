#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
《火炬之光：无限》「火」(初火源质 / Flame Elementium) 价格爬虫

纯代码管线，零 AI/LLM 调用。双平台分层兜底：
  千岛 (Qiandao)  通道1 JSON 接口直抓 (api.qiandao.com)
                  通道2 镜像接口 (api.qiandao.cn) + GET 型接口变体
                  通道3 Playwright 渲染专区页读 DOM
  DD373           通道1 列表页 HTML 直抓 (服务端渲染, 按单价升序)
                  通道2 列表页分页 URL 变体
                  通道3 Playwright 渲染列表页兜底
全局兜底：某平台全部通道失败时，保留上一次成功快照并标 stale=true。

所有价格统一为「元 / 1000 火」，只看赛季服 (SS14 塔罗斯的困兽)，不看永久服。
依赖：requests (+ 可选 playwright，仅通道3需要)。
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

CN_TZ = timezone(timedelta(hours=8))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 每条通道独立超时（秒），避免单条卡死拖垮整轮
TIMEOUT_API = 15
TIMEOUT_PAGE = 20
TIMEOUT_PW = 50

REQUEST_GAP = 0.4  # 请求间隔，保持克制


def log(msg):
    print(f"[{datetime.now(CN_TZ).strftime('%H:%M:%S')}] {msg}", flush=True)


def get(url, headers=None, timeout=TIMEOUT_PAGE, retries=2):
    """带重试的 GET（代理偶发断连时重试）。"""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=headers or HEADERS,
                             timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001 - 记录并重试
            last_exc = e
            if attempt < retries:
                time.sleep(1.2 * (attempt + 1))
    raise last_exc


def post_json(url, body, headers=None, timeout=TIMEOUT_API):
    h = dict(HEADERS)
    h["Content-Type"] = "application/json"
    h["Referer"] = "https://qiandao.com/currency/currency-zone?islandId=300444"
    h["Origin"] = "https://qiandao.com"
    if headers:
        h.update(headers)
    r = requests.post(url, headers=h, json=body, timeout=timeout)
    if r.status_code == 405 or "手速" in r.text or "request_id" in r.text:
        raise ValueError(f"被千岛 WAF 拦截 (HTTP {r.status_code})，"
                         "非浏览器指纹的直连请求被拒绝")
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# DD373
# ---------------------------------------------------------------------------
# 火炬之光(国服) ShortId=3ndhp0 / 初火源质 Identify=370jtr
# 国服 Identify=7eaent / SS14塔罗斯的困兽 Identify=2kax19
DD_LIST_URL = "https://www.dd373.com/s-3ndhp0-c-370jtr-7eaent-2kax19.html"
DD_PAGE_URL = ("https://www.dd373.com/s-3ndhp0-7eaent-2kax19-0-0-0-370jtr"
               "-0-0-0-0-0-{page}-0-0-0.html")
# 走势图接口 (JSONP) 的固定 ID：路线叶子 lastId + 商品类型 id（按名称在接口目录中定位）
DD_TREND_LEAF_ID = "dda30780f39d42debc1e8e9a18bd63e6"
DD_TREND_GOODS_TYPE_ID = "ee95d977c82342798a05e4848c6c3534"
DD_TREND_URL = ("https://order.dd373.com/Api/Order/UserCenter/"
                "GetCommodityRatioList?lastId={leaf}&goodsTypeId={gt}&callback=cb")

RATIO_RE = re.compile(r"1\s*元\s*=\s*([\d.]+)\s*个")


def parse_dd_listings(html):
    """从列表页 HTML 解析每个挂单的「元/1000火」，返回升序列表。"""
    prices = []
    blocks = html.split('<div class="goods-list-item">')
    for block in blocks[1:]:
        m = RATIO_RE.search(block)
        if m:
            ratio = float(m.group(1))  # 1元 = N 个
            if ratio > 0:
                prices.append(round(1000.0 / ratio, 4))
    return sorted(prices)


def parse_dd_buyback(html):
    """极速收货：商家轮播条嵌在列表页 HTML 中，服务器名占位 id 为 receiveQufuN，
    其附近有「1元=N个」收购比例。取所有商家比例的中位数换算为元/1000火。"""
    ratios = []
    for m in re.finditer(r"receiveQufu\d+", html):
        window = html[max(0, m.start() - 1600): m.start() + 1600]
        rm = RATIO_RE.search(window)
        if rm:
            v = float(rm.group(1))
            if 50 <= v <= 400:  # 合理区间过滤异常值
                ratios.append(v)
    if not ratios:
        # 兜底：在「极速收货」字样附近找
        idx = html.find("极速收货")
        if idx >= 0:
            window = html[idx: idx + 6000]
            for rm in RATIO_RE.finditer(window):
                v = float(rm.group(1))
                if 50 <= v <= 400:
                    ratios.append(v)
    if not ratios:
        return None
    ratios.sort()
    median = ratios[len(ratios) // 2] if len(ratios) % 2 == 1 else \
        (ratios[len(ratios) // 2 - 1] + ratios[len(ratios) // 2]) / 2.0
    return round(1000.0 / median, 2)


def dd_snapshot_from_prices(prices, buyback):
    if not prices:
        raise ValueError("列表页未解析到任何挂单")
    first100 = prices[:100]
    buckets = {}
    for p in first100:
        key = round(p, 1)
        buckets[key] = buckets.get(key, 0) + 1
    mainstream = max(buckets.items(), key=lambda kv: kv[1])[0]
    return {
        "min_listing": round(prices[0], 2),
        "mainstream": round(float(mainstream), 2),
        "buyback": buyback,
    }


DD_API_HEADERS = {**HEADERS, "Referer": "https://www.dd373.com/"}


def dd_fetch_trend():
    """7 日走势：返回 [{date, price_cny_per_1000, source}]（接口给近 7 天）。
    注意：该接口必须带 Referer，否则返回 200 空响应体。"""
    url = DD_TREND_URL.format(leaf=DD_TREND_LEAF_ID, gt=DD_TREND_GOODS_TYPE_ID)
    r = get(url, headers=DD_API_HEADERS, timeout=TIMEOUT_API)
    text = r.text.strip()
    if not text:
        raise ValueError("走势接口返回空响应")
    m = re.search(r"cb\((.*)\)\s*;?\s*$", text, re.S)
    payload = json.loads(m.group(1)) if m else json.loads(text)
    rows = payload.get("StatusData", {}).get("ResultData") or []
    daily = []
    for row in rows:
        count = row.get("Count")
        date = str(row.get("Date", "")).replace("/", "-")
        if count and float(count) > 0 and date:
            daily.append({
                "date": date,
                "price_cny_per_1000": round(1000.0 / float(count), 2),
                "source": "dd373",
            })
    daily.sort(key=lambda d: d["date"])
    return daily


def dd_channel1():
    """通道1：列表页直抓（首页 + 分页变体 URL 本就是升序），共取 5 页约 100 条。"""
    prices = []
    buyback = None
    html = get(DD_LIST_URL, timeout=TIMEOUT_PAGE).text
    prices += parse_dd_listings(html)
    buyback = parse_dd_buyback(html)
    for page in range(2, 6):
        time.sleep(REQUEST_GAP)
        html = get(DD_PAGE_URL.format(page=page), timeout=TIMEOUT_PAGE).text
        prices += parse_dd_listings(html)
        if len(prices) >= 100:
            break
    prices = sorted(set(prices)) if False else sorted(prices)
    snap = dd_snapshot_from_prices(prices, buyback)
    daily = []
    try:
        daily = dd_fetch_trend()
    except Exception as e:  # 走势失败不影响快照
        log(f"DD373 走势接口失败（不影响快照）: {e}")
    return snap, daily


def dd_channel2():
    """通道2：仅用分页 URL 形式（与通道1不同的 URL 形态）重抓一遍。"""
    prices = []
    buyback = None
    for page in range(1, 6):
        html = get(DD_PAGE_URL.format(page=page), timeout=TIMEOUT_PAGE).text
        if page == 1:
            buyback = parse_dd_buyback(html)
        prices += parse_dd_listings(html)
        time.sleep(REQUEST_GAP)
        if len(prices) >= 100:
            break
    snap = dd_snapshot_from_prices(sorted(prices), buyback)
    daily = []
    try:
        daily = dd_fetch_trend()
    except Exception:
        pass
    return snap, daily


def dd_channel3():
    """通道3：Playwright 渲染列表页后用同一解析器提取。"""
    html = render_page(DD_LIST_URL, wait_ms=5000)
    prices = parse_dd_listings(html)
    buyback = parse_dd_buyback(html)
    snap = dd_snapshot_from_prices(sorted(prices), buyback)
    daily = []
    try:
        daily = dd_fetch_trend()
    except Exception:
        pass
    return snap, daily


# ---------------------------------------------------------------------------
# 千岛 Qiandao
# ---------------------------------------------------------------------------
# 接口逆向自千岛 Web 端 JS（c2c-web 服务）：
#   POST /c2c-web/v1/common/currency-spu-price-list  {tagId, offset, limit}
#        -> 该岛通货 SPU 列表（火炬之光岛 relatedTagId = 1824609），
#           条目含 strikePrice / minOnlinePrice 等价格字段
#   POST /c2c-web/v1/currency/spu-list-v2            {spuId, ...} 在售挂单
#   GET  /c2c-web/v1/currency/spu-static?spuId=      统计（标注价/成交均价）
#   GET  /c2c-web/v1/c2c/get-spu-specs?spuId=        区服规格（选「赛季/普通」）
#   POST /c2c-web/v1/common/get-spu-trading-summary-by-time {spuId, ...} 成交汇总
QD_ISLAND_ID = "300444"
QD_TAG_ID = "1824609"  # 火炬之光岛 relatedTagId（专区页 SSR 数据）
QD_ZONE_URL = "https://qiandao.com/currency/currency-zone?islandId=300444"
QD_HOSTS = ["https://api.qiandao.com", "https://api.qiandao.cn"]


def qd_normalize_price(v):
    """千岛价格字段单位不一（元/个、元/1000个、个/元），统一换算为元/1000火。
    <1 视为元/个；1~50 视为元/1000个；>50 视为「个/元」取倒数。"""
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v < 1:
        return round(v * 1000, 2)
    if v <= 50:
        return round(v, 2)
    return round(1000.0 / v, 2)


def qd_pick_price(item, keys):
    for k in keys:
        if k in item and item[k] not in (None, "", 0):
            return qd_normalize_price(item[k])
    return None


def qd_find_spu(items):
    for it in items:
        name = str(it.get("name", ""))
        if "初火源质" in name or "初火" in name:
            return it
    return None


def qd_api_flow(host):
    """在指定 API 主机上跑一遍接口流程，返回快照 dict。"""
    base = host.rstrip("/")
    # 1) SPU 列表：拿到初火源质的 spuId 与标注价
    resp = post_json(base + "/c2c-web/v1/common/currency-spu-price-list",
                     {"offset": 0, "limit": 20, "tagId": QD_TAG_ID})
    data = resp.get("data") or {}
    items = data.get("items") or data.get("list") or []
    spu = qd_find_spu(items)
    if not spu:
        raise ValueError(f"SPU 列表未找到初火源质（共 {len(items)} 条）")
    spu_id = spu.get("id") or spu.get("spuId")
    listed = qd_pick_price(spu, ["strikePrice", "minOnlinePrice", "minPrice", "price"])

    # 2) 区服规格：定位「赛季」规格 id（排除永久/专家）
    spec_ids = None
    try:
        r = get(base + "/c2c-web/v1/c2c/get-spu-specs",
                headers={**HEADERS, "Referer": QD_ZONE_URL},
                timeout=TIMEOUT_API)
        spec_data = r.json()
        found = []

        def walk(node):
            if isinstance(node, dict):
                nm = str(node.get("name", "") or node.get("label", ""))
                if "赛季" in nm and "永久" not in nm and "专家" not in nm:
                    sid = node.get("id") or node.get("value")
                    if sid:
                        found.append(str(sid))
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(spec_data)
        if found:
            spec_ids = found
    except Exception as e:
        log(f"千岛规格接口失败（继续无规格抓取）: {e}")

    # 3) 在售挂单：取最低价
    min_listing = None
    body = {"spuId": spu_id, "offset": 0, "limit": 20}
    if spec_ids:
        body["specIds"] = spec_ids
    try:
        resp2 = post_json(base + "/c2c-web/v1/currency/spu-list-v2", body)
        d2 = resp2.get("data") or {}
        listings = d2.get("items") or d2.get("list") or d2.get("records") or []
        prices = []
        for it in listings:
            p = qd_pick_price(it, ["ratioPrice", "price", "rmbPerAmount",
                                   "minOnlinePrice", "strikePrice"])
            if p:
                prices.append(p)
        if prices:
            min_listing = min(prices)
    except Exception as e:
        log(f"千岛挂单接口失败: {e}")
    if min_listing is None:
        min_listing = qd_pick_price(spu, ["minOnlinePrice", "stock_order_min_sell_price",
                                          "min_price", "minPrice"])

    # 4) 统计：标注价 / 近1天成交均价
    avg_1d = None
    try:
        rr = requests.get(base + "/c2c-web/v1/currency/spu-static",
                          headers={**HEADERS, "Referer": QD_ZONE_URL},
                          params={"spuId": spu_id}, timeout=TIMEOUT_API)
        if rr.ok:
            sd = rr.json().get("data") or {}

            def deep_find(node, pats):
                if isinstance(node, dict):
                    for k, v in node.items():
                        if any(p in k.lower() for p in pats) and \
                                isinstance(v, (int, float)):
                            return v
                    for v in node.values():
                        got = deep_find(v, pats)
                        if got is not None:
                            return got
                elif isinstance(node, list):
                    for v in node:
                        got = deep_find(v, pats)
                        if got is not None:
                            return got
                return None
            avg_1d = qd_normalize_price(
                deep_find(sd, ["avgtradingprice", "avgprice", "averageprice",
                               "avg_1d", "avg1d"]))
            if listed is None:
                listed = qd_normalize_price(
                    deep_find(sd, ["strikeprice", "minonlineprice", "minprice"]))
    except Exception as e:
        log(f"千岛统计接口失败: {e}")

    if min_listing is None and listed is None and avg_1d is None:
        raise ValueError("千岛接口未返回任何可用价格字段")
    return {"min_listing": min_listing, "listed_price": listed, "avg_1d": avg_1d}


def qd_channel1():
    return qd_api_flow(QD_HOSTS[0])


def qd_channel2():
    return qd_api_flow(QD_HOSTS[1])


def qd_channel3():
    """通道3：Playwright 渲染专区页，选「赛季」区服后读页面文本中的价格。"""
    from playwright.sync_api import sync_playwright  # 延迟导入，缺失时通道失败

    exe = os.environ.get("FE_CRAWLER_CHROME_PATH")
    proxy_url = os.environ.get("FE_CRAWLER_PW_PROXY")
    launch_kwargs = {"headless": True, "args": ["--no-sandbox",
                                                "--disable-blink-features=AutomationControlled",
                                                "--lang=zh-CN"]}
    if exe:
        launch_kwargs["executable_path"] = exe
    if proxy_url:
        launch_kwargs["proxy"] = {"server": proxy_url}
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        ctx = browser.new_context(
            user_agent=UA, locale="zh-CN", timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=bool(proxy_url))
        page = ctx.new_page()
        page.goto(QD_ZONE_URL, timeout=TIMEOUT_PW * 1000, wait_until="commit")
        page.wait_for_timeout(8000)
        # 选区服：点开级联选择器，选含「赛季」的选项
        try:
            page.locator(".n-cascader").first.click(timeout=6000)
            page.wait_for_timeout(2500)
            page.get_by_text(re.compile(r"赛季")).first.click(timeout=6000)
            page.wait_for_timeout(6000)
        except Exception as e:
            log(f"千岛渲染通道选区服失败（继续读当前页）: {e}")
        text = page.inner_text("body")
        ctx.close()
        browser.close()
    # 从文本提取：标注价（通货名附近的价格）、近1天成交均价、最低在售
    listed = avg = min_l = None
    m = re.search(r"近\s*1\s*天成交均价[^¥\d]*([\d.]+)", text)
    if m:
        avg = qd_normalize_price(float(m.group(1)))
    m = re.search(r"标注价[^¥\d]*([\d.]+)", text)
    if m:
        listed = qd_normalize_price(float(m.group(1)))
    nums = [qd_normalize_price(float(x)) for x in
            re.findall(r"(?<![\d.])0\.00\d{2}(?![\d])", text)]
    nums = [n for n in nums if n]
    if nums:
        min_l = min(nums)
    if listed is None and min_l is not None:
        listed = min_l
    if min_l is None and listed is None and avg is None:
        raise ValueError("渲染页未提取到价格（可能被 WAF 拦截或需登录选服）")
    return {"min_listing": min_l, "listed_price": listed, "avg_1d": avg}


# ---------------------------------------------------------------------------
# Playwright 通用渲染（DD373 通道3 使用）
# ---------------------------------------------------------------------------
def render_page(url, wait_ms=5000):
    from playwright.sync_api import sync_playwright

    exe = os.environ.get("FE_CRAWLER_CHROME_PATH")
    proxy_url = os.environ.get("FE_CRAWLER_PW_PROXY")
    launch_kwargs = {"headless": True, "args": ["--no-sandbox"]}
    if exe:
        launch_kwargs["executable_path"] = exe
    if proxy_url:
        launch_kwargs["proxy"] = {"server": proxy_url}
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        ctx = browser.new_context(user_agent=UA, locale="zh-CN",
                                  ignore_https_errors=bool(proxy_url))
        page = ctx.new_page()
        page.goto(url, timeout=TIMEOUT_PW * 1000, wait_until="domcontentloaded")
        page.wait_for_timeout(wait_ms)
        html = page.content()
        ctx.close()
        browser.close()
    return html


# ---------------------------------------------------------------------------
# 主流程：分层兜底 + stale 保留
# ---------------------------------------------------------------------------
def run_platform(name, channels, prev):
    """按顺序尝试各通道；全失败则回退上一次成功快照并标 stale。"""
    errors = []
    for label, fn in channels:
        t0 = time.time()
        try:
            result = fn()
            log(f"{name} {label} 成功（{time.time() - t0:.1f}s）")
            return result, errors, False
        except Exception as e:  # noqa: BLE001
            msg = f"{label}: {type(e).__name__}: {str(e)[:180]}"
            log(f"{name} {label} 失败（{time.time() - t0:.1f}s） {msg}")
            errors.append(msg)
            time.sleep(0.5)
    # 全部失败：回退旧数据
    if prev:
        stale_snap = dict(prev)
        stale_snap["stale"] = True
        return stale_snap, errors, True
    return None, errors, True


def main():
    started = time.time()
    prev_data = {}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                prev_data = json.load(f)
        except Exception:
            prev_data = {}

    now_iso = datetime.now(CN_TZ).isoformat(timespec="seconds")

    # --- DD373 ---
    dd_prev = prev_data.get("dd373")
    if dd_prev:
        dd_prev = {k: dd_prev.get(k) for k in ("min_listing", "mainstream", "buyback")}
        if all(v is None for v in dd_prev.values()):
            dd_prev = None
    dd_result, dd_errors, dd_stale = run_platform(
        "DD373",
        [("通道1 列表页直抓", dd_channel1),
         ("通道2 分页URL变体", dd_channel2),
         ("通道3 Playwright渲染", dd_channel3)],
        dd_prev)
    dd_snap, daily = {}, []
    if isinstance(dd_result, tuple):
        dd_snap, daily = dd_result
    elif isinstance(dd_result, dict):
        dd_snap = dd_result
        daily = prev_data.get("daily", []) if dd_stale else []
    dd_out = {
        "min_listing": dd_snap.get("min_listing"),
        "mainstream": dd_snap.get("mainstream"),
        "buyback": dd_snap.get("buyback"),
        "stale": dd_stale,
        "last_success_at": prev_data.get("dd373", {}).get("last_success_at")
        if dd_stale else now_iso,
    }

    # --- 千岛 ---
    qd_prev = prev_data.get("qiandao")
    if qd_prev:
        qd_prev = {k: qd_prev.get(k) for k in ("min_listing", "listed_price", "avg_1d")}
        if all(v is None for v in qd_prev.values()):
            qd_prev = None
    qd_snap, qd_errors, qd_stale = run_platform(
        "千岛",
        [("通道1 JSON接口(api.qiandao.com)", qd_channel1),
         ("通道2 镜像接口(api.qiandao.cn)", qd_channel2),
         ("通道3 Playwright渲染", qd_channel3)],
        qd_prev)
    if isinstance(qd_snap, tuple):  # 千岛通道不返回 tuple，此分支仅防御
        qd_snap = qd_snap[0]
    qd_snap = qd_snap or {}
    qd_out = {
        "min_listing": qd_snap.get("min_listing"),
        "listed_price": qd_snap.get("listed_price"),
        "avg_1d": qd_snap.get("avg_1d"),
        "stale": qd_stale,
        "last_success_at": prev_data.get("qiandao", {}).get("last_success_at")
        if qd_stale else now_iso,
    }

    out = {
        "updated_at": now_iso,
        "unit": "CNY per 1000 FE",
        "server_scope": "赛季服 (SS14 塔罗斯的困兽)",
        "qiandao": qd_out,
        "dd373": dd_out,
        "daily": daily,
        "errors": {"qiandao": qd_errors, "dd373": dd_errors},
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    log(f"完成，总耗时 {time.time() - started:.1f}s -> {DATA_FILE}")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
