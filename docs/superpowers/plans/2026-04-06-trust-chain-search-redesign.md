# 信任链搜索架构重构 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将邮箱搜索从"信任 LLM 给邮箱"重构为"信任链管道 + LLM 只做导航"架构，消除 LLM 幻觉导致的假邮箱问题。

**Architecture:** 7 层信任链管道按可信度从高到低排列（ORCID → Crossref email → 论文页面 → Homepage → 同机构推断 → LLM 导航 → 评分验证），高可信源命中即早退。先搜通讯作者，再搜一作（继承通讯的机构信息）。

**Tech Stack:** Python 3, requests, BeautifulSoup4, cloudscraper, dns.resolver, OpenAI-compatible API (千问)

**Design Spec:** `docs/superpowers/specs/2026-04-06-trust-chain-search-redesign-design.md`

---

## File Structure

### New Files
- `backend/services/orcid_resolver.py` — ORCID 公开 API 查询模块（查邮箱、机构、个人页面 URL）

### Modified Files
- `backend/services/doi_resolver.py` — 新增 Crossref email 字段提取
- `backend/services/author_extractor.py` — 透传 crossref_email 字段
- `backend/services/email_finder.py` — 核心重写：信任链管道替换三轮 LLM 搜索
- `backend/main.py` — 先搜通讯再搜一作，传递 ORCID + crossref_email + corr_result

---

### Task 1: 创建 ORCID 查询模块

**Files:**
- Create: `backend/services/orcid_resolver.py`

- [ ] **Step 1: 创建 `orcid_resolver.py` 基础结构**

创建 `backend/services/orcid_resolver.py`：

```python
"""
ORCID 公开 API 查询模块。
通过 ORCID ID 查询研究者的公开邮箱、机构、个人页面链接。
API 文档: https://info.orcid.org/documentation/api-tutorials/

公开 API 不需要 API Key，但有速率限制（约 24 req/sec）。
"""
import requests
import re
import time

ORCID_API_BASE = "https://pub.orcid.org/v3.0"
ORCID_HEADERS = {
    "Accept": "application/json"
}
ORCID_TIMEOUT = 10

# ORCID ID 格式: 0000-0002-1234-5678
ORCID_PATTERN = re.compile(r'\d{4}-\d{4}-\d{4}-\d{3}[\dX]')


def normalize_orcid(raw: str) -> str:
    """
    从各种格式中提取标准 ORCID ID。
    支持:
      - 纯 ID: "0000-0002-1234-5678"
      - URL: "https://orcid.org/0000-0002-1234-5678"
      - 带前缀: "ORCID: 0000-0002-1234-5678"
    """
    if not raw:
        return ""
    match = ORCID_PATTERN.search(raw)
    return match.group() if match else ""


def query_orcid(orcid_id: str) -> dict:
    """
    查询 ORCID 公开 API，返回研究者的公开信息。

    Args:
        orcid_id: 标准 ORCID ID（如 "0000-0002-1234-5678"）或包含 ID 的字符串

    Returns:
        {
            "email": "xxx@xxx.edu" | "",        # 公开邮箱
            "emails": ["xxx@xxx.edu", ...],     # 所有公开邮箱
            "name": "Full Name",                # ORCID 上的名字
            "affiliations": ["MIT", ...],       # 机构列表
            "urls": ["https://...", ...],       # 个人页面URL列表
            "success": True/False               # 查询是否成功
        }
    """
    orcid = normalize_orcid(orcid_id)
    if not orcid:
        return {"email": "", "emails": [], "name": "", "affiliations": [],
                "urls": [], "success": False}

    print(f"    🔗 [ORCID] 查询 {orcid}...")
    result = {
        "email": "", "emails": [], "name": "", "affiliations": [],
        "urls": [], "success": False
    }

    try:
        # 1. 查询 person 端点（邮箱 + 名字 + 个人页面链接）
        person_data = _fetch_person(orcid)
        if person_data:
            result["name"] = person_data.get("name", "")
            result["emails"] = person_data.get("emails", [])
            result["email"] = person_data["emails"][0] if person_data.get("emails") else ""
            result["urls"] = person_data.get("urls", [])

        # 2. 查询 employments 端点（机构历史）
        affs = _fetch_employments(orcid)
        if affs:
            result["affiliations"] = affs

        result["success"] = True
        _log_result(orcid, result)

    except Exception as e:
        print(f"    ⚠️ [ORCID] 查询异常: {e}")

    return result


def _fetch_person(orcid: str) -> dict:
    """获取 ORCID person 数据（邮箱、名字、研究者链接）"""
    try:
        url = f"{ORCID_API_BASE}/{orcid}/person"
        resp = requests.get(url, headers=ORCID_HEADERS, timeout=ORCID_TIMEOUT)
        if resp.status_code != 200:
            print(f"    ⚠️ [ORCID] person 端点返回 {resp.status_code}")
            return {}

        data = resp.json()
        result = {"name": "", "emails": [], "urls": []}

        # 提取名字
        name_data = data.get("name", {})
        if name_data:
            given = name_data.get("given-names", {}).get("value", "")
            family = name_data.get("family-name", {}).get("value", "")
            result["name"] = f"{given} {family}".strip()

        # 提取公开邮箱
        emails_data = data.get("emails", {}).get("email", [])
        for em in emails_data:
            email_val = em.get("email", "")
            if email_val:
                result["emails"].append(email_val)

        # 提取 researcher-urls（个人页面链接）
        urls_data = data.get("researcher-urls", {}).get("researcher-url", [])
        for u in urls_data:
            url_val = u.get("url", {}).get("value", "")
            if url_val:
                result["urls"].append(url_val)

        return result

    except Exception as e:
        print(f"    ⚠️ [ORCID] person 请求异常: {e}")
        return {}


def _fetch_employments(orcid: str) -> list:
    """获取 ORCID 就业/机构历史"""
    try:
        url = f"{ORCID_API_BASE}/{orcid}/employments"
        resp = requests.get(url, headers=ORCID_HEADERS, timeout=ORCID_TIMEOUT)
        if resp.status_code != 200:
            return []

        data = resp.json()
        affiliations = []

        # employments 结构: affiliation-group -> summaries -> employment-summary
        for group in data.get("affiliation-group", []):
            for summary in group.get("summaries", []):
                emp = summary.get("employment-summary", {})
                org = emp.get("organization", {})
                org_name = org.get("name", "")
                if org_name and org_name not in affiliations:
                    affiliations.append(org_name)

        return affiliations

    except Exception as e:
        print(f"    ⚠️ [ORCID] employments 请求异常: {e}")
        return []


def _log_result(orcid: str, result: dict):
    """打印 ORCID 查询结果摘要"""
    if result["email"]:
        print(f"    ✅ [ORCID] 找到公开邮箱: {result['email']}")
    elif result["urls"]:
        print(f"    📎 [ORCID] 无公开邮箱，但有 {len(result['urls'])} 个页面链接")
    elif result["affiliations"]:
        print(f"    🏛️ [ORCID] 无邮箱/链接，但有机构: {result['affiliations'][0]}")
    else:
        print(f"    ⚠️ [ORCID] {orcid} 信息极少（可能未公开）")
```

