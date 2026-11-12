# 滤膜采购组合优化 + 替代链追溯 API

实验室耗材采购场景，提供两个 API：

1. **报价优化**（`POST /api/v1/quote`）：给定供应商**目录 URL**、**滤膜型号**和**需求片数**，
   用 `requests` + `BeautifulSoup` 抓取同站分页列表与匹配型号的详情页，读取每种包装的
   **每包片数 / 含税包价 / 可购包数**，在库存约束内求**总价最低的整包组合**（不同包装可混购；
   同价时优先超量更少的组合），并返回各包装采购包数、实到片数、总价与详情来源。
2. **替代链追溯**（`POST /api/v1/replacement-chain`）：供应商停产滤膜后，给定**停产详情页
   URL**，沿详情页"替代为"的同站链接逐级追踪（一个商品可有多个后继），按页面标注的
   **在售状态**与包装字段，返回**有库存的在售末端包装、替代路径及来源**。多条路径抵达同一
   详情只抓取一次但保留各条路径；遇到环、失效链接或在售详情缺少关键字段时，受影响分支
   标记**待核实**，其余已核实分支照常返回，且响应明确标注**结果不完整**。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

包含两个服务：

| 服务 | 地址 | 说明 |
|---|---|---|
| `api` | http://localhost:8000 | FastAPI 服务（文档见 `/docs`） |
| `sample-site` | http://localhost:8080 | 离线样本站（3 页目录 + 报价详情页 + 替代链样例详情页） |

调用示例（注意：`catalog_url` / `detail_url` 由 API 容器访问，Compose 网络内请用服务名
`http://sample-site:8080/`，而不是 `localhost:8080`）：

```bash
# 报价优化
curl -X POST http://localhost:8000/api/v1/quote \
  -H 'Content-Type: application/json' \
  -d '{"catalog_url": "http://sample-site:8080/", "model": "MF-50", "demand": 180}'

# 替代链追溯（停产商品 PES-45：分叉 + 合流 + 失效链接 + 在售缺字段）
curl -X POST http://localhost:8000/api/v1/replacement-chain \
  -H 'Content-Type: application/json' \
  -d '{"detail_url": "http://sample-site:8080/details/pes-45.html"}'
```

报价响应（最优组合：2 包 100 片经济装，总价 ¥450）：

```json
{
  "status": "ok",
  "model": "MF-50",
  "demand": 180,
  "list_pages": ["http://sample-site:8080/", "..."],
  "matched_detail_pages": ["...mf50-50.html", "...mf50-100.html", "...mf50-25.html"],
  "duplicate_detail_links_skipped": 1,
  "solution": {
    "items": [
      {
        "detail_url": "http://sample-site:8080/details/mf50-100.html",
        "title": "MCE 微孔滤膜 MF-50（100片经济装）",
        "model": "MF-50",
        "pack_size": 100,
        "pack_price": "225.00",
        "stock_packs": 10,
        "purchased_packs": 2,
        "delivered_pieces": 200,
        "subtotal_price": "450.00"
      }
    ],
    "total_pieces": 200,
    "demand": 180,
    "overage": 20,
    "total_price": "450.00",
    "currency": "CNY"
  },
  "total_available_pieces": 3750
}
```

替代链响应（PES-45：唯一可采购末端 PES-45C 经两条路径抵达，另有 2 条分支待核实）：

```json
{
  "status": "partial",
  "complete": false,
  "message": "存在 2 条待核实分支，结果不完整；已核实的分支如下，但可能还有其他可采购替代包装未确认",
  "start_url": "http://sample-site:8080/details/pes-45.html",
  "start": {"detail_url": "...pes-45.html", "title": "PES 针式滤膜 PES-45 ...", "model": "PES-45", "sale_status": "停产"},
  "terminals": [
    {
      "detail_url": "http://sample-site:8080/details/pes-45c.html",
      "title": "PES 针式滤膜 PES-45C 0.45μm 25mm（100片装）",
      "model": "PES-45C",
      "sale_status": "在售",
      "pack_size": 100,
      "pack_price": "210.00",
      "stock_packs": 8,
      "paths": [
        ["...pes-45.html", "...pes-45a.html", "...pes-45c.html"],
        ["...pes-45.html", "...pes-45b.html", "...pes-45c.html"]
      ]
    }
  ],
  "out_of_stock_terminals": [],
  "pending_branches": [
    {"reason": "missing_fields", "path": ["...pes-45.html", "...pes-45a.html", "...pes-45d.html"],
     "message": "在售详情缺少关键字段：可购包数"},
    {"reason": "dead_link", "path": ["...pes-45.html", "...pes-45b.html", "...pes-45gone.html"],
     "message": "替代链接抓取失败：...pes-45gone.html"}
  ],
  "fetched_pages": ["...pes-45.html", "...pes-45a.html", "...pes-45c.html", "...pes-45d.html", "...pes-45b.html"],
  "fetch_count": 5
}
```

