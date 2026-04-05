"""
邮箱查找器 V2 —— 六级穿透策略定位作者邮箱。
重大升级：新增论文原始页面抓取（命中率最高）、ORCID 查询、多轮搜索引擎。

防线0 [新增]: DOI 论文原始页面抓取（出版商页面几乎100%有通讯邮箱）
防线1: Semantic Scholar 直查作者主页
防线2: Google Scholar 获取认证邮箱域（带反封锁）
防线3 [增强]: 多轮搜索引擎（DuckDuckGo 多组关键词）
防线4 [新增]: ORCID 查询
防线5 [新增]: 目标主页深度抓取 + 子页面穿透 + LLM 提取
"""
import re
import json
import time
import requests
import concurrent.futures
import cloudscraper
from bs4 import BeautifulSoup
from urllib.parse import quote, urljoin
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

from backend.config import smart_generate, HTTP_TIMEOUT, API_RATE_LIMIT_DELAY, SCRAPE_TIMEOUT

# 邮箱正则校验
EMAIL_PATTERN = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')

# 噪音邮箱过滤集
NOISE_EMAILS = {'noreply', 'admin', 'info@', 'support', 'webmaster', 'example',
                'privacy', 'contact@', 'help@', 'feedback', 'editor', 'editorial',
                'office@', 'journal', 'press', 'submission', 'subscribe'}


def find_email_for_paper(doi: str, name: str, org: str, role: str = "通讯") -> dict:
    """
    最强入口：结合 DOI（论文原始页面）+ 作者信息，全力搜索邮箱。
    相比 find_email()，多了对 DOI 论文页面的直接抓取。
    """
    invalid_tags = ["未提供", "未找到", "无", "none", "unknown", ""]
    if not name or name.lower().strip() in invalid_tags:
        return {"邮箱": "未找到", "主页": "未找到", "谷歌学术": "未找到", "来源": "none"}
    
    print(f"\n  📧 [邮箱搜索V2] 开始追踪: {name} ({org}) [角色: {role}]")
    
    found_email = ""
    target_url = ""
    email_domain = ""
    scholar_profile = ""
    
    # ============================================================
    # 防线 0 [核弹级]: 直接抓取论文出版商页面（命中率最高！）
    # Nature/Cell/Science 等出版商页面几乎 100% 标注通讯邮箱
    # ============================================================
    if doi:
        print(f"  🎯 [防线0] 抓取论文原始页面 (doi.org/{doi})...")
        paper_email = _scrape_paper_page(doi, name)
        if paper_email:
            print(f"  ✅ [防线0] 论文页面直接命中: {paper_email}")
            return {
                "邮箱": paper_email,
                "主页": f"https://doi.org/{doi}",
                "谷歌学术": "未找到",
                "来源": "paper_page"
            }
    
    # ============================================================
    # 防线 1: Semantic Scholar 直查作者主页
    # ============================================================
    s2_data = _search_s2_author(name)
    if s2_data:
        if s2_data.get("homepage"):
            target_url = s2_data["homepage"]
            print(f"  ✅ [防线1] S2 找到作者主页: {target_url}")
        if s2_data.get("affiliations") and (not org or org in invalid_tags):
            org = " ".join(s2_data["affiliations"])
    
    # ============================================================
    # 防线 2: Google Scholar 获取认证邮箱域
    # ============================================================
    gs_data = _search_google_scholar(name, org)
    if gs_data and gs_data != "BLOCKED":
        email_domain = gs_data.get("email_domain", "")
        scholar_profile = gs_data.get("profile_url", "")
        if email_domain:
            print(f"  ✅ [防线2] Google Scholar 认证邮箱域: {email_domain}")
    
    # ============================================================
    # 防线 3 [增强]: 多轮搜索引擎（3 组不同关键词）
    # ============================================================
    if not target_url:
        print(f"  🔍 [防线3] 多轮搜索引擎启动...")
        target_url = _multi_round_web_search(name, org)
    
    # ============================================================
    # 防线 4 [新增]: ORCID 查询
    # ============================================================
    if not found_email:
        orcid_result = _search_orcid(name, org)
        if orcid_result:
            if orcid_result.get("email"):
                found_email = orcid_result["email"]
                print(f"  ✅ [防线4] ORCID 直接拿到邮箱: {found_email}")
            if not target_url and orcid_result.get("homepage"):
                target_url = orcid_result["homepage"]
                print(f"  ✅ [防线4] ORCID 拿到主页: {target_url}")
    
    # ============================================================
    # 防线 5: 深度抓取目标主页 + 子页面穿透 + LLM 提取
    # ============================================================
    if not found_email and target_url:
        print(f"  🕸️ [防线5] 深度抓取目标页面: {target_url}")
        page_text = _fetch_page_deep(target_url, find_team=True)
        if page_text:
            found_email = _regex_find_email(page_text, name, email_domain)
            if not found_email:
                found_email = _llm_extract_email(page_text, name, email_domain)
    
    # 如果有邮箱域但没找到完整邮箱，尝试构建猜测邮箱
    if not found_email and email_domain:
        found_email = _guess_email(name, email_domain)
    
    source = "none"
    if found_email:
        if target_url:
            source = "web_scrape"
        elif email_domain:
            source = "google_scholar"
        else:
            source = "orcid"
    elif s2_data and s2_data.get("homepage"):
        source = "semantic_scholar"
    
    result = {
        "邮箱": found_email if found_email else "未找到",
        "主页": target_url if target_url else "未找到",
        "谷歌学术": scholar_profile if scholar_profile else "未找到",
        "来源": source
    }
    print(f"  📧 最终结果: {result['邮箱']} (来源: {result['来源']})")
    return result


