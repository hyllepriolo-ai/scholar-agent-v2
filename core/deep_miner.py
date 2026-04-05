import os
import json
import time
import requests
import cloudscraper
import concurrent.futures
from bs4 import BeautifulSoup
import pandas as pd
from duckduckgo_search import DDGS
from urllib.parse import urljoin
from openai import OpenAI

YOUR_API_KEY = os.environ.get("GLM_API_KEY", "657998bf48944aa7a073b6cae6a2527f.EVTDVtArJZpNP0CG")

# 全量替换为智谱兼容大模型基座
llm_client = OpenAI(
    api_key=YOUR_API_KEY, 
    base_url="https://open.bigmodel.cn/api/paas/v4/"
)

# 核心突破：构建模型储备池。GLM-4-flash 为免费无限制长文本模型
FALLBACK_MODELS = ['glm-4-flash', 'glm-4-plus']

def smart_model_generate(prompt):
    """带有多级火箭级联回退的智能大模型请求器（国产智谱版）"""
    for model_name in FALLBACK_MODELS:
        try:
            resp = llm_client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": "你是一个严谨且输出为纯净代码格式的学术提取专家。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1
            )
            return resp.choices[0].message.content
        except Exception as e:
            print(f"  🔻 [大模型警报] {model_name} 引擎计算失败 ({type(e).__name__})，可能由于并发限制自动切换至备用...")
            continue
    raise Exception("所有国内 API 节点计算超时或抛锚，请检查网络设置或账户状态。")

