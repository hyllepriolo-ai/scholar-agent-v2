# 第一作者搜索轻量增强版 Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不引入浏览器级抓取依赖的前提下，完成第一作者邮箱搜索的第一阶段重构：简化 LLM 提示词输出、本地 URL 分类与筛选、变形邮箱识别、严格验收、全局防污染与特性开关。

**Architecture:** 第一作者搜索改为“LLM 返回候选证据页 -> 本地分类筛选 -> 轻量抓取页面文本 -> 本地提取标准/变形邮箱 -> 严格验收”的单通路。旧的 Layer 6 直搜邮箱兜底被删除，Layer 5 保留实验室成员页搜索，纯域名首页探测删除。浏览器级抓取不在本次计划内，只通过诊断数据为后续单独计划提供依据。

**Tech Stack:** Python 3.10, unittest, requests, cloudscraper, BeautifulSoup4, dnspython, OpenAI-compatible API (DashScope / Zhipu)

**Design Spec:** `docs/superpowers/specs/2026-04-09-first-author-llm-search-redesign-design.md`

---

## File Structure

### New Files
- `tests/test_email_finder_first_author.py` — 第一作者搜索 Phase 1 的单元回归测试，覆盖变形邮箱、候选页解析、URL 分类、防污染与新 Layer 6 流程

### Modified Files
- `backend/services/email_finder.py` — 核心改造文件：变形邮箱提取、本地 URL 分类、简化 Prompt、第一作者新流程、删除旧兜底
- `backend/config.py` — 新增第一作者重构特性开关和时间预算配置
- `test_batch.py` — 增加第一作者来源与来源 URL 的批量诊断输出

### Explicitly Not In Scope For This Plan
- `Playwright` / `Selenium` / Chromium 依赖
- Dockerfile / Render 运行时调整
- 浏览器级抓取实现

---

### Task 1: 建立第一作者 Phase 1 回归测试骨架

**Files:**
- Create: `tests/test_email_finder_first_author.py`
- Modify: `backend/services/email_finder.py`

- [ ] **Step 1: 写出变形邮箱与 URL 分类的失败测试**

创建 `tests/test_email_finder_first_author.py`，先写最小测试骨架和 4 个失败用例：

```python
import unittest
from backend.services import email_finder


class ObfuscatedEmailTests(unittest.TestCase):
    def test_extracts_square_bracket_at_dot_email(self):
        html = "<div>Alice Zhang alice.zhang [at] example [dot] edu</div>"
        self.assertEqual(
            email_finder._extract_email_from_html(html, "Alice Zhang"),
            "alice.zhang@example.edu",
        )

    def test_extracts_plain_at_dot_email(self):
        html = "<div>Contact: alicez at example dot edu</div>"
        self.assertEqual(
            email_finder._extract_email_from_html(html, "Alice Zhang"),
            "alicez@example.edu",
        )


class CandidatePageHelperTests(unittest.TestCase):
    def test_classifies_faculty_profile_url(self):
        info = email_finder._classify_candidate_url(
            "https://medicine.example.edu/faculty/alice-zhang",
            "example.edu",
        )
        self.assertEqual(info["page_type"], "faculty_page")
        self.assertTrue(info["is_official_domain"])

    def test_filters_search_results_page(self):
        candidates = [
            {"url": "https://www.google.com/search?q=alice+zhang", "why_relevant": "搜索结果"},
            {"url": "https://medicine.example.edu/faculty/alice-zhang", "why_relevant": "官网 faculty 页面"},
        ]
        filtered = email_finder._filter_candidate_pages(candidates, "example.edu")
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["url"], "https://medicine.example.edu/faculty/alice-zhang")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试，确认当前实现确实失败**

Run:
```bash
cd D:\Scholar_Agent
python -m unittest discover -s tests -p "test_email_finder_first_author.py" -v
```

Expected:
- `AttributeError: module 'backend.services.email_finder' has no attribute '_classify_candidate_url'`
- 变形邮箱提取测试失败，因为当前实现只识别标准 `@`

- [ ] **Step 3: 在 `email_finder.py` 中添加变形邮箱模式和 URL 分类函数**

在 `backend/services/email_finder.py` 中添加下列代码块，放在 `EMAIL_PATTERN` 和 `_URL_BLACKLIST` 常量附近：

```python
OBFUSCATED_EMAIL_PATTERNS = [
    re.compile(r'([a-zA-Z0-9._%+-]+)\s*\[\s*at\s*\]\s*([a-zA-Z0-9.-]+)\s*\[\s*dot\s*\]\s*([a-zA-Z]{2,})', re.IGNORECASE),
    re.compile(r'([a-zA-Z0-9._%+-]+)\s*\(\s*at\s*\)\s*([a-zA-Z0-9.-]+)\s*\(\s*dot\s*\)\s*([a-zA-Z]{2,})', re.IGNORECASE),
    re.compile(r'([a-zA-Z0-9._%+-]+)\s+at\s+([a-zA-Z0-9.-]+)\s+dot\s+([a-zA-Z]{2,})', re.IGNORECASE),
    re.compile(r'([a-zA-Z0-9._%+-]+)\s*\{\s*at\s*\}\s*([a-zA-Z0-9.-]+)\s*\{\s*dot\s*\}\s*([a-zA-Z]{2,})', re.IGNORECASE),
]