# 保留旧接口兼容
def find_email(name: str, org: str) -> dict:
    """兼容旧接口——不带 DOI 的邮箱搜索"""
    return find_email_for_paper("", name, org)


# ================================================================
# 防线 0: 论文出版商页面直接抓取（核心新增！）
# ================================================================
def _scrape_paper_page(doi: str, target_name: str) -> str:
    """
    直接通过 DOI 访问论文出版商页面（Nature/Cell/Science/Wiley/Elsevier等），
    从页面中提取通讯作者邮箱。这是命中率最高的方式！
    
    增强策略：
    - Elsevier: linkinghub跳板页 → 提取真实ScienceDirect URL再抓
    - Science: 403时尝试备用路径
    - Wiley: 直接构建 onlinelibrary URL
    """
    # 构建多个候选 URL（不同出版商路径）
    urls_to_try = [f"https://doi.org/{doi}"]
    
    # 常见出版商的直接 URL 模板
    if "10.1016/" in doi:  # Elsevier / Cell Press
        pii_candidates = _doi_to_elsevier_urls(doi)
        urls_to_try.extend(pii_candidates)
    elif "10.1126/" in doi:  # Science/AAAS
        slug = doi.split("/")[-1]
        urls_to_try.append(f"https://www.science.org/doi/full/{doi}")
        urls_to_try.append(f"https://www.science.org/doi/abs/{doi}")
    
    scraper = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://scholar.google.com/",
    }
    
    all_page_texts = []  # 收集所有成功的页面内容
    
    for url in urls_to_try:
        try:
            resp = scraper.get(url, headers=headers, timeout=SCRAPE_TIMEOUT, allow_redirects=True)
            if resp.status_code != 200:
                print(f"    ⚠️ DOI 页面返回 {resp.status_code}: {url}")
                continue
            
            final_url = resp.url
            print(f"    📄 DOI 重定向到: {final_url}")
            
            page_text = resp.text
            
            # Elsevier 跳板页处理：linkinghub 页面只是重定向
            if 'linkinghub.elsevier.com' in final_url:
                real_url = _extract_elsevier_redirect(page_text, final_url)
                if real_url:
                    print(f"    🔗 Elsevier 跳板页 → 跟踪到: {real_url}")
                    try:
                        resp2 = scraper.get(real_url, headers=headers, timeout=SCRAPE_TIMEOUT, allow_redirects=True)
                        if resp2.status_code == 200:
                            page_text = resp2.text
                            final_url = resp2.url
                            print(f"    📄 最终页面: {final_url}")
                    except Exception as e2:
                        print(f"    ⚠️ 跟踪 Elsevier 跳板失败: {e2}")
            
            # 解析页面提取邮箱
            email = _extract_email_from_html(page_text, target_name)
            if email:
                return email
            
            all_page_texts.append(page_text)
                        
        except Exception as e:
            print(f"    ⚠️ 论文页面抓取异常: {e}")
            continue
    
    # 如果所有页面都没有直接找到邮箱，做一次联合分析
    if all_page_texts:
        combined = "\n".join(all_page_texts)
        email = _extract_email_from_html(combined, target_name)
        if email:
            return email
    
    return ""