def _do_fetch_advanced(url, truncate_len, find_team=False):
    """底层的硬派网页获取器，带有极强的子页面衍生抓取能力"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
    }
    # 既然已经有了绝对时间锁，重启 cloudscraper 穿盾
    scraper = cloudscraper.create_scraper()
    resp = scraper.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')
    
    # ==== 工业级迭代：如果是实验室首页，必须向下渗透查找“团队页” ====
    team_links = []
    if find_team:
        for a in soup.find_all('a', href=True):
            text = a.get_text().lower()
            # 常见学术网站放人和邮箱的地方
            if any(k in text for k in ['people', 'team', 'members', 'directory', 'contact']):
                full_url = urljoin(url, a['href'])
                if full_url not in team_links:
                    team_links.append(full_url)
                    
    # 原网页清理
    for s in soup(['script', 'style', 'nav', 'footer', 'header', 'noscript', 'svg']): s.decompose()
    main_text = f"【主域 {url} 内容】:\n" + soup.get_text(separator=' ', strip=True)[:truncate_len]
    
    # 附带拉取前3个关键团队子页的文本
    for link in team_links[:3]:
        try:
            print(f"      [发现子节点] 自动渗透连带页面: {link}")
            r = scraper.get(link, headers=headers, timeout=10)
            if r.status_code == 200:
                s2 = BeautifulSoup(r.text, 'html.parser')
                for s in s2(['script', 'style', 'nav', 'footer']): s.decompose()
                main_text += f"\n\n【子域 {link} 内容】:\n" + s2.get_text(separator=' ', strip=True)[:15000]
        except Exception:
            pass
            
    return main_text[:truncate_len * 2]

def tool_fetch_web(url, truncate_len=50000, find_team=False):
    print(f"  [黑箱穿透] 开启强力超频提取 (允许深度渗透: {find_team}): {url}")
    try:
        # 硬性超时管控 45 秒，因为包含了多个子网页的连续请求
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_do_fetch_advanced, url, truncate_len, find_team)
            text = future.result(timeout=45)
        return text
    except concurrent.futures.TimeoutError:
        print(f"⚠️ [系统防护锁] 抓取链无限卡死，已强行熔断丢弃: {url}")
        return ""
    except Exception as e:
        print(f"⚠️ 核心突破失败: {url} | 报错: {e}")
        return ""

def tool_search_web(query):
    print(f"  [情报眼] 泛互联网反追溯搜索: {query}")
    try:
        results = DDGS().text(query, max_results=4)
        return [{"title": r['title'], "href": r['href']} for r in results]
    except Exception as e:
        print(f"⚠️ 雷达被干扰: {e}")
        return []

from core.search_tools import semantic_scholar_get_paper_authors

def planner_decompose(paper_url):
    print(f"\n🧠 [逻辑中枢] 大模型研读顶刊论文结构，抽取通讯特征...")
    
    source_context = ""
    # 战略变轨：跨越反爬，强制首选 API 拉取元数据
    s2_data = semantic_scholar_get_paper_authors(paper_url)
    
    if s2_data and s2_data.get("authors"):
        print("  => ✅ 成功拦截底层数据链路！无需暴力破解 HTML 即可调阅作者名图册！")
        # 将官方 json 转为纯文本供大模型降维分析
        source_context = f"【官方底层元数据 JSON】\n{json.dumps(s2_data, ensure_ascii=False)}"
    else:
        # S2 阵亡，降级回暴力突破模式
        print("  => ⚠️ 官方元数据缺失，降级至底层网页 HTML 暴力突袭模式 (可能遭遇 403)...")
        html_text = tool_fetch_web(paper_url, find_team=False)
        if not html_text: 
            return None
        source_context = f"【源网页截获摘要】\n{html_text[:35000]}"
    
    prompt = f"""
    在以下提取的学术档案中，找出该研究的第一作者和通讯作者。
    如果发现提供的数据是【官方底层元数据 JSON】，请从 "authors" 数组中抽取（通常第一个是一作，末尾的是通讯作者）。
    如果是【源网页截获摘要】，如果在文章中通讯作者（Corresponding author）不明确，默认找最后的领衔专家。
    
    如果真的获取不到姓名，或者发现是 "Unknown" 或查无此人，请严格填入 "未提供"。
    
    强制输出纯净无 ```json 标签的 JSON 对象：
    {{
        "第一作者": {{"姓名": "Xxx", "机构": "Xxx"}},
        "通讯作者": {{"姓名": "Xxx", "机构": "Xxx"}}
    }}
    
    {source_context}
    """
    try:
        text_resp = smart_model_generate(prompt)
        text_resp = text_resp.replace('```json', '').replace('```', '').strip()
        data = json.loads(text_resp)
        return data
    except Exception as e:
        print(f"⚠️ Planner 智能解码实体破裂: {e}")
        return None

from core.search_tools import scholar_find_author_info, semantic_scholar_find_author_info

def agent_worker(author_info, author_type):
    name = author_info.get("姓名", "").strip()
    org = author_info.get("机构", "").strip()
    
    # 工业级截流阀：坚决挡住毒瘤脏数据
    invalid_tags = ["未提供", "未找到", "无", "none", "unknown", ""]
    if not name or name.lower() in invalid_tags:
        print(f"  [系统熔断] {author_type} 解析出无效空数据，直接跳过侦测以保护检索器。")
        return {}
    
    target_url = ""
    scholar_profile = ""
    official_email_domain = ""
    
    # ==========================================
    # 防线 1： Semantic Scholar 直接降维打击与机构特征剥离
    # ==========================================
    s2_data = semantic_scholar_find_author_info(name, org)
    if s2_data:
        if s2_data.get("homepage"):
            target_url = s2_data["homepage"]
            print(f"  [雷达捷报] Semantic Scholar 官方图谱免搜直透出了作者大本营: {target_url}")
        
        if s2_data.get("affiliations") and len(org) < 3:
            org = " ".join(s2_data["affiliations"])
            print(f"  [情报眼萃取] 成功从 S2 盲盒洗出该学者的隐藏机构！更新通缉特征为: {org}")
    
    # ==========================================
    # 防线 2： 调取 Google Scholar 获取权威直连邮箱后缀
    # ==========================================
    # 净化谷歌学术的名字(剥离逗号等非法字符)，提升识别率
    clean_name = name.replace(',', '').replace('.', ' ').strip()
    scholar_data = scholar_find_author_info(clean_name, org)
    
    if scholar_data == "BLOCKED":
        print(f"  [系统灾备切换] 发现谷歌学术被封墙屏蔽...")
    elif scholar_data:
        official_email_domain = scholar_data["所属邮箱后缀"]
        scholar_profile = scholar_data["谷歌学术主页"]
        
    # ==========================================
    # 防线 3：鸭鸭搜全网搜索引擎探底 + 大模型判别验证器
    # ==========================================
    if not target_url:
        print(f"  [后手兜底] 前沿防线未能拿到明文网址，动用搜索引擎反狙网络结构...")
        query = f"\"{name}\" {org} research lab website biology chemistry computer science members people"
        search_results = tool_search_web(query)
    
        if search_results:
            print(f"  [智能路由判决] 已截获 {len(search_results)} 条野生情报，交由大模型进行真伪筛选...")
            prompt = f"以下是寻找学者 {name} ({org}) 的实验室主页时，搜索引擎返回的4条记录：\n"
            for idx, res in enumerate(search_results):
                prompt += f"选项 {idx+1}:\nURL: {res.get('href')}\n标题: {res.get('title')}\n\n"
            prompt += """请你判断哪个链接最有可能是其实验室官方大本营(包含团队列表或联络方式)。我们主要寻找 .edu, .ac.uk, .org 或专有实验室域名。
如果全部都是百度、谷歌搜索页、研究集团首页、问答网、或者明显错误的商业官网，请直接输出 "NONE"。
如果是明确的学术主页，请只输出该最终确定的纯 URL 地址。"""
            try:
                llm_decision = smart_model_generate(prompt).strip()
                if "http" in llm_decision and "NONE" not in llm_decision:
                    # 提取纯URL
                    import re
                    match = re.search(r'(https?://[^\s]+)', llm_decision)
                    if match:
                        target_url = match.group(1)
                        print(f"  => 🧠 LLM 慧眼识珠，从乱草中挑出真骨血: {target_url}")
                else:
                    print("  => ❌ LLM 判断：所搜结果全是垃圾广告或无关重名。直接丢弃该作者的网搜动作。")
            except Exception as e:
                print(f"  => ⚠️ LLM 判决器离线: {e}")
            
    if not target_url:
        print(f"  -> 无法在公网锁定匹配 {name} 的高净值实验室网站。")
        return {
            "网站": scholar_profile if scholar_profile else "无", 
            "本人邮箱": f"未知@{official_email_domain}" if official_email_domain else "无", 
            "中国人名单": []
        }
        
    print(f"  -> 锁死敌方大本营基站主页为: {target_url}")
    lab_text = tool_fetch_web(target_url, 40000, find_team=True)
    
    domain_hint = f"注意：该教授在谷歌学术上的官方注册邮箱后缀是 {official_email_domain}" if official_email_domain else ""
    prompt = f"""
    在从目标 "{name}" 关联的网站提取的乱码文本中进行地毯式搜索。
    {domain_hint}
    1. 请你竭尽全力找出 {name} 本人的完整邮箱。
    2. 基于中国人的拼音姓氏法则，给我列出所有在这份文本中找到的中国人/亚裔的学者与学生姓名，以及必须要对应附上能找到的属于他们的各种邮箱。
    
    不需要啰嗦，直接以规范纯净的 JSON 返回。
    {{
        "网站": "{target_url}",
        "本人邮箱": "未找到",
        "中国人名单": [ {{"姓名": "Xxx", "邮箱": "Xxx"}} ]
    }}
    截获的包含子页面的超级网页缓存：
    {lab_text}
    """
    try:
        print("  🧠 正在召唤巨量上下文模型融合双轨数据...")
        text_resp = smart_model_generate(prompt)
        text_resp = text_resp.replace('```json', '').replace('```', '').strip()
        data = json.loads(text_resp)
        if not data.get("网站") and scholar_profile: data["网站"] = scholar_profile
        return data
    except Exception as e:
        print(f"⚠️ 智能解析子页面时数据超载崩溃: {e}")
        return {}

def run_deep_mine_pipeline(urls: list):
    print("\n🚀 [大统领中枢] 已接管目标靶向资源，开始全自动无停歇追踪流水线...")
    if not urls: return
        
    all_results = []
    for url in urls:
        print(f"\n▶ 锚定当前文献源: {url}")
        entities = planner_decompose(url)
        if not entities: 
            print("⚠️ 源作者未能解析。")
            continue
            
        row_data = {"原文献追溯链接": url}
        
        time.sleep(2) # 强稳策略
        first_res = agent_worker(entities.get("第一作者", {}), "第一作者")
        row_data["第一作者"] = entities.get("第一作者", {}).get("姓名", "")
        row_data["一作实验室大本营"] = first_res.get("网站", "无")
        row_data["一作本人邮箱"] = first_res.get("本人邮箱", "无")
        
        time.sleep(2)
        corr_res = agent_worker(entities.get("通讯作者", {}), "通讯作者")
        row_data["通讯作者"] = entities.get("通讯作者", {}).get("姓名", "")
        row_data["通讯实验室大本营"] = corr_res.get("网站", "无")
        row_data["通讯作者邮箱"] = corr_res.get("本人邮箱", "无")
        
        # 聚合并过滤噪音
        c_list = first_res.get("中国人名单", []) + corr_res.get("中国人名单", [])
        seen = []
        for c in c_list:
            if not isinstance(c, dict): continue
            name = c.get('姓名', '')
            email = c.get('邮箱', '')
            s = f"{name}({email})"
            if s not in seen and len(name) > 1: seen.append(s)
            
        row_data["全网站扫出的华人团队与邮箱"] = "; ".join(seen)
        all_results.append(row_data)
        
    if all_results:
        df = pd.DataFrame(all_results)
        df.to_csv("agent_exports_toolkit_v2.csv", index=False, encoding='utf-8-sig')
        df.to_excel("agent_exports_toolkit_v2.xlsx", index=False)
        print("✅ =============================================")
        print("✅ [工业级成果出炉] 完全穿透防守链！更精准且量大的战利品已放置于 agent_exports_toolkit_v2")
        print("✅ =============================================")
    else:
        print("🤷 行动结束。")