## API

### `POST /api/v1/quote`

请求体：

| 字段 | 类型 | 说明 |
|---|---|---|
| `catalog_url` | string | 供应商目录列表页 URL（任意一页均可，自动沿"下一页"翻页） |
| `model` | string | 滤膜型号，如 `MF-50`（忽略大小写、空格与连字符的包含匹配） |
| `demand` | int ≥ 1 | 需求片数 |

响应 `status` 语义：

| status | 含义 | 是否给出确定最低价 |
|---|---|---|
| `ok` | 已找到库存内总价最低的整包组合 | ✅ `solution` |
| `insufficient_stock` | 可购总量 < 需求（附 `total_available_pieces`） | ❌ |
| `scrape_failed` | 匹配详情页抓取/解析失败（附 `failed_detail_pages`） | ❌ |
| `no_matching_products` | 目录中无匹配型号（若有页面未能核对会附 `unverified_failed_pages`） | ❌ |
| `catalog_unreachable` | 目录列表页抓取失败 | ❌ |
| `demand_too_large` | 需求超出求解器安全上限 | ❌ |

`solution.items[]` 给出每种包装的**采购包数、实到片数、含税包价、小计与详情来源 URL**；
`solution` 另含**总实到片数、超量（overage）、总价**。

### `POST /api/v1/replacement-chain`

请求体：

| 字段 | 类型 | 说明 |
|---|---|---|
| `detail_url` | string | 停产商品详情页 URL（沿其"替代为"同站链接逐级追踪） |

响应 `status` 语义：

| status | 含义 | `complete` |
|---|---|---|
| `ok` | 替代链追踪完成，所有分支已核实 | `true` |
| `partial` | 存在待核实分支，已核实分支仍返回，**结果不完整** | `false` |
| `start_unreachable` | 起始详情页抓取失败 | `false` |

响应要点：

- `terminals[]`：**有库存的在售末端包装**——每包片数、含税包价、可购包数、详情来源
  URL，以及抵达该末端的**全部替代路径**（`paths`，每条都是从起始页到末端的 URL 序列）。
  多条路径抵达同一详情时该详情**只抓取一次**（见 `fetch_count`），但各条路径都保留。
- `out_of_stock_terminals[]`：在售但可购包数为 0 的末端（已核实、暂时无货，单独列出）。
- `pending_branches[]`：**待核实分支**，每条含 `reason`、从起始页到问题点的完整 `path`
  与 `message`。`reason` 取值：
  - `cycle`：替代链出现环（路径中重复出现同一详情页）；
  - `dead_link`：替代链接抓取失败（404、超时等）；
  - `missing_fields`：在售详情缺少关键包装字段（每包片数 / 含税包价 / 可购包数）；
  - `status_unknown`：末端页面未标注在售状态，无法核实；
  - `off_site`：替代链接指向站外，未追踪；
  - `limit_exceeded`：抓取页数超过上限（50 页），分支未追踪。
- 只要存在待核实分支，`status=partial` 且 `complete=false`——已核实分支照返，
  但**不得宣称结果完整**（可能还有其他可采购替代包装未确认）。
- `fetched_pages` / `fetch_count`：实际抓取的详情页（去重后），用于核对抓取次数。

页面标注约定（解析规则）：

- **在售状态**：`<td class="sale-status">在售</td>`（或 `[data-field="status"]` /
  `.product-status`），回退到文本正则"销售状态：在售 / 停产 / 下架"；
- **替代为链接**：`<a class="replaced-by" href="...">`（或 `.replaced-by` / `.replacement`
  容器内的链接），回退到锚文本或父元素文本含"替代"的链接；仅跟随与起始页**同站**的链接；
- **包装字段**：与报价 API 相同的 `.pack-size` / `.pack-price` / `.stock-packs`
  及中文标签正则。

## 规则与实现要点

### 报价优化（`app/scraper.py` + `app/optimizer.py`）

- **同站分页**：从 `catalog_url` 出发，识别 `rel="next"` / `class="next"` / 分页区
  "下一页"链接，仅跟随与目录同站点（同 host）的链接； visited 集合防环，上限 50 页。
- **跨页去重**：同一详情 URL 在多个列表页重复出现只计一次
  （`duplicate_detail_links_skipped` 计数）。