def _doi_to_elsevier_urls(doi: str) -> list:
    """为 Elsevier DOI 构建直接的 ScienceDirect URL"""
    urls = []
    # ScienceDirect 使用 PII，可以尝试从 Crossref 获取
    try:
        cr_url = f"https://api.crossref.org/works/{doi}"
        r = requests.get(cr_url, timeout=8)
        if r.status_code == 200:
            msg = r.json().get("message", {})
            # 尝试从 link 字段获取全文链接
            links = msg.get("link", [])
            for link in links:
                url = link.get("URL", "")
                if "sciencedirect" in url or "elsevier" in url:
                    urls.append(url)
            # 尝试从 URL 字段获取
            resource_url = msg.get("URL", "")
            if resource_url:
                urls.append(resource_url)
    except Exception:
        pass
    return urls


def _extract_elsevier_redirect(html_text: str, current_url: str) -> str:
    """从 Elsevier linkinghub 跳板页提取真实的 ScienceDirect URL"""
    soup = BeautifulSoup(html_text, 'html.parser')
    
    # 方法1：查找 meta refresh 重定向
    meta = soup.find('meta', attrs={'http-equiv': re.compile(r'refresh', re.I)})
    if meta:
        content = meta.get('content', '')
        # format is generally "0; url='/retrieve...'"
        match = re.search(r'url=([^;]+)', content, re.I)
        if match:
            redirect_url = match.group(1).strip().strip('\'"')
            if redirect_url.startswith('/'):
                return urljoin("https://linkinghub.elsevier.com", redirect_url)
            return redirect_url
    
    # 方法2：查找页面中的 ScienceDirect 链接
    for a in soup.find_all('a', href=True):
        href = a['href']
        if 'sciencedirect.com' in href:
            return href
    
    # 方法3：从 URL 重构 ScienceDirect 路径
    if 'pii/' in current_url:
        pii = current_url.split('pii/')[-1]
        return f"https://www.sciencedirect.com/science/article/pii/{pii}"
    elif 'pii/S' in html_text:
        match = re.search(r'pii/(S\w+)', html_text)
        if match:
            return f"https://www.sciencedirect.com/science/article/pii/{match.group(1)}"
    
    return ""


def _extract_email_from_html(page_text: str, target_name: str) -> str:
    """从 HTML 页面中提取邮箱——统一的三策略抽取"""
    soup = BeautifulSoup(page_text, 'html.parser')
    
    # 策略A: 从 mailto: 链接中提取邮箱
    mailto_emails = []
    for a_tag in soup.find_all('a', href=True):
        href = a_tag.get('href', '')
        if href.startswith('mailto:'):
            email = href.replace('mailto:', '').split('?')[0].strip()
            if EMAIL_PATTERN.match(email):
                mailto_emails.append(email)
    
    if mailto_emails:
        result = _match_best_email(mailto_emails, target_name, "")
        if result:
            return result
        academic_domains = ['.edu', '.ac.', '.org', '.gov']
        for em in mailto_emails:
            if any(d in em.lower() for d in academic_domains):
                return em
    
    # 策略B: 从"corresponding author"上下文中提取
    corr_patterns = [
        r'(?:corresponding|correspondence|通讯)[\s\S]{0,300}?([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})',
        r'(?:email|e-mail|邮箱|Email address)[\s:：]*\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})',
        r'(?:Contact|联系)[\s\S]{0,200}?([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})',
    ]
    for pat in corr_patterns:
        matches = re.findall(pat, page_text, re.IGNORECASE)
        if matches:
            for em in matches:
                if not any(n in em.lower() for n in NOISE_EMAILS):
                    return em
    
    # 策略C: 正则全文搜索，优先匹配名字相关邮箱
    all_emails = EMAIL_PATTERN.findall(page_text)
    clean = [e for e in all_emails if not any(n in e.lower() for n in NOISE_EMAILS)]
    if clean:
        result = _match_best_email(clean, target_name, "")
        if result:
            return result
        for em in clean:
            if any(d in em.lower() for d in ['.edu', '.ac.', '.org']):
                return em
    
    return ""