- [ ] **Step 2: 验证 ORCID 模块可导入**

运行:
```bash
cd d:\Scholar_Agent
python -c "from backend.services.orcid_resolver import query_orcid, normalize_orcid; print('OK')"
```
Expected: 输出 `OK`，无 import 错误

- [ ] **Step 3: Commit**

```bash
git add backend/services/orcid_resolver.py
git commit -m "feat: add ORCID public API query module"
```

---

### Task 2: 修改 DOI 解析器提取 Crossref 邮箱

**Files:**
- Modify: `backend/services/doi_resolver.py`

**Context:** 当前 `_try_crossref()` 在解析 Crossref 返回的 author 对象时，完全忽略了 `email` 字段。某些出版商会在 Crossref 元数据中直接提供作者邮箱。同样，`_enrich_affiliations_from_crossref()` 也需要传递 email。

- [ ] **Step 1: 在文件顶部添加 EMAIL_PATTERN**

在 `doi_resolver.py` 的 import 区域后添加:

```python
import re

# 邮箱正则（用于从 affiliation 中提取误放的邮箱）
EMAIL_PATTERN = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
```

- [ ] **Step 2: 修改 `_try_crossref()` 提取 email 字段**

在 `_try_crossref()` 函数中，修改 author 循环。当前代码（约第206-225行）：

```python
for a in msg.get("author", []):
    name_parts = []
    if a.get("given"):
        name_parts.append(a["given"])
    if a.get("family"):
        name_parts.append(a["family"])
    name = " ".join(name_parts)

    affs = []
    for aff in a.get("affiliation", []):
        if aff.get("name"):
            affs.append(aff["name"])

    authors.append({
        "name": name,
        "affiliations": affs,
        "homepage": "",
        "orcid": a.get("ORCID", ""),
        "is_corresponding": a.get("sequence", "") == "additional"
    })
```

替换为:

```python
for a in msg.get("author", []):
    name_parts = []
    if a.get("given"):
        name_parts.append(a["given"])
    if a.get("family"):
        name_parts.append(a["family"])
    name = " ".join(name_parts)

    affs = []
    email = ""
    for aff in a.get("affiliation", []):
        aff_name = aff.get("name", "")
        if aff_name:
            # 某些出版商把邮箱塞在 affiliation name 里
            email_match = EMAIL_PATTERN.search(aff_name)
            if email_match:
                email = email_match.group()
                # 清理 affiliation：去掉邮箱部分
                clean_aff = EMAIL_PATTERN.sub("", aff_name).strip().strip(",; ")
                if clean_aff:
                    affs.append(clean_aff)
            else:
                affs.append(aff_name)

    # Crossref 有时在 author 对象中直接提供 email
    if not email and a.get("email"):
        email = a["email"]

    authors.append({
        "name": name,
        "affiliations": affs,
        "homepage": "",
        "orcid": a.get("ORCID", ""),
        "email": email,  # 新增！Crossref 直接提供的邮箱
        "is_corresponding": a.get("sequence", "") == "additional"
    })
```

- [ ] **Step 3: 修改 `_enrich_affiliations_from_crossref()` 也传递 email**

在 `_enrich_affiliations_from_crossref()` 中（约第50-105行），修改 Crossref 映射构建部分。

