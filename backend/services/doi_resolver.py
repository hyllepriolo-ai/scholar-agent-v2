"""
DOI 解析器 —— 将 DOI 号转换为论文的结构化元数据。
双通道策略：Semantic Scholar API 优先，Crossref API 兜底。
"""
import time
import requests
from urllib.parse import quote
from backend.config import HTTP_TIMEOUT, API_RATE_LIMIT_DELAY


def resolve_doi(doi: str) -> dict:
    """
    核心入口：传入一个标准 DOI，返回论文元数据字典。
    包含：标题、期刊、作者列表（含姓名、机构、是否通讯）。
    
    返回格式:
    {
        "title": "xxx",
        "journal": "Nature",
        "authors": [
            {"name": "Xxx", "affiliations": ["MIT"], "is_corresponding": False},
            ...
        ],
        "source": "semantic_scholar" | "crossref"
    }
    """
    print(f"\n🔬 [DOI解析] 开始解析: {doi}")
    
    # 第一通道：Semantic Scholar（结构化数据最好）
    result = _try_semantic_scholar(doi)
    if result and result.get("authors"):
        print(f"  ✅ Semantic Scholar 命中，标题: {result.get('title', '未知')[:60]}")
        return result

    # 第二通道：Crossref（覆盖面更广）
    result = _try_crossref(doi)
    if result:
        print(f"  ✅ Crossref 命中，标题: {result.get('title', '未知')[:60]}")
        return result

    print(f"  ❌ DOI {doi} 在所有数据源中均未找到")
    return {"title": "未获取", "journal": "未获取", "authors": [], "source": "none"}


def _try_semantic_scholar(doi: str) -> dict | None:
    """通过 Semantic Scholar API 获取论文和作者元数据"""
    print(f"  📡 [S2] 查询 Semantic Scholar...")
    try:
        paper_id = f"DOI:{doi}"
        url = (
            f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}"
            f"?fields=title,venue,authors,authors.name,authors.affiliations,authors.homepage"
        )
        resp = requests.get(url, timeout=HTTP_TIMEOUT)
        time.sleep(API_RATE_LIMIT_DELAY)
        
        if resp.status_code == 200:
            data = resp.json()
            if data and data.get("authors"):
                authors = []
                for a in data["authors"]:
                    authors.append({
                        "name": a.get("name", ""),
                        "affiliations": a.get("affiliations", []) or [],
                        "homepage": a.get("homepage", ""),
                        "is_corresponding": False  # S2 不直接标记，后续由 extractor 判断
                    })
                return {
                    "title": data.get("title", ""),
                    "journal": data.get("venue", ""),
                    "authors": authors,
                    "source": "semantic_scholar"
                }
        elif resp.status_code == 404:
            print(f"  ⚠️ S2 未收录此 DOI，尝试通过标题反查...")
            return _s2_fallback_by_title(doi)
        else:
            print(f"  ⚠️ S2 异常状态码: {resp.status_code}")
    except Exception as e:
        print(f"  ⚠️ S2 请求异常: {e}")
    return None


def _s2_fallback_by_title(doi: str) -> dict | None:
    """S2 主接口未收录时，先用 Crossref 拿标题，再用标题在 S2 搜索"""
    try:
        # 用 Crossref 获取标题
        cr_url = f"https://api.crossref.org/works/{doi}"
        cr_resp = requests.get(cr_url, timeout=HTTP_TIMEOUT)
        if cr_resp.status_code != 200:
            return None
        title = cr_resp.json().get("message", {}).get("title", [""])[0]
        if not title:
            return None
        
        print(f"  🔄 Crossref 拿到标题: {title[:50]}... 反查 S2...")
        s2_search_url = (
            f"https://api.semanticscholar.org/graph/v1/paper/search"
            f"?query={quote(title)}&limit=1"
            f"&fields=title,venue,authors,authors.name,authors.affiliations,authors.homepage"
        )
        s2_resp = requests.get(s2_search_url, timeout=HTTP_TIMEOUT)
        time.sleep(API_RATE_LIMIT_DELAY)
        
        if s2_resp.status_code == 200:
            s2_data = s2_resp.json()
            if s2_data.get("data") and len(s2_data["data"]) > 0:
                paper = s2_data["data"][0]
                authors = []
                for a in paper.get("authors", []):
                    authors.append({
                        "name": a.get("name", ""),
                        "affiliations": a.get("affiliations", []) or [],
                        "homepage": a.get("homepage", ""),
                        "is_corresponding": False
                    })
                return {
                    "title": paper.get("title", ""),
                    "journal": paper.get("venue", ""),
                    "authors": authors,
                    "source": "semantic_scholar"
                }
    except Exception as e:
        print(f"  ⚠️ S2 标题反查异常: {e}")
    return None


def _try_crossref(doi: str) -> dict | None:
    """通过 Crossref API 获取论文元数据"""
    print(f"  📡 [Crossref] 查询 Crossref API...")
    try:
        url = f"https://api.crossref.org/works/{doi}"
        resp = requests.get(url, timeout=HTTP_TIMEOUT)
        
        if resp.status_code == 200:
            msg = resp.json().get("message", {})
            title = msg.get("title", [""])[0] if msg.get("title") else ""
            journal = msg.get("container-title", [""])[0] if msg.get("container-title") else ""
            
            authors = []
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
                    "is_corresponding": a.get("sequence", "") == "additional"  # Crossref 约定
                })
            
            # Crossref 中 sequence="first" 的是第一作者
            if authors and not any(a["is_corresponding"] for a in authors):
                # 如果没有标记通讯作者，默认最后一个为通讯
                authors[-1]["is_corresponding"] = True
            
            return {
                "title": title,
                "journal": journal,
                "authors": authors,
                "source": "crossref"
            }
        else:
            print(f"  ⚠️ Crossref 返回状态码: {resp.status_code}")
    except Exception as e:
        print(f"  ⚠️ Crossref 请求异常: {e}")
    return None