# ================================================================
# 防线 1: Semantic Scholar
# ================================================================
def _search_s2_author(name: str) -> dict | None:
    """通过 Semantic Scholar Author API 查找作者主页和机构"""
    try:
        safe_query = quote(name)
        url = f"https://api.semanticscholar.org/graph/v1/author/search?query={safe_query}&fields=name,affiliations,homepage"
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        time.sleep(API_RATE_LIMIT_DELAY)
        if r.status_code == 200:
            data = r.json()
            if data.get('data') and len(data['data']) > 0:
                author = data['data'][0]
                return {
                    "homepage": author.get('homepage'),
                    "affiliations": author.get('affiliations', [])
                }
    except Exception as e:
        print(f"    ⚠️ S2 作者查询异常: {e}")
    return None


# ================================================================
# 防线 2: Google Scholar
# ================================================================
def _search_google_scholar(name: str, org: str) -> dict | str | None:
    """通过 scholarly 库查询 Google Scholar 认证学者信息"""
    try:
        from scholarly import scholarly
        clean_name = name.replace(',', '').replace('.', ' ').strip()
        search_query = scholarly.search_author(f"{clean_name} {org}")
        author = next(search_query)
        author_filled = scholarly.fill(author, sections=['basics'])
        
        email_domain = author_filled.get('email_domain', '')
        affiliation = author_filled.get('affiliation', '')
        scholar_id = author_filled.get('scholar_id', '')
        
        print(f"    ✅ [Google Scholar] 匹配: {affiliation}, 邮箱域: {email_domain}")
        return {
            "email_domain": email_domain,
            "affiliation": affiliation,
            "profile_url": f"https://scholar.google.com/citations?user={scholar_id}" if scholar_id else ""
        }
    except StopIteration:
        print(f"    ❌ [Google Scholar] 未找到 {name}")
        return None
    except Exception as e:
        error_msg = str(e).lower()
        if any(k in error_msg for k in ["captcha", "blocked", "429", "too many"]):
            print(f"    ⚠️ [Google Scholar] 被封锁，跳过")
            return "BLOCKED"
        print(f"    ⚠️ [Google Scholar] 异常: {e}")
        return None


# ================================================================
# 防线 3 [增强]: 多轮搜索引擎
# ================================================================
def _multi_round_web_search(name: str, org: str) -> str:
    """
    用多组不同关键词进行搜索，大幅提高主页命中率。
    第1轮: 姓名 + 机构 + lab homepage
    第2轮: 姓名 + 机构 + email contact
    第3轮: 姓名 + professor / researcher
    """
    queries = [
        f'"{name}" {org} lab homepage',
        f'"{name}" {org} email contact professor',
        f'"{name}" researcher homepage university',
    ]
    
    all_search_results = []
    
    for q in queries:
        try:
            results = DDGS().text(q, max_results=3)
            for r in results:
                item = {"title": r.get('title', ''), "href": r.get('href', '')}
                if item["href"] and item not in all_search_results:
                    all_search_results.append(item)
            time.sleep(0.5)  # 防限流
        except Exception as e:
            print(f"    ⚠️ 搜索异常 [{q[:30]}...]: {e}")
            continue
    
    if not all_search_results:
        print(f"    ❌ 多轮搜索均无结果")
        return ""
    
    # 去重后用 LLM 筛选
    unique_results = all_search_results[:8]  # 最多8条
    
    prompt = f"以下是搜索学者 {name} ({org}) 主页/联系方式时，搜索引擎返回的记录：\n"
    for idx, res in enumerate(unique_results):
        prompt += f"选项 {idx+1}: URL: {res.get('href')}  标题: {res.get('title')}\n"
    prompt += """请判断哪个链接最可能是该学者/其实验室的官方网站或个人学术主页。
优先选择 .edu, .ac.uk, .org 域名。排除 researchgate.net, linkedin.com, 百度百科, 知乎。
如果有多个可用链接，返回最好的一个。
如果完全没有合适的，输出 "NONE"。
只返回纯 URL，不要其他文字。"""
    
    try:
        llm_resp = smart_generate(prompt).strip()
        if "http" in llm_resp and "NONE" not in llm_resp.upper():
            match = re.search(r'(https?://[^\s,"\']+)', llm_resp)
            if match:
                url = match.group(1).rstrip('.)')
                print(f"    ✅ [多轮搜索] LLM 识别学者主页: {url}")
                return url
        print(f"    ❌ [多轮搜索] LLM 判断无有效结果")
    except Exception as e:
        print(f"    ⚠️ LLM 判断异常: {e}")
    
    return ""