def _extract_obfuscated_emails(page_text: str) -> list[str]:
    emails = []
    for pattern in OBFUSCATED_EMAIL_PATTERNS:
        for local_part, domain_main, suffix in pattern.findall(page_text or ""):
            email = f"{local_part}@{domain_main}.{suffix}".lower()
            if EMAIL_PATTERN.match(email) and not _is_noise_email(email):
                emails.append(email)
    return list(dict.fromkeys(emails))


def _classify_candidate_url(url: str, cached_domain: str = "") -> dict:
    url_lower = (url or "").lower()
    is_official_domain = any(token in url_lower for token in [".edu", ".ac.", ".gov", cached_domain.lower()]) if cached_domain else any(token in url_lower for token in [".edu", ".ac.", ".gov"])

    page_type = "other"
    if any(token in url_lower for token in ["/faculty/", "/staff/", "/directory/", "/profile/"]):
        page_type = "faculty_page"
    elif any(token in url_lower for token in ["/people/", "/members/", "/member/", "/team/", "/group/", "/lab/"]):
        page_type = "lab_member"
    elif "orcid.org" in url_lower:
        page_type = "orcid"
    elif "scholar.google" in url_lower:
        page_type = "scholar"
    elif "researchgate.net" in url_lower:
        page_type = "researchgate"

    return {
        "url": url,
        "page_type": page_type,
        "is_official_domain": is_official_domain,
    }


def _filter_candidate_pages(candidates: list, cached_domain: str = "") -> list:
    filtered = []
    seen = set()
    for item in candidates or []:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        normalized = url.rstrip('.,;:)]\'">').lower()
        if normalized in seen:
            continue
        if any(bk in normalized for bk in _URL_BLACKLIST):
            continue
        if normalized.endswith(".pdf"):
            continue
        meta = _classify_candidate_url(url, cached_domain)
        if meta["page_type"] == "other" and not meta["is_official_domain"]:
            continue
        seen.add(normalized)
        filtered.append({**item, **meta})
    return filtered
```

- [ ] **Step 4: 把变形邮箱提取接到 `_extract_email_from_html()`**

在 `_extract_email_from_html()` 的“策略 C: 全文 + 名字匹配”之前插入：

```python
    obfuscated_emails = _extract_obfuscated_emails(page_text)
    if obfuscated_emails:
        best = _match_best_email(obfuscated_emails, target_name)
        if best:
            return best