在构建 `cr_aff_map` 时，当前代码:
```python
orcid = a.get("ORCID", "")
if affs or orcid:
    cr_aff_map[full] = {"affs": affs, "orcid": orcid}
    if family:
        cr_aff_map[family] = {"affs": affs, "orcid": orcid}
```

替换为:
```python
orcid = a.get("ORCID", "")
email = a.get("email", "")
if affs or orcid or email:
    cr_aff_map[full] = {"affs": affs, "orcid": orcid, "email": email}
    if family:
        cr_aff_map[family] = {"affs": affs, "orcid": orcid, "email": email}
```

在补全循环中，当前代码:
```python
if found_cr:
    if not author.get("affiliations") and found_cr["affs"]:
        author["affiliations"] = found_cr["affs"]
        enriched_count += 1
    if found_cr["orcid"]:
        author["orcid"] = found_cr["orcid"]
```

替换为:
```python
if found_cr:
    if not author.get("affiliations") and found_cr["affs"]:
        author["affiliations"] = found_cr["affs"]
        enriched_count += 1
    if found_cr["orcid"]:
        author["orcid"] = found_cr["orcid"]
    if found_cr.get("email") and not author.get("email"):
        author["email"] = found_cr["email"]
```

- [ ] **Step 4: 确保 S2 返回结构也包含 email 字段**

在 `_try_semantic_scholar()` 的 author 构建中（约第124-131行），当前:
```python
authors.append({
    "name": a.get("name", ""),
    "affiliations": a.get("affiliations", []) or [],
    "homepage": a.get("homepage", ""),
    "orcid": a.get("externalIds", {}).get("ORCID", ""),
    "is_corresponding": False
})
```

添加 `"email": ""` 字段:
```python
authors.append({
    "name": a.get("name", ""),
    "affiliations": a.get("affiliations", []) or [],
    "homepage": a.get("homepage", ""),
    "orcid": a.get("externalIds", {}).get("ORCID", ""),
    "email": "",  # S2 不提供邮箱，后续由其他层补充
    "is_corresponding": False
})
```

同样修改 `_s2_fallback_by_title()` 中的 author 构建（约第174-181行），加入 `"email": ""`。

- [ ] **Step 5: 验证修改**

```bash
cd d:\Scholar_Agent
python -c "from backend.services.doi_resolver import resolve_doi; print('import OK')"
```

- [ ] **Step 6: Commit**

```bash
git add backend/services/doi_resolver.py
git commit -m "feat: extract email field from Crossref author metadata"
```

---

### Task 3: 修改作者提取器透传新字段

**Files:**
- Modify: `backend/services/author_extractor.py`

**Context:** `author_extractor.py` 从 DOI 解析结果中提取第一作者和通讯作者。需要让 `crossref_email` 和 `orcid` 字段正确透传到输出。

- [ ] **Step 1: 修改 `extract_authors()` 透传 email 字段**

当前代码（约第27-32行）:
```python
first = authors[0]
first_name = first.get("name", "未找到")
first_org = ", ".join(first.get("affiliations", [])) or "未找到"
first_homepage = first.get("homepage", "")
first_orcid = first.get("orcid", "")
```

在后面添加:
```python
first_email = first.get("email", "")
```

修改 result 构建（约第43-46行）:
```python
result = {
    "第一作者": {"姓名": first_name, "机构": first_org, "主页": first_homepage,
                "orcid": first_orcid, "crossref_email": first_email},
    "通讯作者": corr
}
```

- [ ] **Step 2: 修改 `_identify_corresponding_normal()` 透传字段**

当前返回字典（约第60-65行和67-73行），确保每个返回都包含 `crossref_email`:

```python
def _identify_corresponding_normal(authors: list) -> dict:
    """常规论文：从结构化数据中提取通讯作者"""
    for a in authors:
        if a.get("is_corresponding"):
            return {
                "姓名": a.get("name", "未找到"),
                "机构": ", ".join(a.get("affiliations", [])) or "未找到",
                "主页": a.get("homepage", ""),
                "orcid": a.get("orcid", ""),
                "crossref_email": a.get("email", "")  # 新增
            }
    last = authors[-1]
    return {
        "姓名": last.get("name", "未找到"),
        "机构": ", ".join(last.get("affiliations", [])) or "未找到",
        "主页": last.get("homepage", ""),
        "orcid": last.get("orcid", ""),
        "crossref_email": last.get("email", "")  # 新增
    }
```

- [ ] **Step 3: 修改 `_identify_corresponding_large_paper()` 透传字段**

确保该函数的所有返回路径都包含 `crossref_email`:

在显式标记路径（约第83-89行）加入:
```python
"crossref_email": a.get("email", "")
```

在 LLM 分析结果路径（约第119-124行），由于 LLM 不返回 email，设为空:
```python
if result.get("姓名"):
    result["crossref_email"] = ""  # LLM 分析无法提供 email
    return result
```

在兜底路径（约第128-134行）加入:
```python
return {
    "姓名": fallback.get("name", "未找到"),
    "机构": ", ".join(fallback.get("affiliations", [])) or "未找到",
    "orcid": fallback.get("orcid", ""),
    "crossref_email": fallback.get("email", "")  # 新增
}
```

