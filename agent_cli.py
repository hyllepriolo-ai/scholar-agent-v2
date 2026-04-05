import os
import json

# 引入强大的国产封装探员
from core.search_tools import resolve_doi_to_url, search_papers_by_query
from core.deep_miner import run_deep_mine_pipeline, smart_model_generate

# 正规化引入长期历史记忆模块
agent_conversation_memory = []

def nlp_agent_router(user_input):
    """最强大脑：自然语言全系解析调度器（附带 Memory 驱动）"""
    # 装载长期短期记忆回溯帧
    memory_string = "\n".join(agent_conversation_memory[-5:]) if agent_conversation_memory else "无历史。"
    
    prompt = f"""
    你是一个负责调度多级子模块的“中枢大统领 Agent”。
    用户的历史语境如下（如果他说“继续搜刚才那篇的其他人”，请参考上下文）：
    {memory_string}
    
    当前用户下达的自然语言是："{user_input}"
    
    你手里有且必须使用以下三大工具分支（请推理后选择）：
    1. 【DOI/文名 暴力解析】: 包含确切的 DOI （如 10.1038/xxx）。返回 {{"tool": "doi", "param": "xxx"}}
    2. 【批量搜文献雷达】: 模糊搜索要求。返回 {{"tool": "search", "param": "关键词", "count": 2}}
    3. 【直达网址网络投递】: 网页纯链接。返回 {{"tool": "url", "param": [url1, url2]}}
    4. 【闲聊或不知道】: 返回 {{"tool": "none"}}
       
    请在研读后，只返回那句 JSON （不要 ```json）：
    """
    try:
        print("\n🤔 [中枢大脑运转中] 分析解析老板指令意图并融合历史 Context...")
        text_resp = smart_model_generate(prompt)
        j = text_resp.strip()
        if j.startswith("```json"): j = j[7:-3].strip()
        if j.startswith("```"): j = j[3:-3].strip()
        
        agent_conversation_memory.append(f"User: {user_input}")
        agent_conversation_memory.append(f"Agent决策: {j}")
        return json.loads(j)
    except Exception as e:
        print(f"⚠️ Agent 大脑短路，无法理解自然语言意图: {e}")
        return None

def start_interactive_console():
    print("="*70)
    print("🏛️ 重装封装：【学术极客黑箱 - 全系溯源探员套件 - Runtime 启动】")
    print("="*70)
    print("功能简介：无需配置，只需告诉我你想搜狐什么领域的牛人或什么论文。所有结果自动生成表格。")
    print("命令示例：")
    print("  1. 这篇论文： 10.1038/s41586-024-07510-0 ，抓取一下里面的人！")
    print("  2. 在顶级期刊里找 3 篇关于 LLM Prompting 的文献，深入拉出他们作者与华人圈邮箱。")
    print("  3. https://xxx... 给我进去挖！")
    print("(随时输入 q 退出挂机系统)\n")
    
    while True:
        txt = input("\n👑 [BOSS 主控终端] 指令输入 > ")
        if txt.lower() in ['q', 'quit', 'exit', '登出']:
            print("🚀 Agent 进入休眠，期待下次任务！")
            break
            
        intent = nlp_agent_router(txt)
        if not intent:
            print("🤷 通讯系统受阻，指令未送达，请重新下令。")
            continue
            
        action = intent.get("tool")
        param = intent.get("param")
        limit = intent.get("count", 2) # 测试为快响应暂锁紧上限
        
        urls_to_dig = []
        
        if action == "doi":
            print(f"⚡ [解析系统触发] 已锁定单一明确目标标记，即将破解其底地址: {param}")
            url = resolve_doi_to_url(param)
            if url: URLs_to_target = [url]
            else: print("❌ 未通过全球库拿到合法解析 URL。")
        elif action == "search":
            print(f"📡 [网基雷达通联] 以该关键词启动大网跨国筛查: \'{param}\'。寻获 {limit} 个席位...")
            papers = search_papers_by_query(param, limit)
            for p in papers:
                print(f"   => [截获目标信号] {p['title']}")
                urls_to_dig.append(p['url'])
        elif action == "url":
            print(f"⚡ [超空间跃迁触发] 直接对标注阵地投送探底胶囊...")
            urls_to_dig = param if isinstance(param, list) else [param]
        else:
            print(f"⚠️ 底层架构尚未开发此类异构技能包，退回。")
            continue
            
        if urls_to_dig:
            run_deep_mine_pipeline(urls_to_dig)

if __name__ == "__main__":
    start_interactive_console()