```

- [ ] **Step 5: 重新运行测试，确认 4 个辅助用例通过**

Run:
```bash
cd D:\Scholar_Agent
python -m unittest tests.test_email_finder_first_author -v
```

Expected:
- 4 个测试全部 `ok`

- [ ] **Step 6: Commit**

```bash
git add tests/test_email_finder_first_author.py backend/services/email_finder.py
git commit -m "test: cover obfuscated emails and candidate URL helpers"
```

---

### Task 2: 简化 Prompt 1 并添加候选页 JSON 解析

**Files:**
- Modify: `tests/test_email_finder_first_author.py`
- Modify: `backend/services/email_finder.py`

- [ ] **Step 1: 为 Prompt 构造与候选页 JSON 解析写失败测试**

在 `tests/test_email_finder_first_author.py` 追加：

```python
class CandidatePromptAndParsingTests(unittest.TestCase):
    def test_build_prompt_mentions_required_context(self):
        prompt = email_finder._build_first_author_candidate_prompt(
            name="Alice Zhang",
            org="Example University",
            doi="10.1000/test",
            paper_title="Aging rewires immunity",
            cached_domain="example.edu",
        )
        self.assertIn("Alice Zhang", prompt)
        self.assertIn("Example University", prompt)
        self.assertIn("Aging rewires immunity", prompt)
        self.assertIn("10.1000/test", prompt)
        self.assertIn("@example.edu", prompt)
        self.assertNotIn("邮箱: xxx@xxx.edu", prompt)

    def test_parse_candidate_pages_from_wrapped_json(self):
        raw = '候选如下：{"candidates":[{"url":"https://example.edu/faculty/alice","why_relevant":"官网 faculty 页面"}]}'
        parsed = email_finder._parse_candidate_pages(raw)
        self.assertEqual(parsed, [{"url": "https://example.edu/faculty/alice", "why_relevant": "官网 faculty 页面"}])
```

- [ ] **Step 2: 运行测试，确认 Prompt 和解析函数尚不存在**

Run:
```bash
cd D:\Scholar_Agent
python -m unittest discover -s tests -p "test_email_finder_first_author.py" -v
```

Expected:
- `AttributeError`，提示 `_build_first_author_candidate_prompt` 或 `_parse_candidate_pages` 不存在

- [ ] **Step 3: 实现简化 Prompt 与 JSON 解析函数**

在 `backend/services/email_finder.py` 的 Layer 6 辅助函数区域添加：

```python
def _build_first_author_candidate_prompt(name: str, org: str, doi: str,
                                         paper_title: str, cached_domain: str) -> str:
    return f"""你是学术作者联系信息检索助手。你的任务是联网搜索“目标第一作者”最可能出现联系方式的页面，并返回结构化候选结果。

【目标作者信息】
- 姓名：{name}
- 机构：{org}
- 论文标题：{paper_title}
- DOI：{doi}
- 已知通讯作者邮箱域名：@{cached_domain}

【任务目标】
请搜索与目标作者 {name} 最相关、最可能出现联系方式的页面。
重点找“目标作者本人”的人员页、成员页、目录页、个人页。
不要把普通论文页、搜索结果页、新闻页、聚合页当成优先结果。

【输出要求】
只返回纯 JSON，不要解释，不要 markdown 代码块。

{{
  "candidates": [
    {{
      "url": "https://...",
      "why_relevant": "一句话说明为什么这页像是目标作者本人页面"
    }}
  ]
}}

最多返回 5 个候选页。
如果没有高相关结果，返回：
{{"candidates":[]}}"""


def _parse_candidate_pages(raw: str) -> list[dict]:
    parsed = _parse_json_response(raw)
    candidates = parsed.get("candidates", []) if isinstance(parsed, dict) else []
    results = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        url = (item.get("url") or "").strip()
        why = (item.get("why_relevant") or "").strip()
        if url:
            results.append({"url": url, "why_relevant": why})
    return results