- [ ] **Step 4: 验证修改**

```bash
cd d:\Scholar_Agent
python -c "from backend.services.author_extractor import extract_authors; print('import OK')"
```

- [ ] **Step 5: Commit**

```bash
git add backend/services/author_extractor.py
git commit -m "feat: pass through crossref_email in author extractor"
```

---

### Task 4: 重写 email_finder.py — 信任链管道

**Files:**
- Modify: `backend/services/email_finder.py` (核心重写)

**Context:** 这是最大的改动。将当前的"三轮 LLM 搜索"替换为"7 层信任链管道"。保留现有的工具函数（`_scrape_paper_page`, `_scrape_homepage`, `_extract_email_from_html`, `_is_strong_name_match`, `_match_best_email`, `_verify_email_mx`, `_cache_org_domain`, `_get_cached_domain`），删除三个 `_qwen_search_round*` 函数，新增 ORCID 层、Crossref 层、同机构推断层、LLM 导航层、评分验证层。

**重要依赖:** 需要 Task 1 的 `orcid_resolver.py` 已存在。

- [ ] **Step 1: 重写文件头部文档字符串和导入**

将文件最开头的文档字符串和 import 区域（约第1-36行）替换为:

```python
"""
邮箱查找器 V4.0 —— 信任链架构。

核心升级（V4.0）：
1. 7 层信任链管道，按可信度从高到低排列
2. ORCID API 直查（最高可信源）
3. Crossref 邮箱字段提取（之前完全忽略的数据）
4. LLM 降级为"导航器"——只找 URL，不直接返回邮箱
5. 同机构推断（一作专用）——继承通讯作者的机构线索
6. 评分制验证——非硬门槛，gmail 等非机构邮箱也不拒绝
7. 每条结果附来源 URL——可追溯

管道流程：
  Layer 1: ORCID API 直查 (基础分 90)
  Layer 2: Crossref 邮箱字段 (基础分 85)
  Layer 3: 论文页面抓取 (基础分 80)
  Layer 4: S2/ORCID Homepage 抓取 (基础分 75)
  Layer 5: 同机构推断 - 一作专用 (基础分 70)
  Layer 6: LLM 导航模式 (基础分 60)
  Layer 7: 评分验证 (所有结果必过)
"""
import re
import json
import time
import socket
import dns.resolver
import requests
import cloudscraper
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from functools import lru_cache

from backend.config import (
    smart_generate, smart_generate_with_search,
    HTTP_TIMEOUT, SCRAPE_TIMEOUT, API_RATE_LIMIT_DELAY
)
from backend.services.orcid_resolver import query_orcid, normalize_orcid
```

- [ ] **Step 2: 保留工具函数区域不变**

以下函数完全保留不动（确认不修改）:
- `EMAIL_PATTERN` (第39行)
- `NOISE_EMAILS` (第42-45行)
- `_org_domain_cache` 和 `_cache_org_domain()` 和 `_get_cached_domain()` (第48-77行)
- `_verify_email_mx()` (第83-97行)
- `_scrape_paper_page()` (第246-298行)
- `_scrape_homepage()` (第304-321行)
- `_extract_elsevier_redirect()` (第324-345行)
- `_extract_email_from_html()` (第348-396行)
- `_is_noise_email()` (第550-553行)
- `_is_strong_name_match()` (第556-621行)
- `_match_best_email()` (第624-659行)

- [ ] **Step 3: 重写主入口函数 `find_email_for_paper()`**

删除当前的 `find_email_for_paper()` 函数（约第103-234行），替换为新的信任链管道版本:

