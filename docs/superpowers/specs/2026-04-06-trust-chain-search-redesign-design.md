# 信任链搜索架构重构设计

## 背景

当前邮箱搜索严重依赖千问联网搜索（3轮 LLM 调用），存在系统性准确率问题：
- LLM 编造格式正确但不存在的邮箱
- Round 2 "验证" 是 LLM 自问自答，无法识别幻觉
- 一作信息网上少，LLM 更容易编造
- 找到同名不同人的邮箱
- 机构识别错误导致连锁错误

**核心升级理念**：从 "信任 LLM 给的邮箱" 变为 "只信任真实网页上提取的邮箱"。LLM 降级为"导航器"——只找 URL，不直接返回邮箱。

---

## 新架构：信任链 + 早退 + 评分

### 管道总览

```
处理顺序：先通讯作者 → 再一作（继承通讯的机构线索）

find_email_for_paper(doi, name, org, role, orcid, homepage, paper_title, corr_result)
│
├─ Layer 1: ORCID API 直查 (基础分 90)
│   └─ 有 ORCID ID → 查 pub.orcid.org 公开 API
│   └─ 获取: 公开邮箱、机构、个人页面 URL
│   └─ 命中邮箱 → 早退 ✅
│
├─ Layer 2: Crossref 邮箱字段 (基础分 85)
│   └─ doi_resolver 解析时直接提取 author.email
│   └─ 命中 → 早退 ✅
│
├─ Layer 3: 论文页面增强抓取 (基础分 80)
│   └─ 保留现有 _scrape_paper_page() 
│   └─ 新增: 解析 JSON-LD (schema.org) 结构化数据
│   └─ 新增: 解析 <meta name="citation_author_email"> 标签
│   └─ 命中 + 名字匹配 → 早退 ✅
│
├─ Layer 4: S2 / ORCID Homepage 抓取 (基础分 75)
│   └─ 保留现有 _scrape_homepage()
│   └─ 新增: ORCID 返回的 researcher-url 也尝试抓取
│   └─ 命中 → 早退 ✅
│
├─ Layer 5: 同机构推断 (一作专用, 基础分 70)
│   └─ 前提: role == "一作" 且 corr_result 已有结果
│   └─ 策略 5a: 继承通讯作者的机构域名作为一作的搜索约束
│   └─ 策略 5b: 抓取通讯作者的 lab/group page → 找 members/people 子页面
│   └─ 策略 5c: 在通讯作者机构的 people directory 中搜索一作名字
│   └─ 命中 → 早退 ✅
│
├─ Layer 6: LLM 导航模式 (基础分 60, 需页面验证)
│   └─ Prompt 改造: "请搜索 XXX 的个人页面 URL，不要返回邮箱"
│   └─ LLM 返回 URL 列表
│   └─ 逐个抓取 URL → 用 _extract_email_from_html() 提取邮箱
│   └─ 只需一轮（不再三轮）
│
└─ Layer 7: 评分验证 (所有结果必过)
    ├─ 基础分: 由来源层决定 (90/85/80/75/70/60)
    ├─ +10  域名与已知机构匹配
    ├─ +10  邮箱前缀与作者名字匹配
    ├─ +15  两个独立来源交叉印证同一邮箱
    ├─ +5   MX 记录验证通过
    ├─ -20  MX 记录验证失败
    ├─ -30  邮箱前缀与名字完全无关
    └─ 最终置信度: ≥70 高 / 50-69 中 / <50 低
```

---

## 文件级变更设计

### [NEW] `backend/services/orcid_resolver.py`

ORCID 公开 API 查询模块。

```python
# 核心函数
def query_orcid(orcid_id: str) -> dict:
    """
    查询 ORCID 公开 API，返回:
    {
        "email": "xxx@xxx.edu" | "",       # 公开邮箱（很多人不公开）
        "affiliations": ["MIT", ...],       # 机构列表
        "urls": ["https://lab.mit.edu/~xxx", ...],  # 个人页面URL列表
        "name": "Verified Name"             # ORCID 上的名字（可用于交叉验证）
    }
    """

# API 端点
# GET https://pub.orcid.org/v3.0/{orcid}/person
#   → 获取邮箱、名字
# GET https://pub.orcid.org/v3.0/{orcid}/employments
#   → 获取机构历史
# GET https://pub.orcid.org/v3.0/{orcid}/researcher-urls
#   → 获取个人页面链接

# Headers: Accept: application/json
# 无需 API Key（公开 API），但有速率限制（~24 req/sec）
```

**关键细节**：
- ORCID 邮箱需要研究者自己设为 public 才能查到，很多人没有公开
- 但即使没邮箱，ORCID 的 `researcher-urls`（个人页面链接）和 `employments`（机构）也非常有价值
- 可以拿到个人页面 URL → 交给 Layer 4 抓取

---

### [MODIFY] `backend/services/doi_resolver.py`

改动点：
1. **Crossref 解析时提取 `email` 字段**（当前完全忽略了这个字段）
2. **确保 ORCID 正确传递**（当前已有，但需确认格式统一）

