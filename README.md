# 火价爬虫 ·《火炬之光：无限》初火源质（FE）价格

纯 Python 代码管线，**零 AI/LLM 调用**——脚本与 GitHub Actions 工作流里没有任何
需要模型参与的步骤，不存在「爬一次烧一次词元」。定时抓取千岛 + DD373 两家平台的
「火」（初火源质）价格，统一换算为 **元 / 1000 火**，只看 **赛季服**
（SS14 塔罗斯的困兽），不看永久服。输出 `data.json`。

## 为什么选 Python

- 生态：requests + Playwright 是爬虫事实标准，两个平台的直抓/渲染双模式可在同一仓库、同一语言内完成；
- GitHub Actions 一等支持（setup-python + pip 缓存），安装快、无编译步骤；
- 只用 requests + 标准库 json/datetime（Playwright 仅渲染兜底才需要），依赖面小、易维护；
- 单文件 `crawler.py` 即可跑全流程，后续改口径/加平台成本低。

## 单次运行预计时长

- 典型（通道 1 命中）：**约 20–30 秒**（本地实测 20–28 秒）；
- GitHub Actions（含装依赖 + Chromium 走缓存）：**1–2 分钟**内完成抓取 + 提交；
- 最坏情况（多条通道超时重试）：约 3–4 分钟，工作流上限设为 10 分钟。

## 数据字段与口径（data.json）

```jsonc
{
  "updated_at": "2026-10-28T19:27:50+08:00",   // 本轮运行时间（北京时间）
  "unit": "CNY per 1000 FE",
  "server_scope": "赛季服 (SS14 塔罗斯的困兽)",
  "qiandao": {
    "min_listing":  null,        // 千岛最低在售挂单价（元/1000火）
    "listed_price": null,        // 千岛标注价（SPU 牌价 strikePrice/minOnlinePrice）
    "avg_1d":       null,        // 千岛近 1 天成交均价
    "stale": true,               // 本轮全通道失败、沿用上次成功数据时为 true
    "last_success_at": null      // 该平台最后一次抓取成功的时间
  },
  "dd373": {
    "min_listing": 5.86,         // DD373 最低挂单（列表按单价升序，取最小）
    "mainstream":  6.2,          // 主流挂单：前 100 条挂单按 0.1 元分档的众数档
    "buyback":     5.9,          // 极速收货商家收购价：商家比例中位数换算
    "stale": false,
    "last_success_at": "2026-10-28T19:27:50+08:00"
  },
  "daily": [                     // 逐日价（DD373 官方走势接口，近 7 天）
    {"date": "2026-10-27", "price_cny_per_1000": 6.99, "source": "dd373"}
  ],
  "errors": {"qiandao": ["通道1 ... 失败原因"], "dd373": []}
}
```

口径细节：

- DD373 挂单是「N 个 = M 元」打包价，页面同时给出「1 元 = N 个」比例，
  按 `1000 / N` 换算为元/1000 火。列表默认按单价升序排列。
- 「主流挂单」= 前 100 条挂单的单位价按 0.1 元取整分档后，条数最多的档位。
- 「极速收货」商家条嵌在列表页 HTML 内（服务器名占位 `receiveQufuN` 附近的
  「1 元 = N 个」），取全部商家比例的中位数再换算；异常比例（<50 或 >400 个/元）剔除。
- 走势接口必须带 `Referer: https://www.dd373.com/`，否则返回 200 空响应体；
  接口给的 `Count` 是「个/元」，同样按 `1000 / Count` 换算。
- 千岛价格字段单位不一（元/个、元/1000 个、「个/元」三种都出现过），爬虫按数值
  量级启发式归一：<1 视为元/个 ×1000；1–50 视为元/1000 火；>50 视为个/元取倒数。
- 火价日内波动剧烈：以上都是抓取时刻的快照，只看量级，别把小数当精确参数。

## 分层兜底（每平台 3 条通道，顺序尝试、成功即停）