```python
# ================================================================
# 信任链评分常量
# ================================================================
LAYER_BASE_SCORES = {
    1: 90,   # ORCID API
    2: 85,   # Crossref 元数据
    3: 80,   # 论文页面抓取
    4: 75,   # Homepage 抓取
    5: 70,   # 同机构推断
    6: 60,   # LLM 导航
}


# ================================================================
# 主入口：信任链管道
# ================================================================
def find_email_for_paper(doi: str, name: str, org: str, role: str = "通讯",
                         homepage: str = "", paper_title: str = "",
                         orcid: str = "", crossref_email: str = "",
                         corr_result: dict = None) -> dict:
    """
    信任链架构搜索策略定位作者邮箱。

    Args:
        doi:            论文 DOI
        name:           作者姓名
        org:            作者机构
        role:           角色（"一作" 或 "通讯"）
        homepage:       S2 返回的作者个人主页 URL（可选）
        paper_title:    论文标题（可选）
        orcid:          作者 ORCID ID（可选）
        crossref_email: Crossref 元数据中直接提供的邮箱（可选）
        corr_result:    通讯作者已找到的结果（一作搜索时传入，可选）
    """
    invalid_tags = ["未提供", "未找到", "无", "none", "unknown", ""]
    if not name or name.lower().strip() in invalid_tags:
        return _empty_result()

    print(f"\n  📧 [V4.0 信任链] 开始追踪: {name} ({org}) [角色: {role}]")

    # 收集所有候选邮箱（用于后续交叉印证）
    all_candidates = []
    # 收集所有可抓取的 URL（ORCID 返回的 + homepage + 后续发现的）
    extra_urls = []

    # ==============================================================
    # Layer 1: ORCID API 直查 (基础分 90)
    # ==============================================================
    if orcid:
        print(f"  🔗 [Layer1] ORCID API 查询...")
        orcid_data = query_orcid(orcid)
        if orcid_data.get("success"):
            # 收集 ORCID 返回的 URL，后续 Layer 4 可用
            extra_urls.extend(orcid_data.get("urls", []))

            if orcid_data.get("email"):
                email = orcid_data["email"]
                score = _score_email(email, 1, name, org, all_candidates)
                if score >= 50:  # ORCID 自填邮箱，基本都过
                    print(f"  ✅ [Layer1] ORCID 公开邮箱命中: {email} (得分: {score})")
                    _cache_org_domain(org, email)
                    return _build_result(email, f"https://orcid.org/{normalize_orcid(orcid)}",
                                        "orcid_api", score)
                else:
                    all_candidates.append({"email": email, "layer": 1, "score": score})

            # ORCID 有机构信息但没邮箱 → 更新 org（可能比 Crossref 的更准确）
            if orcid_data.get("affiliations") and org in invalid_tags:
                org = orcid_data["affiliations"][0]
                print(f"  🏛️ [Layer1] ORCID 补全机构: {org}")

    # ==============================================================
    # Layer 2: Crossref 邮箱字段 (基础分 85)
    # ==============================================================
    if crossref_email:
        print(f"  📋 [Layer2] Crossref 元数据邮箱: {crossref_email}")
        if _verify_email_mx(crossref_email):
            score = _score_email(crossref_email, 2, name, org, all_candidates)
            if score >= 50:
                print(f"  ✅ [Layer2] Crossref 邮箱验证通过 (得分: {score})")
                _cache_org_domain(org, crossref_email)
                return _build_result(crossref_email, f"https://doi.org/{doi}",
                                    "crossref_metadata", score)
        else:
            print(f"  ⚠️ [Layer2] Crossref 邮箱 MX 验证失败")
            all_candidates.append({"email": crossref_email, "layer": 2, "score": 30})

    # ==============================================================
    # Layer 3: 论文页面抓取 (基础分 80)
    # ==============================================================
    if doi:
        print(f"  🎯 [Layer3] 论文页面抓取 (doi.org/{doi})...")
        paper_email = _scrape_paper_page(doi, name)
        if paper_email:
            score = _score_email(paper_email, 3, name, org, all_candidates)
            if score >= 50 and _verify_email_mx(paper_email):
                print(f"  ✅ [Layer3] 论文页面命中: {paper_email} (得分: {score})")
                _cache_org_domain(org, paper_email)
                return _build_result(paper_email, f"https://doi.org/{doi}",
                                    "paper_page", score)
            else:
                all_candidates.append({"email": paper_email, "layer": 3, "score": score})

    # ==============================================================
    # Layer 4: Homepage 抓取 (基础分 75)
    # ==============================================================
    # 合并所有已知的 homepage URL
    all_homepages = []
    if homepage and homepage.startswith("http"):
        all_homepages.append(homepage)
    all_homepages.extend([u for u in extra_urls if u.startswith("http")])

    for hp_url in all_homepages[:3]:  # 最多尝试 3 个
        print(f"  🏠 [Layer4] 抓取个人主页: {hp_url[:60]}...")
        hp_email = _scrape_homepage(hp_url, name)
        if hp_email and _verify_email_mx(hp_email):
            score = _score_email(hp_email, 4, name, org, all_candidates)
            if score >= 50:
                print(f"  ✅ [Layer4] 主页命中: {hp_email} (得分: {score})")
                _cache_org_domain(org, hp_email)
                return _build_result(hp_email, hp_url, "homepage_scrape", score)
            else:
                all_candidates.append({"email": hp_email, "layer": 4, "score": score})

    # ==============================================================
    # Layer 5: 同机构推断 (一作专用, 基础分 70)
    # ==============================================================
    if role in ["一作", "first_author"] and corr_result:
        print(f"  🔄 [Layer5] 同机构推断（利用通讯作者信息）...")
        coaff_email = _try_coaffiliation_search(name, corr_result, paper_title)
        if coaff_email:
            score = _score_email(coaff_email, 5, name, org, all_candidates)
            if score >= 50 and _verify_email_mx(coaff_email):
                source_url = corr_result.get("来源URL", corr_result.get("主页", ""))
                print(f"  ✅ [Layer5] 同机构推断命中: {coaff_email} (得分: {score})")
                _cache_org_domain(org, coaff_email)
                return _build_result(coaff_email, source_url, "coaffiliation", score)
            else:
                all_candidates.append({"email": coaff_email, "layer": 5, "score": score})

    # ==============================================================
    # Layer 6: LLM 导航模式 (基础分 60)
    # ==============================================================
    print(f"  🧭 [Layer6] LLM 导航搜索...")
    nav_result = _llm_navigate_and_scrape(name, org, doi, paper_title,
                                          _get_cached_domain(org))
    if nav_result.get("email"):
        nav_email = nav_result["email"]
        score = _score_email(nav_email, 6, name, org, all_candidates)
        if score >= 50 and _verify_email_mx(nav_email):
            print(f"  ✅ [Layer6] LLM 导航命中: {nav_email} (得分: {score})")
            _cache_org_domain(org, nav_email)
            return _build_result(nav_email, nav_result.get("source_url", ""),
                                "llm_navigate", score)
        else:
            all_candidates.append({"email": nav_email, "layer": 6, "score": score})

    # 新增：如果 LLM 找到了主页 URL 但没邮箱，也记录主页
    nav_homepage = nav_result.get("homepage", "")

    # ==============================================================
    # 兜底：从所有候选中选最高分的
    # ==============================================================
    if all_candidates:
        # 重新计算分数（可能有交叉印证加分）
        for c in all_candidates:
            c["score"] = _score_email(c["email"], c["layer"], name, org, all_candidates)

        best = max(all_candidates, key=lambda x: x["score"])
        if best["score"] >= 40:
            print(f"  📬 [兜底] 选择最高分候选: {best['email']} (得分: {best['score']})")
            return _build_result(best["email"],
                                nav_homepage or homepage or "",
                                f"candidate_layer{best['layer']}",
                                best["score"])

    # ==============================================================
    # 全部未命中
    # ==============================================================
    print(f"  ❌ 所有层级均未找到可信邮箱")
    final_homepage = nav_homepage or homepage or ""
    return {
        "邮箱": "未找到",
        "主页": final_homepage if final_homepage else "未找到",
        "来源": "none",
        "来源URL": "",
        "置信度": "无",
        "置信分": 0
    }
```