- **型号匹配**：先按列表文案匹配；一个都匹配不上时退回逐个抓取详情页核对型号
  （此时若有页面抓取失败，因无法确认是否漏掉更优匹配，同样不给确定最低价）。
- **详情解析**：优先 CSS 选择器（`.product-model` / `.pack-size` / `.pack-price` /
  `.stock-packs`），回退到中文标签正则（`型号：`、`每包片数：`、`含税包价：`、
  `可购包数：`）；缺任一关键字段即视为该详情解析失败。价格以"分"为整数运算，
  避免浮点误差。
- **组合优化**（`app/optimizer.py`）：有界背包 → 二进制拆分转 0/1 背包，对
  "恰好 j 片"做最小费用 DP，再按 **(总价, 总片数)** 字典序取最优——即总价最低，
  同价时超量更少。可证最优解总片数 < 需求 + 最大包装片数，DP 状态有界。
- **不给确定最低价的情形**：库存总量不足；匹配详情页抓取或解析失败。

### 替代链追溯（`app/chain.py`）

- **抓取一次、路径全留**：DFS + memo——每个详情 URL 全局只抓取/展开一次；
  合流节点（多条路径抵达）的子树结果被各条路径分别接续，因此末端 `paths` 保留
  所有抵达路径，而 `fetch_count` 不重复计数。
- **环检测**：DFS 栈内集合检测"后继已在当前路径中"，对应分支记 `cycle` 待核实，
  不会死循环；抓取上限 50 页兜底。
- **末端判定**：无"替代为"链接的页面为末端。在售 + 字段齐全 + 可购包数 > 0 →
  可采购末端；在售 + 缺字段 → `missing_fields` 待核实；在售 + 0 库存 → 已核实无货，
  单列；停产 + 无替代链接 → 已核实死胡同（确认无替代，不算待核实）；状态未标注 →
  `status_unknown` 待核实。
- **中间节点宽松解析**：停产中间页只要求能解析出"替代为"链接，不要求包装字段齐全。
- **站外链接**：不追踪，记 `off_site` 待核实（不静默丢弃）。

## 样本站说明（`sample_site/`）

报价样例（目录分页）：

- 3 页分页目录，`index.html → page2.html → page3.html`；
- `MF-50` 三种包装：50片/¥120（30包）、100片/¥225（10包）、25片/¥68（50包），
  可购总量 3750 片；
- `mf50-50.html` 在第 1、2 页**重复出现**（验证跨页去重）；
- `BROKEN-1` 的详情链接故意 404（验证"匹配详情抓取失败"分支）；
- 另有 `NY-66`、`CA-22` 两个其他型号商品。

替代链样例（独立详情页，不挂在目录中，直接用详情 URL 调用）：

- **分叉 + 合流 + 失效 + 缺字段**：`pes-45.html`（停产）→ `pes-45a.html` /
  `pes-45b.html`（分叉）；两者共同指向 `pes-45c.html`（合流：在售 100 片 ¥210，
  8 包，只抓取一次但保留 2 条路径）；`pes-45b.html` → `pes-45gone.html`
  （**失效链接**，404 → 待核实）；`pes-45a.html` → `pes-45d.html`
  （在售但**缺少"可购包数"** → 待核实）；
- **环**：`ptfe-9.html` ↔ `ptfe-9r.html` 互相标注为替代品（→ `cycle` 待核实）；
- **完整单链**：`mce-13.html`（停产）→ `mce-13p.html`（在售 50 片 ¥99，25 包），
  全部分支已核实，`status=ok`、`complete=true`。

试几组报价：`demand=180` → 2×100片 ¥450；`demand=175` → 100+50+25 各一包 ¥413（零超量）；
`demand=1000` → 买光 10 包 100 片装 ¥2250；`demand=4000` → `insufficient_stock`。

## 本地开发与测试

```bash
pip install -r app/requirements.txt
python -m unittest discover -s tests -v   # 优化器 + 替代链单元测试
python tests/e2e_check.py                 # 端到端检查（本地起样本站，走完整 API 链路）
```

## 目录结构

```
app/
  main.py        FastAPI 入口与响应组装（报价 + 替代链两个 API）
  scraper.py     分页抓取 + 详情字段解析（requests + BeautifulSoup）
  chain.py       替代链追踪（分叉/合流/环/失效/缺字段，抓取一次、路径全留）
  optimizer.py   库存约束下的最低总价整包组合（DP）
  requirements.txt / Dockerfile
sample_site/     离线样本站（静态 HTML：报价目录 + 替代链样例详情页）
tests/           单元测试（优化器、替代链）+ 端到端检查
docker-compose.yml
```