```

- [ ] **Step 4: 重新运行解析测试**

Run:
```bash
cd D:\Scholar_Agent
python -m unittest discover -s tests -p "test_email_finder_first_author.py" -v
```

Expected:
- 2 个测试全部 `ok`

- [ ] **Step 5: Commit**

```bash
git add tests/test_email_finder_first_author.py backend/services/email_finder.py
git commit -m "feat: simplify first-author candidate prompt and parsing"
```

---

### Task 3: 删除 Layer 5 根域名探测并加入全局防污染辅助函数

**Files:**
- Modify: `tests/test_email_finder_first_author.py`
- Modify: `backend/services/email_finder.py`

- [ ] **Step 1: 为 Layer 5 删除策略 B 和防污染辅助函数写失败测试**

在 `tests/test_email_finder_first_author.py` 追加：

```python
from unittest.mock import patch


class FirstAuthorGuardTests(unittest.TestCase):
    @patch("backend.services.email_finder.cloudscraper.create_scraper")
    def test_coaffiliation_search_no_longer_probes_root_domain(self, mock_scraper):
        corr_result = {"邮箱": "mentor@example.edu", "主页": "", "来源URL": ""}
        result = email_finder._try_coaffiliation_search("Alice Zhang", corr_result, "")
        self.assertEqual(result, "")
        mock_scraper.assert_not_called()

    def test_rejects_email_equal_to_corresponding_author(self):
        corr_result = {"邮箱": "mentor@example.edu"}
        self.assertTrue(email_finder._is_cross_contaminated_email("mentor@example.edu", corr_result))
        self.assertFalse(email_finder._is_cross_contaminated_email("alice@example.edu", corr_result))
```

- [ ] **Step 2: 运行测试，确认新防污染函数尚不存在**

Run:
```bash
cd D:\Scholar_Agent
python -m unittest discover -s tests -p "test_email_finder_first_author.py" -v
```

Expected:
- `_is_cross_contaminated_email` 缺失导致失败

- [ ] **Step 3: 在 `email_finder.py` 中移除 Layer 5 的根域名策略并加入防污染函数**

在 `backend/services/email_finder.py` 中添加辅助函数：

```python
def _is_cross_contaminated_email(email: str, corr_result: dict | None) -> bool:
    if not email or not corr_result:
        return False
    corr_email = (corr_result.get("邮箱") or "").strip().lower()
    return bool(corr_email) and email.strip().lower() == corr_email
```

然后把 `_try_coaffiliation_search()` 中“策略 B: 如果知道机构域名，尝试在机构网站搜索”整段删除，只保留：

```python
    if corr_homepage and corr_homepage.startswith("http"):
        lab_email = _search_lab_page_for_member(corr_homepage, target_name)
        if lab_email:
            return lab_email

    return ""
```

- [ ] **Step 4: 在所有一作接受邮箱的路径上调用防污染检查**

把 `find_email_for_paper()` 中接受 Layer 3、Layer 5、Layer 6 结果的地方，统一加上：

```python
            if _is_cross_contaminated_email(candidate_email, corr_result):
                print(f"  ⚠️ 命中邮箱 {candidate_email} 与通讯作者邮箱相同，拒绝作为一作结果")
```

Layer 6 的最终验收必须先过这层检查，再进入 `_score_email()`。

- [ ] **Step 5: 重跑防污染测试**

Run:
```bash
cd D:\Scholar_Agent
python -m unittest discover -s tests -p "test_email_finder_first_author.py" -v
```

Expected:
- 2 个测试全部 `ok`

- [ ] **Step 6: Commit**

```bash
git add tests/test_email_finder_first_author.py backend/services/email_finder.py
git commit -m "refactor: remove root-domain probing and add cross-contamination guard"
```

---

### Task 4: 增加特性开关并切换到第一作者新 Layer 6 流程

**Files:**
- Modify: `tests/test_email_finder_first_author.py`
- Modify: `backend/config.py`
- Modify: `backend/services/email_finder.py`

- [ ] **Step 1: 为新 Layer 6 主流程写失败测试**

在 `tests/test_email_finder_first_author.py` 追加：

```python
from unittest.mock import patch