- [ ] **Step 4: 添加辅助函数：结果构建和空结果**

在主入口函数之后添加:

```python
def _empty_result():
    """返回空结果"""
    return {"邮箱": "未找到", "主页": "未找到", "来源": "none",
            "来源URL": "", "置信度": "无", "置信分": 0}


def _build_result(email: str, source_url: str, source: str, score: int) -> dict:
    """构建标准输出结果"""
    if score >= 70:
        confidence = "高"
    elif score >= 50:
        confidence = "中"
    else:
        confidence = "低"

    return {
        "邮箱": email,
        "主页": source_url if source_url else "未找到",
        "来源": source,
        "来源URL": source_url,
        "置信度": confidence,
        "置信分": score
    }
```

- [ ] **Step 5: 添加评分函数**

```python
def _score_email(email: str, source_layer: int, target_name: str,
                 known_org: str, all_candidates: list) -> int:
    """
    计算邮箱可信度分数。

    评分规则:
      基础分由来源层决定 (90/85/80/75/70/60)
      +10  域名与已知机构匹配
      +10  邮箱前缀与作者名字匹配
      +15  两个独立来源交叉印证同一邮箱
      +5   MX 记录验证通过
      -20  MX 记录验证失败
      -30  邮箱前缀与名字完全无关（且来源非 ORCID/Crossref）
    """
    score = LAYER_BASE_SCORES.get(source_layer, 50)

    # 域名与机构匹配
    if _domain_matches_org(email, known_org):
        score += 10

    # 名字匹配
    if _is_strong_name_match(email, target_name):
        score += 10
    elif source_layer > 2:
        # 非高可信源（ORCID/Crossref 除外）且名字不匹配，扣分
        # 但不对 ORCID 和 Crossref 扣分（它们是元数据源，邮箱前缀可能与名字无关）
        prefix = email.split("@")[0].lower()
        name_parts = target_name.lower().split()
        if name_parts and not any(p in prefix for p in name_parts if len(p) >= 2):
            score -= 30

    # 多源交叉印证
    other_emails = [c["email"] for c in all_candidates
                    if c.get("email") and c.get("layer") != source_layer]
    if email in other_emails:
        score += 15

    # MX 验证
    if _verify_email_mx(email):
        score += 5
    else:
        score -= 20

    return score


def _domain_matches_org(email: str, org: str) -> bool:
    """检查邮箱域名是否与机构名匹配"""
    if not email or not org or org in ["未提供", "未找到", "无", ""]:
        return False

    domain = email.split("@")[-1].lower()
    org_lower = org.lower()

    # 直接包含关系检查
    # 例: "fudan.edu.cn" 匹配 "Fudan University"
    domain_parts = domain.replace(".", " ").split()
    for part in domain_parts:
        if len(part) >= 3 and part in org_lower:
            return True

    # 机构名关键词在域名中
    org_words = re.split(r'[\s,;]+', org_lower)
    for word in org_words:
        if len(word) >= 4 and word in domain:
            return True

    # 缓存的域名匹配
    cached = _get_cached_domain(org)
    if cached and cached == domain:
        return True

    return False
```

- [ ] **Step 6: 添加同机构推断函数（Layer 5）**

