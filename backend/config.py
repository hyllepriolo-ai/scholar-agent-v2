"""
全局配置中心 —— 统一管理所有 API Key、模型名称、超时设置。
架构设计：使用 OpenAI 兼容的客户端接口，可一键热插拔切换大模型后端。

支持的后端：
  - 阿里云通义千问（DashScope）：本地开发默认使用，enable_search 联网搜索
  - 智谱 GLM（BigModel / Z.AI）：Render 部署使用，tools web_search 联网搜索
"""
import os
from openai import OpenAI

# ================================================================
# 大模型供应商配置（通过环境变量切换供应商）
# ================================================================
LLM_API_KEY = os.environ.get(
    "LLM_API_KEY",
    "sk-f40e31d390094e72b978bb75630e4be1"
)
LLM_BASE_URL = os.environ.get(
    "LLM_BASE_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
# 模型降级池：主力模型挂了自动轮换到备用
LLM_MODEL_POOL = os.environ.get("LLM_MODEL_POOL", "qwen-plus,qwen-turbo").split(",")

# ================================================================
# 自动检测供应商（根据 base_url 判断）
# ================================================================
def _is_zhipu():
    """判断当前是否使用智谱 GLM 后端"""
    return "bigmodel.cn" in LLM_BASE_URL or "z.ai" in LLM_BASE_URL

# ================================================================
# 统一初始化 LLM 客户端（OpenAI 兼容协议）
# ================================================================
llm_client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


def smart_generate(prompt: str, system_msg: str = "你是一个严谨且输出为纯净JSON格式的学术提取专家。") -> str:
    """
    带有多级降级回退的智能大模型请求器（不联网）。
    遍历 LLM_MODEL_POOL 中的所有模型，直到成功获取回复。
    """
    for model_name in LLM_MODEL_POOL:
        try:
            resp = llm_client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1
            )
            return resp.choices[0].message.content
        except Exception as e:
            print(f"  ⚠️ [大模型] {model_name} 请求失败 ({type(e).__name__}: {e})，尝试备用模型...")
            continue
    raise Exception("所有大模型节点均不可用，请检查 API Key 或网络连接。")


def smart_generate_with_search(prompt: str, system_msg: str = "你是一个专业的学术信息搜索专家。请利用你的联网搜索能力，尽力查找准确的学术联系信息。") -> str:
    """
    带有联网搜索能力的大模型请求器。
    自动适配不同供应商的联网搜索方式：
      - DashScope（阿里云）：extra_body={"enable_search": True}
      - 智谱 GLM：tools=[{"type": "web_search", ...}]
    """
    # 根据供应商构造搜索参数
    if _is_zhipu():
        search_kwargs = {"tools": [{"type": "web_search", "web_search": {"enable": True}}]}
    else:
        search_kwargs = {"extra_body": {"enable_search": True}}

    for model_name in LLM_MODEL_POOL:
        try:
            resp = llm_client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                **search_kwargs
            )
            return resp.choices[0].message.content
        except Exception as e:
            print(f"  ⚠️ [联网搜索] {model_name} 请求失败 ({type(e).__name__}: {e})，尝试备用模型...")
            continue
    raise Exception("所有联网搜索模型节点均不可用，请检查 API Key 或网络连接。")


# ================================================================
# 网络请求超时配置
# ================================================================
HTTP_TIMEOUT = 15          # 普通 API 请求超时（秒）
SCRAPE_TIMEOUT = 45        # 网页深度抓取超时（秒）
API_RATE_LIMIT_DELAY = 1.5 # API 调用间隔（秒），防止被限流