class FirstAuthorLayer6FlowTests(unittest.TestCase):
    @patch.object(email_finder, "_verify_email_mx", return_value=True)
    @patch.object(email_finder, "smart_generate_with_search")
    @patch.object(email_finder, "_fetch_page_text_lightweight")
    def test_first_author_returns_email_from_candidate_page(self, mock_fetch, mock_search, _mock_mx):
        mock_search.return_value = '{"candidates":[{"url":"https://medicine.example.edu/faculty/alice-zhang","why_relevant":"官网 faculty 页面"}]}'
        mock_fetch.return_value = "Alice Zhang Example University alice.zhang@example.edu"

        result = email_finder.find_email_for_paper(
            doi="10.1000/test",
            name="Alice Zhang",
            org="Example University",
            role="一作",
            homepage="",
            paper_title="Aging rewires immunity",
            orcid="",
            crossref_email="",
            corr_result={"邮箱": "mentor@example.edu", "主页": "未找到", "来源URL": ""},
        )

        self.assertEqual(result["邮箱"], "alice.zhang@example.edu")
        self.assertEqual(result["来源"], "llm_evidence_page")

    @patch.object(email_finder, "_verify_email_mx", return_value=True)
    @patch.object(email_finder, "smart_generate_with_search", return_value='{"candidates":[]}')
    @patch.object(email_finder, "_fetch_page_text_lightweight")
    def test_first_author_fast_fails_when_no_candidates(self, mock_fetch, _mock_search, _mock_mx):
        result = email_finder.find_email_for_paper(
            doi="10.1000/test",
            name="Alice Zhang",
            org="Example University",
            role="一作",
            homepage="",
            paper_title="Aging rewires immunity",
            orcid="",
            crossref_email="",
            corr_result={"邮箱": "mentor@example.edu", "主页": "未找到", "来源URL": ""},
        )

        self.assertEqual(result["邮箱"], "未找到")
        mock_fetch.assert_not_called()
```

- [ ] **Step 2: 运行测试，确认 `_fetch_page_text_lightweight` 和新来源尚不存在**

Run:
```bash
cd D:\Scholar_Agent
python -m unittest discover -s tests -p "test_email_finder_first_author.py" -v
```

Expected:
- `AttributeError`，提示 `_fetch_page_text_lightweight` 不存在

- [ ] **Step 3: 在 `backend/config.py` 中加入特性开关和预算配置**

在 `backend/config.py` 中 `API_RATE_LIMIT_DELAY` 之后添加：

```python
USE_NEW_FIRST_AUTHOR_SEARCH = os.environ.get("USE_NEW_FIRST_AUTHOR_SEARCH", "1") == "1"
FIRST_AUTHOR_SEARCH_BUDGET_SECONDS = int(os.environ.get("FIRST_AUTHOR_SEARCH_BUDGET_SECONDS", "210"))
FIRST_AUTHOR_MAX_CANDIDATES = int(os.environ.get("FIRST_AUTHOR_MAX_CANDIDATES", "5"))
```

并在 `email_finder.py` 的 import 中补上：

```python
from backend.config import (
    smart_generate, smart_generate_with_search,
    HTTP_TIMEOUT, SCRAPE_TIMEOUT, API_RATE_LIMIT_DELAY,
    USE_NEW_FIRST_AUTHOR_SEARCH, FIRST_AUTHOR_SEARCH_BUDGET_SECONDS, FIRST_AUTHOR_MAX_CANDIDATES
)
```

- [ ] **Step 4: 在 `email_finder.py` 中实现第一作者新搜索主流程**

在 Layer 6 附近新增 3 个函数：

```python
def _fetch_page_text_lightweight(url: str) -> str:
    try:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True},
            ssl_context=ctx
        )
        resp = scraper.get(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/xhtml+xml"},
            timeout=SCRAPE_TIMEOUT,
            allow_redirects=True
        )
        return resp.text if resp.status_code == 200 else ""
    except Exception:
        return ""