# ================================================================
# 防线 4 [新增]: ORCID 查询
# ================================================================
def _search_orcid(name: str, org: str) -> dict | None:
    """通过 ORCID 公共 API 查找学者，获取邮箱和主页"""
    try:
        parts = name.replace(',', ' ').strip().split()
        if len(parts) < 2:
            return None
        
        # ORCID 搜索 API
        query = f'family-name:{parts[-1]}+AND+given-names:{parts[0]}'
        url = f"https://pub.orcid.org/v3.0/search/?q={query}&rows=3"
        headers = {"Accept": "application/json"}
        
        r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return None
        
        data = r.json()
        results = data.get("result", [])
        if not results:
            return None
        
        # 取第一个结果的 ORCID ID
        orcid_id = results[0].get("orcid-identifier", {}).get("path", "")
        if not orcid_id:
            return None
        
        # 获取详细信息
        detail_url = f"https://pub.orcid.org/v3.0/{orcid_id}/person"
        r2 = requests.get(detail_url, headers=headers, timeout=HTTP_TIMEOUT)
        if r2.status_code != 200:
            return None
        
        person = r2.json()
        
        # 提取邮箱
        email = ""
        emails_data = person.get("emails", {}).get("email", [])
        for em in emails_data:
            if em.get("email"):
                email = em["email"]
                break
        
        # 提取主页
        homepage = ""
        urls_data = person.get("researcher-urls", {}).get("researcher-url", [])
        for u in urls_data:
            url_val = u.get("url", {}).get("value", "")
            if url_val:
                homepage = url_val
                break
        
        if email or homepage:
            print(f"    ✅ [ORCID] ID: {orcid_id}, 邮箱: {email or '无'}, 主页: {homepage or '无'}")
            return {"email": email, "homepage": homepage, "orcid_id": orcid_id}
        
    except Exception as e:
        print(f"    ⚠️ ORCID 查询异常: {e}")
    return None