```python
def _try_coaffiliation_search(target_name: str, corr_result: dict,
                               paper_title: str = "") -> str:
    """
    Layer 5: 利用通讯作者信息搜索一作邮箱。

    策略:
    1. 从通讯作者结果中获取机构域名
    2. 尝试抓取通讯作者主页/lab页面的 members/people 子路径
    3. 在这些页面中搜索一作名字
    """
    if not corr_result or corr_result.get("邮箱") == "未找到":
        return ""

    # 获取通讯作者的机构域名
    corr_email = corr_result.get("邮箱", "")
    corr_homepage = corr_result.get("主页", "") or corr_result.get("来源URL", "")

    # 策略 A: 从通讯 lab page 找一作
    if corr_homepage and corr_homepage.startswith("http"):
        lab_email = _search_lab_page_for_member(corr_homepage, target_name)
        if lab_email:
            return lab_email

    # 策略 B: 如果知道机构域名，尝试生成该机构的 people directory URL
    if corr_email and "@" in corr_email:
        domain = corr_email.split("@")[-1]
        # 尝试常见的大学 people directory 路径
        base_domain = domain
        if domain.startswith("mail.") or domain.startswith("email."):
            base_domain = domain.split(".", 1)[1]

        people_urls = [
            f"https://www.{base_domain}",  # 先尝试首页
        ]
        for people_url in people_urls:
            try:
                scraper = cloudscraper.create_scraper(
                    browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
                )
                resp = scraper.get(people_url, timeout=10, allow_redirects=True)
                if resp.status_code == 200:
                    email = _extract_email_from_html(resp.text, target_name)
                    if email:
                        return email
            except Exception:
                continue

    return ""


def _search_lab_page_for_member(lab_url: str, target_name: str) -> str:
    """在实验室页面及其 members/people 子页面中搜索目标成员"""
    scraper = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }

    # 先搜主页面
    try:
        resp = scraper.get(lab_url, headers=headers, timeout=SCRAPE_TIMEOUT, allow_redirects=True)
        if resp.status_code == 200:
            email = _extract_email_from_html(resp.text, target_name)
            if email:
                return email

            # 从主页面提取 members/people/team 相关链接
            soup = BeautifulSoup(resp.text, 'html.parser')
            member_keywords = ['member', 'people', 'team', 'lab', 'group',
                              'student', 'personnel', 'staff', '成员', '团队']
            sub_urls = []
            for a_tag in soup.find_all('a', href=True):
                href_lower = a_tag.get('href', '').lower()
                text_lower = a_tag.get_text().lower()
                if any(kw in href_lower or kw in text_lower for kw in member_keywords):
                    full_url = urljoin(lab_url, a_tag['href'])
                    if full_url not in sub_urls:
                        sub_urls.append(full_url)

            # 搜索子页面（最多 3 个）
            for sub_url in sub_urls[:3]:
                try:
                    sub_resp = scraper.get(sub_url, headers=headers,
                                          timeout=SCRAPE_TIMEOUT, allow_redirects=True)
                    if sub_resp.status_code == 200:
                        email = _extract_email_from_html(sub_resp.text, target_name)
                        if email:
                            return email
                except Exception:
                    continue

    except Exception as e:
        print(f"    ⚠️ Lab 页面抓取异常: {e}")

    return ""
```

- [ ] **Step 7: 添加 LLM 导航模式函数（Layer 6）**

删除旧的 `_qwen_search_round1()`, `_qwen_search_round2_verify()`, `_qwen_search_round3_deep()` 三个函数，替换为:

```python
def _llm_navigate_and_scrape(name: str, org: str, doi: str,
                              paper_title: str = "",
                              cached_domain: str = "") -> dict:
    """
    Layer 6: LLM 导航模式。
    LLM 只返回 URL（不返回邮箱），我们自己抓取页面提取邮箱。
    """
    org_hint = f"，机构为 {org}" if org and org not in ["未提供", "未找到", "无", ""] else ""
    doi_hint = f"，其近期发表的论文DOI为 {doi}" if doi else ""
    title_hint = f"，论文标题为《{paper_title}》" if paper_title else ""

    prompt = f"""请帮我搜索以下学术研究者的**个人主页**、**实验室网页**、**大学通讯录页面**的URL链接：

姓名：{name}{org_hint}{doi_hint}{title_hint}

搜索策略：
1. 搜索 "{name}" + 机构名 + "homepage" 或 "lab" 查找个人/实验室页面
2. 搜索 Google Scholar 上的 "{name}" 个人主页
3. 搜索 ResearchGate / ORCID 上的 "{name}" 个人资料页面
4. 搜索该学者在大学院系通讯录（faculty directory / people）中的页面
5. 搜索 "{name}" + "contact" 查找联系方式页面

⚠️ 重要要求：
- 请只返回真实存在的网页URL链接
- **不要返回邮箱地址**，我只需要网页链接
- 优先返回 .edu / .ac / .org 域名的页面

请严格以以下 JSON 格式输出（不要输出任何其他内容）：
{{"urls": ["url1", "url2", "url3"], "homepage": "最可能的个人主页URL"}}"""

    try:
        raw = smart_generate_with_search(prompt)
        parsed = _parse_json_response(raw)

        urls_to_scrape = []

        # 提取 LLM 返回的 URL 列表
        if parsed.get("urls"):
            urls_to_scrape.extend(parsed["urls"])
        if parsed.get("homepage"):
            hp = parsed["homepage"]
            if hp not in urls_to_scrape:
                urls_to_scrape.insert(0, hp)

        # 过滤和验证 URL
        valid_urls = [u for u in urls_to_scrape
                      if isinstance(u, str) and u.startswith("http")]

        print(f"    🧭 LLM 返回了 {len(valid_urls)} 个 URL，开始逐个抓取...")

        # 逐个抓取 URL，提取邮箱
        result = {"email": "", "homepage": parsed.get("homepage", ""), "source_url": ""}

        for url in valid_urls[:5]:  # 最多尝试 5 个
            print(f"    📄 抓取: {url[:60]}...")
            try:
                email = _scrape_homepage(url, name)
                if email:
                    result["email"] = email
                    result["source_url"] = url
                    print(f"    ✅ 从 {url[:40]}... 提取到邮箱: {email}")
                    return result
            except Exception:
                continue

        return result

    except Exception as e:
        print(f"    ⚠️ LLM 导航搜索异常: {e}")
        return {"email": "", "homepage": "", "source_url": ""}
```

