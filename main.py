import os
import json
import time
import cloudscraper
from bs4 import BeautifulSoup
import pandas as pd
from duckduckgo_search import DDGS
import google.generativeai as genai

# ================================
# Agent 环境准备与大模型初始化
# ================================
YOUR_API_KEY = "AIzaSyA8Ajb2nH0qs9SnWfOenWA0QVEmi5UqdpE"
genai.configure(api_key=YOUR_API_KEY)

# 仍然使用我们极其稳定且免费额度管够的 gemini-2.5-flash
model = genai.GenerativeModel('gemini-2.5-flash')

TEST_URLS = [
    # 继续沿用之前的测试源，让大统领 Agent 证明它可以通过这一篇线索挖出所有东西
    "https://www.nature.com/articles/s41586-024-07510-0",
]

# ================================
# Tools 层 (工具函数库)
# ================================
def tool_fetch_web(url, truncate_len=40000):
    print(f"🔧 [Tool调用] 正在抓取页面指纹内容: {url}")
    try:
        scraper = cloudscraper.create_scraper()
        resp = scraper.get(url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        for s in soup(['script', 'style', 'nav', 'footer']): s.decompose()
        return soup.get_text(separator=' ', strip=True)[:truncate_len]
    except Exception as e:
        print(f"⚠️ 抓取失败 {url}: {e}")
        return ""

def tool_search_web(query):
    print(f"🔍 [Tool调用] 正在使用 DuckDuckGo 公网搜索: {query}")
    try:
        # DDG 在连续高频访问时可能会抽风被屏蔽，所以只拿前3个结果保本
        results = DDGS().text(query, max_results=3)
        return [{"title": r['title'], "href": r['href']} for r in results]
    except Exception as e:
        print(f"⚠️ 搜索失败: {e}")
        return []

# ================================
# Agent: Planner (分析大师，负责拆解任务和提取高管目标)
# ================================
def planner_decompose(paper_url):
    print(f"\n🧠 [Planner规划] 分析论文意图并抽取目标核心人物...")
    text = tool_fetch_web(paper_url)
    if not text: return None
    
    prompt = f"""
    严格基于以下这篇学术论文网页的内容。请找出：第一作者的姓名及其所属机构，通讯作者的姓名及其所属机构。
    如果实在没在一篇很短的abstract摘要里标明谁是第一谁是通讯，请把第一个名字算作第一作者，把最后一个名字算作通讯作者。
    请严格返回JSON格式，不能含有 markdown ```json 的任何格式标签，仅仅保留一个有效的大括号：
    {{
        "第一作者": {{"姓名": "xxx", "机构": "yyy"}},
        "通讯作者": {{"姓名": "xxx", "机构": "yyy"}}
    }}
    源文本：{text[:30000]}
    """
    try:
        resp = model.generate_content(prompt)
        text_resp = resp.text.strip()
        if text_resp.startswith("```json"): text_resp = text_resp[7:-3].strip()
        if text_resp.startswith("```"): text_resp = text_resp[3:-3].strip()
        return json.loads(text_resp)
    except Exception as e:
        print(f"⚠️ Planner 分析人物实体时失败: {e}")
        return None

# ================================
# Agent: Worker (搬砖牛马，负责顺藤摸瓜找到目标并搜刮敏感信息)
# ================================
def agent_worker(author_info, author_type):
    name = author_info.get("姓名", "").strip()
    org = author_info.get("机构", "").strip()
    if not name or name == "未找到":
        return {"网站": "未找到", "本人邮箱": "未找到", "其他中国人名单": []}
        
    print(f"🤖 [Worker行动] 执行指令 -> 深入追踪 {author_type}: {name} ({org})")
    
    # 构建搜商公式：名字 + 机构 + lab 组合保证高搜中率
    query = f"{name} {org} research lab homepage"
    search_results = tool_search_web(query)
    
    if not search_results:
        print("🤖 [Worker报告] 公网搜索无任何结果返回，终止深潜。")
        return {"网站": "未找到", "本人邮箱": "未找到", "其他中国人名单": []}
        
    target_url = search_results[0]['href']
    print(f"🤖 [Worker报告] 判定高价值目标阵地主页为: {target_url}")
    
    # 深空潜能爬取，直接挖该老师或实验室的主页
    lab_text = tool_fetch_web(target_url, 30000)
    
    prompt = f"""
    以下是一个学术实验室或个人主页的纯文本（可能隶属于 {name}）。请提取：
    1. {name} 本人的联系邮箱 (通常带有 @ 符号)。
    2. 依据中国大陆姓名拼音特色（如 Zhang, Wang, Li, Chen），列出该主页文本中所有的中国研究人员以及能找到的每个人的邮箱（注意：不要包含 {name} 本人，只拿他们的手下同事）。如果邮箱没找到，请填"无"。
    
    强制输出格式为无修饰纯 JSON:
    {{
        "个人或实验室网站": "{target_url}",
        "本人邮箱": "提取出的邮箱，没有填未找到",
        "其他中国人名单": [
            {{"姓名": "Wang Xxx", "邮箱": "xxxx"}}
        ]
    }}
    需要研读的文本：{lab_text}
    """
    try:
        resp = model.generate_content(prompt)
        text_resp = resp.text.strip()
        if text_resp.startswith("```json"): text_resp = text_resp[7:-3].strip()
        if text_resp.startswith("```"): text_resp = text_resp[3:-3].strip()
        return json.loads(text_resp)
    except Exception as e:
        print(f"⚠️ Worker 剥离敏感信息失败: {e}")
        return {"个人或实验室网站": target_url, "本人邮箱": "未解析成功", "其他中国人名单": []}

# ================================
# Agent: Orchestrator (最高级：业务流控中心)
# ================================
def multi_agent_orchestrator():
    print("🚀 ========== 多智能体深度信息网络挖掘 Agent 启动 ==========")
    print(">>> 架构参照: Planner -> Agent -> Tools -> LLM Core -> Memory")
    
    all_results = []
    
    for url in TEST_URLS:
        print(f"\n▶ 开始侦查原始接头目标: {url}")
        
        # 将任务下发给 Planner 分析实体
        entities = planner_decompose(url)
        if not entities:
            continue
            
        print(f"📋 Planner 解析目标出库: {json.dumps(entities, ensure_ascii=False)}")
        
        row_data = {"原论文目标URL": url}
        
        # 将一作任务下发给 Worker
        time.sleep(2) # 规避网络拥堵
        first_author = entities.get("第一作者", {})
        first_res = agent_worker(first_author, "第一作者")
        row_data["第一作者"] = first_author.get("姓名", "未知名讳")
        row_data["第一作者主页或所属实验室"] = first_res.get("个人或实验室网站", "未搜到")
        row_data["第一作者邮箱"] = first_res.get("本人邮箱", "未发掘出")
        
        # 将通作任务下发给 Worker
        time.sleep(2)
        corr_author = entities.get("通讯作者", {})
        corr_res = agent_worker(corr_author, "通讯作者")
        row_data["通讯作者"] = corr_author.get("姓名", "未知名讳")
        row_data["通讯作者主页或所属实验室"] = corr_res.get("个人或实验室网站", "未搜到")
        row_data["通讯作者邮箱"] = corr_res.get("本人邮箱", "未发掘出")
        
        # Worker带回来的中国人们，由中台聚合除重
        chinese_list = first_res.get("其他中国人名单", []) + corr_res.get("其他中国人名单", [])
        seen = set()
        unique_chinese = []
        for x in chinese_list:
            if not isinstance(x, dict): continue
            key = str(x.get('姓名', '')) + str(x.get('邮箱', ''))
            if key not in seen:
                seen.add(key)
                unique_chinese.append(x)
                
        row_data["深挖出附带的中国研究员清单"] = "; ".join([f"{i.get('姓名', '未知')}({i.get('邮箱', '无')})" for i in unique_chinese])
        all_results.append(row_data)
        
    print("\n💾 大统领 Agent 正在整理汇聚的情报网络...")
    if all_results:
        df = pd.DataFrame(all_results)
        # 为解决 VSCode 原生不支持二进制 xlsx 阅读体验差的问题，同时输出 csv 文本表
        df.to_csv("target_emails.csv", index=False, encoding='utf-8-sig')
        df.to_excel("target_emails.xlsx", index=False)
        print("🎉 Agent 工程完全执行顺利。数据已下发至硬盘: target_emails.csv 和 .xlsx！")
    else:
        print("🤷 行动彻底失败，情报未被组织成功。")

if __name__ == "__main__":
    multi_agent_orchestrator()
