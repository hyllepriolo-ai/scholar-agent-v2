import requests
from scholarly import scholarly, ProxyGenerator

def resolve_doi_to_url(doi):
    """根据 DOI 直接解析为该文章的最原始网址"""
    try:
        if doi.startswith("http"):
            doi = doi.split("doi.org/")[-1]
            
        url = f"https://api.crossref.org/works/{doi}"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"⚠️ 解析 DOI {doi} 交叉库失败: {e}")
        return None

def semantic_scholar_find_author_info(name, org=""):
    """
    零防线（排头兵）：完全公开免费的 Semantic Scholar API Graph。
    可以直接抓出官方认证的 URL 个人主页！
    """
    print(f"  [S2 学术智库] 免代理探查 Semantic Scholar 图谱: {name} ...")
    try:
        import time
        # URL 编码处理
        from urllib.parse import quote
        safe_query = quote(f"{name}")
        url = f"https://api.semanticscholar.org/graph/v1/author/search?query={safe_query}&fields=name,affiliations,homepage"
        
        r = requests.get(url, timeout=10)
        time.sleep(1.5) # 尊纪守法防限流
        if r.status_code == 200:
            data = r.json()
            if data.get('data') and len(data['data']) > 0:
                author = data['data'][0]
                homepage = author.get('homepage')
                affiliations = author.get('affiliations', [])
                print(f"  => 🧠 S2 命中目标图谱节点！官方登记网址: {homepage}")
                return {"homepage": homepage, "affiliations": affiliations}
            else:
                print("  => ❌ S2 作者库查无此人。")
        else:
            print(f"  => ⚠️ S2 接口频控或阻断，状态码: {r.status_code}")
    except Exception as e:
        print(f"  => ⚠️ S2 图谱侦测异常: {e}")
    return None

def semantic_scholar_get_paper_authors(identifier):
    """
    全知之眼：跨级获取 Paper 作者元数据，彻底绕开 HTML 的反爬系统
    """
    import time
    print(f"  [元数据直连] 切入 Semantic Scholar 官方档案库: {identifier}")
    try:
        if "doi.org/" in identifier:
            doi_part = identifier.split("doi.org/")[-1]
            paper_id = f"DOI:{doi_part}"
        elif identifier.startswith("10."):
            paper_id = f"DOI:{identifier}"
        else:
            paper_id = f"URL:{identifier}"
            
        url = f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}?fields=title,authors,authors.name,authors.affiliations,authors.homepage"
        r = requests.get(url, timeout=10)
        import time
        time.sleep(1.5)
        if r.status_code == 200:
            data = r.json()
            if data and data.get("authors"):
                return data
        elif r.status_code == 404 and "doi.org" in identifier:
            print(f"  => ⚠️ S2 专属端点 (DOI) 未收录。启动 Crossref API 绕回机制寻找 Title...")
            try:
                doi = identifier.split("doi.org/")[-1]
                cr_url = f"https://api.crossref.org/works/{doi}"
                cr_resp = requests.get(cr_url, timeout=10)
                if cr_resp.status_code == 200:
                    title = cr_resp.json().get("message", {}).get("title", [""])[0]
                    if title:
                        print(f"  => ✨ Crossref 反向打捞获得标题: {title}，向 S2 投递自然语言探测...")
                        from urllib.parse import quote
                        s2_search_url = f"https://api.semanticscholar.org/graph/v1/paper/search?query={quote(title)}&limit=1&fields=title,authors,authors.name,authors.affiliations,authors.homepage"
                        s2_resp = requests.get(s2_search_url, timeout=10)
                        if s2_resp.status_code == 200:
                            s2_search_data = s2_resp.json()
                            if s2_search_data.get("data") and len(s2_search_data["data"]) > 0:
                                return s2_search_data["data"][0]
            except Exception as e:
                print(f"  => ❌ Crossref 备用降维查询失败: {e}")
        else:
            print(f"  => ⚠️ S2 论文端点未收录或触发风控，状态码：{r.status_code}")
    except Exception as e:
        print(f"  => ⚠️ S2 结构化读取崩塌: {e}")
    return None

def scholar_find_author_info(name, org=""):
    """
    第一防线：直接调用 Google Scholar 底层 API，提取作者注册过的认证档案。
    如果 IP 被封或请求被掐断，系统将截获错误由外部切换为后手。
    """
    print(f"  [谷歌学术网] 正在尝试直连检索认证学者库: {name} ...")
    try:
        search_query = scholarly.search_author(f"{name} {org}")
        # 获取第一条认证匹配
        author = next(search_query)
        # 深层提取邮箱后缀与认证机构
        author_filled = scholarly.fill(author, sections=['basics'])
        
        email_domain = author_filled.get('email_domain', '')
        affiliation = author_filled.get('affiliation', '')
        print(f"  => 🎓 谷歌学术匹配成功！该学者拥有官方认证：所属机构 [{affiliation}], 认证邮箱域 [{email_domain}]")
        
        return {
            "认证机构": affiliation,
            "所属邮箱后缀": email_domain,
            "谷歌学术主页": f"https://scholar.google.com/citations?user={author_filled.get('scholar_id')}"
        }
    except StopIteration:
        print("  => ❌ 谷歌学术库中查无此人，可能未注册认证档案。")
        return None
    except Exception as e:
        # 当被拦截、封禁时返回特俗标识以触发后手
        print(f"  => ⚠️ 后手防线触发警报：谷歌学术接口出现网络崩溃或 IP 被封禁！报错：{e}")
        return "BLOCKED"

def search_papers_by_query(query, limit=5):
    """根据自然语言标题或关键字，在 Crossref 中拉取最近或最相关的文献列表"""
    try:
        # 利用强大参数进行高精度匹配
        url = f"https://api.crossref.org/works?query={query}&select=URL,title,DOI&rows={limit}"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        items = data.get('message', {}).get('items', [])
        
        results = []
        for item in items:
            title = item.get('title', ['未命名'])[0]
            link = item.get('URL', '')
            doi = item.get('DOI', '')
            results.append({"title": title, "url": link, "doi": doi})
            
        return results
    except Exception as e:
        print(f"⚠️ 查询全球文献库网络波动异常: {e}")
        return []