- [ ] **Step 8: 更新兼容旧接口**

保留 `find_email` 兼容函数，但更新调用:

```python
def find_email(name: str, org: str) -> dict:
    """兼容旧接口"""
    return find_email_for_paper("", name, org)
```

- [ ] **Step 9: 验证修改**

```bash
cd d:\Scholar_Agent
python -c "from backend.services.email_finder import find_email_for_paper; print('import OK')"
```

- [ ] **Step 10: Commit**

```bash
git add backend/services/email_finder.py
git commit -m "feat: rewrite email_finder with trust chain pipeline (V4.0)"
```

---

### Task 5: 修改 main.py — 先通讯后一作 + 传递新字段

**Files:**
- Modify: `backend/main.py`

**Context:** 当前 main.py 中通讯和一作的搜索是独立的。需要改为：通讯先搜 → 结果传给一作搜索。同时传递 ORCID 和 crossref_email 新字段。

- [ ] **Step 1: 修改通讯作者搜索调用（约第110-120行）**

当前代码:
```python
corr_email_data = {"邮箱": "未找到", "主页": "未找到", "谷歌学术": "未找到"}
if corr_name and corr_name != "未找到":
    corr_email_data = await asyncio.to_thread(
        find_email_for_paper, doi, corr_name, corr_org, "通讯",
        corr_homepage, title
    )
```

替换为:
```python
corr_email_data = {"邮箱": "未找到", "主页": "未找到", "来源": "none",
                   "来源URL": "", "置信度": "无", "置信分": 0}
if corr_name and corr_name != "未找到":
    corr_email_data = await asyncio.to_thread(
        find_email_for_paper, doi, corr_name, corr_org, "通讯",
        corr_homepage, title,
        corr_author.get("orcid", ""),            # 传入 ORCID
        corr_author.get("crossref_email", ""),    # 传入 Crossref 邮箱
        None                                      # 通讯没有 corr_result
    )
```

- [ ] **Step 2: 修改一作搜索调用（约第123-134行）**

当前代码:
```python
first_email_data = {"邮箱": "未找到", "主页": "未找到", "谷歌学术": "未找到"}
if first_name and first_name != "未找到":
    first_email_data = await asyncio.to_thread(
        find_email_for_paper, doi, first_name, first_org, "一作",
        first_homepage, title
    )
```

替换为:
```python
first_email_data = {"邮箱": "未找到", "主页": "未找到", "来源": "none",
                    "来源URL": "", "置信度": "无", "置信分": 0}
if first_name and first_name != "未找到":
    first_email_data = await asyncio.to_thread(
        find_email_for_paper, doi, first_name, first_org, "一作",
        first_homepage, title,
        first_author.get("orcid", ""),            # 传入 ORCID
        first_author.get("crossref_email", ""),    # 传入 Crossref 邮箱
        corr_email_data                            # 传入通讯结果！
    )
```

- [ ] **Step 3: 验证整体流水线可启动**

```bash
cd d:\Scholar_Agent
python -c "from backend.main import app; print('FastAPI app OK')"
```

- [ ] **Step 4: Commit**

```bash
git add backend/main.py
git commit -m "feat: pass ORCID/crossref_email/corr_result through pipeline"
```

---

## 验证计划

### 集成验证

完成所有 Task 后，运行完整流水线测试:

```bash
cd d:\Scholar_Agent
python -c "
from backend.services.doi_resolver import resolve_doi
from backend.services.author_extractor import extract_authors
from backend.services.email_finder import find_email_for_paper

# 测试 DOI
doi = '10.1016/j.cell.2026.01.018'
paper = resolve_doi(doi)
print(f'标题: {paper.get(\"title\", \"未知\")}')

authors = extract_authors(paper)
print(f'一作: {authors[\"第一作者\"]}')
print(f'通讯: {authors[\"通讯作者\"]}')

# 先搜通讯
corr = authors['通讯作者']
corr_result = find_email_for_paper(
    doi, corr['姓名'], corr['机构'], '通讯',
    corr.get('主页', ''), paper.get('title', ''),
    corr.get('orcid', ''), corr.get('crossref_email', '')
)
print(f'通讯邮箱: {corr_result}')

# 再搜一作（传入通讯结果）
first = authors['第一作者']
first_result = find_email_for_paper(
    doi, first['姓名'], first['机构'], '一作',
    first.get('主页', ''), paper.get('title', ''),
    first.get('orcid', ''), first.get('crossref_email', ''),
    corr_result  # 关键！传入通讯结果
)
print(f'一作邮箱: {first_result}')
"
```