```python
# _try_crossref() 中，author 循环内新增:
email = ""
# Crossref 有时在 author 对象中直接提供 email
if a.get("email"):
    email = a["email"]
# 某些出版商在 affiliation 子对象中放邮箱
for aff in a.get("affiliation", []):
    if "@" in aff.get("name", ""):
        # 有些出版商把邮箱塞在 affiliation name 里
        email_match = EMAIL_PATTERN.search(aff["name"])
        if email_match:
            email = email_match.group()

authors.append({
    "name": name,
    "affiliations": affs,
    "homepage": "",
    "orcid": a.get("ORCID", ""),
    "email": email,  # 新增！
    "is_corresponding": a.get("sequence", "") == "additional"
})
```

同理，`_enrich_affiliations_from_crossref()` 也需要将 email 字段合并。

---

### [MODIFY] `backend/services/author_extractor.py`

改动点：
1. **将 Crossref 邮箱传入输出结构**
2. **确保 ORCID 正确传递**

```python
# extract_authors() 返回结构增加 email 和 orcid 字段:
result = {
    "第一作者": {
        "姓名": first_name,
        "机构": first_org,
        "主页": first_homepage,
        "orcid": first_orcid,
        "crossref_email": first.get("email", ""),  # 新增
    },
    "通讯作者": {
        "姓名": corr_name,
        "机构": corr_org,
        "主页": corr_homepage,
        "orcid": corr_orcid,
        "crossref_email": corr.get("email", ""),  # 新增
    }
}
```

---

### [MODIFY] `backend/services/email_finder.py` — 核心重写

**删除**：
- `_qwen_search_round1()` — 替换为 LLM 导航模式
- `_qwen_search_round2_verify()` — 删除（LLM 自问自答验证无效）
- `_qwen_search_round3_deep()` — 合并进 LLM 导航模式

**保留**：
- `_scrape_paper_page()` — 保留 + 增强 JSON-LD 解析
- `_scrape_homepage()` — 保留
- `_extract_email_from_html()` — 保留 + 增强
- `_is_strong_name_match()` — 保留
- `_match_best_email()` — 保留
- `_verify_email_mx()` — 保留
- `_cache_org_domain()` / `_get_cached_domain()` — 保留

**新增**：

#### a) 新入口函数签名

```python
def find_email_for_paper(
    doi: str, name: str, org: str, role: str = "通讯",
    homepage: str = "", paper_title: str = "",
    orcid: str = "",              # 新增：ORCID ID
    crossref_email: str = "",      # 新增：Crossref 直接提供的邮箱
    corr_result: dict = None       # 新增：通讯作者已找到的结果（一作搜索时传入）
) -> dict:
```

返回值增加 `"来源URL"` 字段：
```python
{
    "邮箱": "xxx@fudan.edu.cn",
    "主页": "https://...",
    "来源": "orcid_api",
    "来源URL": "https://orcid.org/0000-0002-xxxx",  # 新增：可追溯
    "置信度": "高",
    "置信分": 95  # 新增：数值化分数
}
```

#### b) ORCID 查询层

```python
def _try_orcid_email(orcid: str, target_name: str) -> dict:
    """Layer 1: ORCID API 直查邮箱和个人页面"""
    # 1. 查询 ORCID person endpoint 获取公开邮箱
    # 2. 如果没有公开邮箱，获取 researcher-urls（个人页面链接）
    # 3. 获取 employments（确认机构信息）
    # 返回 {"email": "...", "urls": [...], "org": "..."}
```

#### c) Crossref 邮箱验证层

```python
def _try_crossref_email(crossref_email: str, target_name: str, org: str) -> dict:
    """Layer 2: 验证 Crossref 直接提供的邮箱"""
    # 1. 检查邮箱格式
    # 2. MX 验证
    # 3. 名字匹配检查
    # 直接来自出版商元数据，可信度很高
```

#### d) 论文页面增强

```python
def _scrape_paper_page_enhanced(doi: str, target_name: str) -> str:
    """Layer 3: 增强版论文页面抓取"""
    # 保留现有逻辑
    # 新增: 解析 JSON-LD
    #   <script type="application/ld+json">
    #   → 找 "author" 数组中的 "email" 字段
    # 新增: 解析 <meta name="citation_author_email">
    # 新增: 解析 <meta name="dc.creator" + "dc.email">
```

#### e) 同机构推断（一作专用）

```python
def _try_coaffiliation_search(
    target_name: str, corr_result: dict, paper_title: str
) -> dict:
    """Layer 5: 利用通讯作者信息搜索一作"""
    # 1. 从 corr_result 获取通讯作者的机构域名
    # 2. 从 corr_result 获取通讯作者的主页/lab URL
    # 3. 尝试抓取通讯 lab page 的 /members, /people, /team 子路径
    # 4. 在这些页面中搜索一作名字
    # 5. 如果找到 → 提取邮箱
    # 6. 如果没找到页面 → 尝试搜索 "{机构名} {一作名} email"
```

#### f) LLM 导航模式