def _page_matches_target(page_text: str, target_name: str, target_org: str) -> bool:
    text_lower = (page_text or "").lower()
    name_parts = [part for part in re.split(r'[\s.\-]+', target_name.lower()) if len(part) >= 2]
    org_parts = [part for part in re.split(r'[\s,;()\-]+', target_org.lower()) if len(part) >= 4]
    has_name = sum(1 for part in name_parts if part in text_lower) >= max(1, min(2, len(name_parts)))
    has_org = any(part in text_lower for part in org_parts[:8]) if org_parts else True
    return has_name and has_org


def _search_first_author_via_llm_evidence(name: str, org: str, doi: str, paper_title: str,
                                          cached_domain: str, corr_result: dict | None) -> dict:
    prompt = _build_first_author_candidate_prompt(name, org, doi, paper_title, cached_domain)
    raw = smart_generate_with_search(prompt)
    candidates = _filter_candidate_pages(_parse_candidate_pages(raw), cached_domain)[:FIRST_AUTHOR_MAX_CANDIDATES]
    if not candidates:
        return {"email": "", "source_url": "", "homepage": ""}

    deadline = time.time() + FIRST_AUTHOR_SEARCH_BUDGET_SECONDS
    for candidate in candidates:
        if time.time() >= deadline:
            break
        page_text = _fetch_page_text_lightweight(candidate["url"])
        if not page_text or not _page_matches_target(page_text, name, org):
            continue
        email = _extract_email_from_html(page_text, name)
        if not email:
            continue
        if _is_cross_contaminated_email(email, corr_result):
            continue
        return {
            "email": email,
            "source_url": candidate["url"],
            "homepage": candidate["url"],
        }

    return {"email": "", "source_url": "", "homepage": ""}
```

然后在 `find_email_for_paper()` 的 Layer 6 入口前，把一作新逻辑插到旧 Layer 6 前面：

```python
    if role in ["一作", "first_author"] and USE_NEW_FIRST_AUTHOR_SEARCH:
        print("  🧭 [Layer6] 第一作者新证据页搜索...")
        new_result = _search_first_author_via_llm_evidence(
            name, org, doi, paper_title, _get_cached_domain(org), corr_result
        )
        if new_result.get("email"):
            email = new_result["email"]
            score = _score_email(email, 6, name, org, all_candidates)
            if score >= 50 and _verify_email_mx(email):
                print(f"  ✅ [Layer6 Phase1] 命中: {email} (得分: {score})")
                return _build_result(email, new_result["source_url"], "llm_evidence_page", score)
```

- [ ] **Step 5: 删除第一作者的旧 Phase2 直搜兜底**

在 `_llm_navigate_and_scrape()` 中，删除“Phase 2: 直搜兜底 —— 强反幻觉 Prompt + 来源回抓验证”整段逻辑，并把函数定位收缩为通讯作者兼容导航函数。

新的结束逻辑保留：

```python
    if phase1_email:
        return {"email": phase1_email, "homepage": phase1_source, "source_url": phase1_source}

    return {"email": "", "homepage": "", "source_url": ""}
```

- [ ] **Step 6: 运行新 Layer 6 流程测试**

Run:
```bash
cd D:\Scholar_Agent
python -m unittest discover -s tests -p "test_email_finder_first_author.py" -v
```

Expected:
- 2 个测试全部 `ok`

- [ ] **Step 7: Commit**

```bash
git add tests/test_email_finder_first_author.py backend/config.py backend/services/email_finder.py
git commit -m "feat: add feature-flagged first-author evidence-page search"
```

---

### Task 5: 补齐批量诊断输出，为后续浏览器抓取决策留数据

**Files:**
- Modify: `test_batch.py`

- [ ] **Step 1: 让批量脚本输出第一作者来源与来源 URL**

在 `test_batch.py` 中追加来源字段记录。把 `results.append({...})` 改成：

```python
    results.append({
        'doi': doi,
        'title': title[:50],
        'corr_name': ln, 'corr_email': ce, 'corr_hit': ch, 'corr_conf': cc,
        'first_name': fn, 'first_email': fe, 'first_hit': fhit, 'first_conf': fc,
        'first_source': fr.get('来源', 'none'),
        'first_source_url': fr.get('来源URL', ''),
    })
