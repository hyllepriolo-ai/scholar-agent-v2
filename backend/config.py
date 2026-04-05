"""
全局配置中心 —— 统一管理所有 API Key、模型名称、超时设置。
架构设计：使用 OpenAI 兼容的客户端接口，可一键热插拔切换大模型后端。
"""
import os
from openai import OpenAI

# ================================================================
# 大模型供应商配置（当前默认：智谱 GLM-4-flash）
# 如需切换至 Deepseek / Gemini / OpenAI，只需修改以下三行
# ================================================================
LLM_API_KEY = os.environ.get(
    "LLM_API_KEY",
    "657998bf48944aa7a073b6cae6a2527f.EVTDVtArJZpNP0CG"
)
LLM_BASE_URL = os.environ.get(
    "LLM_BASE_URL",
    "https://open.bigmodel.cn/api/paas/v4/"
)
# 模型降级池：主力模型挂了自动轮换到备用
LLM_MODEL_POOL = ["glm-4-flash", "glm-4-plus"]

# ================================================================
# 统一初始化 LLM 客户端（OpenAI 兼容协议）
# ================================================================
llm_client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


def smart_generate(prompt: str, system_msg: str = "你是一个严谨且输出为纯净JSON格式的学术提取专家。") -> str:
    """
    带有多级降级回退的智能大模型请求器。
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


# ================================================================
# 网络请求超时配置
# ================================================================
HTTP_TIMEOUT = 15          # 普通 API 请求超时（秒）
SCRAPE_TIMEOUT = 45        # 网页深度抓取超时（秒）
API_RATE_LIMIT_DELAY = 1.5 # API 调用间隔（秒），防止被限流