每条通道有独立超时（接口 15s / 页面 20s / 渲染 50s），失败原因逐条写入
`data.json.errors`。某平台三条全灭时：保留上一次成功快照并标 `stale=true`
（`last_success_at` 保持原值）；单边失败不影响另一平台输出。

### 千岛（qiandao.com/currency/currency-zone?islandId=300444）

| 通道 | 方式 | 可行性 |
|---|---|---|
| 1 | JSON 接口直抓 `api.qiandao.com`：`POST /c2c-web/v1/common/currency-spu-price-list`（tagId=1824609 定位火炬之光岛 SPU 列表）→ `POST /c2c-web/v1/currency/spu-list-v2`（在售挂单取最低）→ `GET /c2c-web/v1/currency/spu-static`（标注价/成交均价）；区服规格走 `GET /c2c-web/v1/c2c/get-spu-specs` 选「赛季」 | 接口已从官网 JS 完整逆向；但千岛有阿里云 WAF，非浏览器指纹的直连请求会被 405 拦截（本沙箱实测），机房 IP 上可能同样被拦 |
| 2 | 同一接口集走镜像主机 `api.qiandao.cn` | 同上；本沙箱实测该主机 POST 超时 |
| 3 | Playwright 渲染专区页：选区服级联（赛季）→ 读 DOM 文本提取价 | 页面本身可渲染（实测），但其数据 XHR 同样要过 WAF；干净网络 + 真实 Chromium 指纹下最有希望 |

千岛接口与页面均未要求登录即可看价；爬虫不登录、不绕验证码。

### DD373（dd373.com 列表页）

| 通道 | 方式 | 可行性 |
|---|---|---|
| 1 | 列表页 HTML 直抓（服务端渲染）：任务页 + 分页 URL 共 5 页约 100 条 + 走势 JSONP 接口 | ✅ 本地实测全通（快照 + 7 日走势） |
| 2 | 仅用分页 URL 形态重抓（与通道 1 不同的 URL 模式） | ✅ 同解析器，实测可用 |
| 3 | Playwright 渲染列表页后同一解析器提取 | ✅ 本地实测全通（最低 5.70 / 主流 6.0 / 收购 6.0） |

## 本地运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium   # 仅通道 3 需要
.venv/bin/python crawler.py
```

可选环境变量（受限沙箱调试用，正常环境不用设）：
`FE_CRAWLER_CHROME_PATH`（Chromium 可执行文件路径）、
`FE_CRAWLER_PW_PROXY`（Playwright 走的本地代理，如 `http://127.0.0.1:18080`）。

## 定时运行（GitHub Actions 模板）

`.github/workflows/crawl.yml`：每 15 分钟触发，装依赖（pip 缓存）→ 装 Chromium
（浏览器目录走 actions/cache，只装 chromium）→ 跑 `crawler.py` → `data.json`
有变化才提交回仓库。**当前只是模板**：本目录尚未 git init、未推送任何仓库，
等部署方案确定后再启用。

## 本地验证记录与已知阻碍（2026-10-28，沙箱环境）

- DD373：通道 1/3 均实测成功。19:26–19:38 多次快照：最低 5.70–5.91、主流 6.0–6.2、
  收购 5.89–6.0，与当日下午人工核查（最低约 5.8、主流约 6.0）一致（±15% 内）；
  7 日走势 21.62 / 16.76 / 14.19 / 12.18 / 10.10 / 8.76 / 6.99（10-21→10-27）
  与人工读数逐日吻合。
- 千岛：三条通道在本地沙箱均未跑通 —— 沙箱出口代理一度丢弃全部 POST；
  接口主机可达但被千岛 WAF 以 405「手速太快」拦截 Python 指纹；Playwright 经
  本地中继可打开页面，但页面数据接口同样过不了 WAF（区服/SPU 列表加载为空）。
  代码与失败记录、stale 回退均已实测（种子数据回退测试通过）。部署到正常网络
  后通道 1/3 值得先观察 `errors` 字段再决定是否调整。