```

并把最终汇总输出改成：

```python
for r in results:
    print(
        f"  {r['doi'][-10:]} | "
        f"通讯: {r['corr_hit']}({r['corr_conf']}) {r['corr_email'][:30]} | "
        f"一作: {r['first_hit']}({r['first_conf']}) {r['first_email'][:30]} | "
        f"来源: {r['first_source']} | URL: {r['first_source_url'][:60]}"
    )
```

- [ ] **Step 2: 运行批量脚本做一次 smoke test**

Run:
```bash
cd D:\Scholar_Agent
python test_batch.py
```

Expected:
- 脚本完整跑完
- 每条一作结果都打印 `来源` 和 `URL`
- 不出现 `KeyError: 'first_source'`

- [ ] **Step 3: Commit**

```bash
git add test_batch.py
git commit -m "chore: print first-author source diagnostics in batch runner"
```

---

### Task 6: 运行完整回归并确认 Phase 1 可上线

**Files:**
- Test: `tests/test_email_finder_first_author.py`
- Test: `test_batch.py`

- [ ] **Step 1: 运行单元测试全集**

Run:
```bash
cd D:\Scholar_Agent
python -m unittest discover -s tests -p "test_email_finder_first_author.py" -v
```

Expected:
- 全部测试 `ok`

- [ ] **Step 2: 运行现有批量脚本，记录命中率与来源分布**

Run:
```bash
cd D:\Scholar_Agent
python test_batch.py > first_author_phase1.log
```

Expected:
- 生成 `first_author_phase1.log`
- 日志中能看到一作的 `来源` 与 `来源URL`

- [ ] **Step 3: 检查是否满足进入浏览器抓取后续计划的条件**

人工检查 `first_author_phase1.log`，只要出现以下任一模式，就单独开第二阶段计划：

```text
1. 候选页 URL 明显高质量，但轻量抓取拿不到有效文本
2. 高频出现 JS-only / 空白页面
3. 主要失败原因已经从“搜不到好页面”变成“拿不到页面内容”
```

- [ ] **Step 4: 切换默认开关并准备上线**

确认 `backend/config.py` 中默认开关保持：

```python
USE_NEW_FIRST_AUTHOR_SEARCH = os.environ.get("USE_NEW_FIRST_AUTHOR_SEARCH", "1") == "1"
```

如果批量结果不满意，临时回滚方式为：

```bash
set USE_NEW_FIRST_AUTHOR_SEARCH=0
python test_batch.py
```

- [ ] **Step 5: Commit**

```bash
git add tests/test_email_finder_first_author.py backend/config.py backend/services/email_finder.py test_batch.py
git commit -m "test: validate first-author phase1 rollout end to end"
```

---

## Self-Review

### Spec Coverage

- 变形邮箱识别：Task 1
- Prompt 1 简化：Task 2
- 本地 URL 分类与筛选：Task 1 + Task 2
- Layer 5 去留明确：Task 3
- 全局防污染：Task 3 + Task 4
- 第一作者新 Layer 6 流程：Task 4
- Feature flag：Task 4 + Task 6
- 浏览器抓取按需增强的数据前置：Task 5 + Task 6

### Placeholder Scan

本计划没有 `TODO`、`TBD`、`稍后实现` 这类占位步骤。浏览器抓取被明确排除出本计划范围，并通过诊断数据作为后续独立计划的输入，而不是留在本计划中半实现。

### Type Consistency

- 新增 helper 名称在全计划中保持一致：
  - `_extract_obfuscated_emails`
  - `_classify_candidate_url`
  - `_filter_candidate_pages`
  - `_build_first_author_candidate_prompt`
  - `_parse_candidate_pages`
  - `_fetch_page_text_lightweight`
  - `_page_matches_target`
  - `_search_first_author_via_llm_evidence`
  - `_is_cross_contaminated_email`

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-09-first-author-search-phase1-rollout.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