# ================================================================
# 网页深度抓取（含子页面探测）
# ================================================================
def _fetch_page_deep(url: str, find_team: bool = False, truncate_len: int = 50000) -> str:
    """深度抓取页面文本，如果 find_team=True 则自动探测 people/team/contact 子页面"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
    }
    
    def _do_fetch():
        scraper = cloudscraper.create_scraper()
        resp = scraper.get(url, headers=headers, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # 探测子页面
        team_links = []
        if find_team:
            keywords = ['people', 'team', 'members', 'directory', 'contact', 
                       'lab', 'about', 'faculty', 'staff', 'group']
            for a in soup.find_all('a', href=True):
                text = (a.get_text() + ' ' + a.get('href', '')).lower()
                if any(k in text for k in keywords):
                    full_url = urljoin(url, a['href'])
                    if full_url not in team_links and full_url != url:
                        team_links.append(full_url)
        
        # 保留原始 HTML（含 mailto 链接）
        raw_html = resp.text[:truncate_len]
        
        # 清理主页面得到纯文本
        for s in soup(['script', 'style', 'nav', 'footer', 'noscript', 'svg']):
            s.decompose()
        main_text = soup.get_text(separator=' ', strip=True)[:truncate_len]
        
        # 拼接原始 HTML
        combined = raw_html + "\n\n" + main_text
        
        # 抓取前 3 个子页面
        for link in team_links[:3]:
            try:
                r = scraper.get(link, headers=headers, timeout=10)
                if r.status_code == 200:
                    # 保留原始 HTML 以获取 mailto
                    combined += "\n\n" + r.text[:15000]
                    s2 = BeautifulSoup(r.text, 'html.parser')
                    for s in s2(['script', 'style', 'nav', 'footer']):
                        s.decompose()
                    combined += "\n\n" + s2.get_text(separator=' ', strip=True)[:15000]
            except Exception:
                pass
        
        return combined[:truncate_len * 3]
    
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_do_fetch)
            return future.result(timeout=SCRAPE_TIMEOUT)
    except concurrent.futures.TimeoutError:
        print(f"    ⚠️ 页面抓取超时: {url}")
        return ""
    except Exception as e:
        print(f"    ⚠️ 页面抓取失败: {e}")
        return ""


# ================================================================
# 邮箱提取辅助方法
# ================================================================
def _match_best_email(emails: list, name: str, domain_hint: str) -> str:
    """从邮箱列表中匹配与名字/域名最相关的一个"""
    name_parts = name.lower().replace(',', ' ').replace('.', ' ').split()
    name_parts = [p for p in name_parts if len(p) > 1]
    
    # 最佳：名字 + 域名都匹配
    if domain_hint:
        for email in emails:
            el = email.lower()
            if domain_hint.lower() in el and any(p in el for p in name_parts):
                return email
    
    # 其次：名字匹配
    for email in emails:
        el = email.lower()
        if any(p in el for p in name_parts):
            return email
    
    # 再次：域名匹配
    if domain_hint:
        for email in emails:
            if domain_hint.lower() in email.lower():
                return email
    
    return ""


def _regex_find_email(text: str, name: str, domain_hint: str = "") -> str:
    """用正则从页面文本中直接匹配邮箱"""
    all_emails = EMAIL_PATTERN.findall(text)
    if not all_emails:
        return ""
    
    filtered = [e for e in all_emails if not any(n in e.lower() for n in NOISE_EMAILS)]
    if not filtered:
        return ""
    
    return _match_best_email(filtered, name, domain_hint)


def _llm_extract_email(text: str, name: str, domain_hint: str = "") -> str:
    """用 LLM 从页面文本中提取目标作者的邮箱"""
    hint = f"注意：该学者的官方邮箱域名后缀可能是 {domain_hint}" if domain_hint else ""
    prompt = f"""
    在以下网页文本中，请找出学者 "{name}" 的联系邮箱。
    {hint}
    
    要求：
    1. 邮箱必须包含 @ 符号
    2. 不要编造邮箱，只提取文本中实际存在的
    3. 如果找不到，返回 "未找到"
    
    只返回邮箱地址本身（如 xxx@yyy.edu），不要其他文字。
    
    网页文本（截取前部分）：
    {text[:12000]}
    """
    try:
        resp = smart_generate(prompt, system_msg="你是一个精准的信息提取专家，只返回请求的具体数据。")
        resp = resp.strip().strip('"').strip("'")
        if '@' in resp and '.' in resp and len(resp) < 100:
            match = EMAIL_PATTERN.search(resp)
            if match:
                return match.group()
    except Exception as e:
        print(f"    ⚠️ LLM 邮箱提取异常: {e}")
    return ""


def _guess_email(name: str, domain: str) -> str:
    """基于姓名和邮箱域构建常见格式的猜测邮箱"""
    parts = name.lower().replace(',', '').replace('.', ' ').split()
    if len(parts) >= 2:
        first = parts[0]
        last = parts[-1]
        guessed = f"{first}.{last}@{domain}"
        print(f"    💡 根据名字和邮箱域猜测邮箱: {guessed}")
        return guessed
    return ""