```python
def _llm_navigate_and_scrape(
    name: str, org: str, doi: str, paper_title: str,
    cached_domain: str = ""
) -> dict:
    """Layer 6: LLM 只返回 URL，我们自己抓取"""
    prompt = f"""请帮我搜索以下学术研究者的**个人主页**、**实验室网页**、
    **大学通讯录页面**的 URL 链接：

    姓名：{name}
    机构：{org}
    论文：{paper_title}

    ⚠️ 重要：请只返回页面 URL，不要返回邮箱地址。
    我需要的是能找到该学者联系方式的网页链接。

    请返回 JSON：
    {{"urls": ["url1", "url2", ...], "source_description": "..."}}
    """
    # 1. 调用 smart_generate_with_search(prompt)
    # 2. 解析返回的 URL 列表
    # 3. 逐个抓取 URL → _extract_email_from_html()
    # 4. 返回第一个通过名字匹配的邮箱
```

#### g) 评分验证函数

```python
def _score_email(
    email: str, source_layer: int, target_name: str,
    known_org: str, all_candidates: list
) -> int:
    """计算邮箱可信度分数"""
    BASE_SCORES = {1: 90, 2: 85, 3: 80, 4: 75, 5: 70, 6: 60}
    score = BASE_SCORES.get(source_layer, 50)

    # 域名与机构匹配
    if _domain_matches_org(email, known_org):
        score += 10

    # 名字匹配
    if _is_strong_name_match(email, target_name):
        score += 10

    # 多源交叉印证
    if email in [c["email"] for c in all_candidates if c.get("email")]:
        score += 15

    # MX 验证
    if _verify_email_mx(email):
        score += 5
    else:
        score -= 20

    return score
```

---

### [MODIFY] `backend/main.py`

改动点：**先搜通讯，再搜一作，将通讯结果传给一作搜索**

```python
# 核心变化：通讯先搜，结果传给一作

# 4. 搜索邮箱——通讯作者（优先搜）
corr_email_data = await asyncio.to_thread(
    find_email_for_paper, doi, corr_name, corr_org, "通讯",
    corr_homepage, title,
    corr_author.get("orcid", ""),           # 传入 ORCID
    corr_author.get("crossref_email", ""),   # 传入 Crossref 邮箱
    None                                     # 通讯没有 corr_result
)

# 5. 搜索邮箱——第一作者（传入通讯结果做同机构推断）
first_email_data = await asyncio.to_thread(
    find_email_for_paper, doi, first_name, first_org, "一作",
    first_homepage, title,
    first_author.get("orcid", ""),           # 传入 ORCID
    first_author.get("crossref_email", ""),   # 传入 Crossref 邮箱
    corr_email_data                           # 传入通讯结果！
)
```

---

## 置信度输出示例

### 高置信度场景
```
输入: DOI 10.1016/j.cell.2026.01.018
      通讯作者: Motoyuki Hattori, ORCID: 0000-0002-xxxx

Layer 1 (ORCID): ORCID 公开邮箱 hattorim@fudan.edu.cn
评分: 90(base) + 10(域名match fudan) + 10(名字match hattori) + 5(MX) = 115 → 高
来源URL: https://orcid.org/0000-0002-xxxx
→ 早退，不再搜后续 Layer
```

### 中置信度场景
```
输入: 一作 Haikun Song, 无 ORCID, 无 Crossref email

Layer 1 (ORCID): 无 ORCID → 跳过
Layer 2 (Crossref): 无 email → 跳过
Layer 3 (论文页面): 页面只有通讯邮箱 → 跳过
Layer 4 (Homepage): 无 homepage → 跳过
Layer 5 (同机构): 通讯在 Fudan → 搜 Fudan people directory
  → 找到 life.fudan.edu.cn/people 页面
  → 页面中有 "Haikun Song, songhk@fudan.edu.cn"
评分: 70(base) + 10(域名match) + 10(名字match) + 5(MX) = 95 → 高
来源URL: https://life.fudan.edu.cn/people/xxx
```

### 低置信度场景
```
Layer 1-5 均未命中
Layer 6 (LLM导航):
  LLM 返回 URL: https://scholar.google.com/citations?user=xxx
  抓取该页面 → 没有邮箱但有 "Verified email at fudan.edu.cn"
  → 只知道域名，不知道具体邮箱
输出: 邮箱=未找到, 但记录 "邮箱域名可能为 @fudan.edu.cn"
```

---

## 不变的部分

以下模块保持不变，不在本次重构范围内：
- `document_parser.py` — DOI 提取逻辑不变
- `frontend/` — 前端展示不变（但可以利用新增的 `来源URL` 字段做展示优化）
- `config.py` — LLM 配置不变

---

## 验证计划

### 自动测试
- 使用现有的 `test_batch.py`，对比重构前后的准确率
- 选取已知邮箱的 DOI（10 篇以上），比较命中率

### 人工验证
- 抽样检查输出的 `来源URL` 是否真的包含对应邮箱
- 检查"低置信度"结果是否确实可疑
