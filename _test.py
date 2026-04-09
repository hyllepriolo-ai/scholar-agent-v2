from openai import OpenAI
c = OpenAI(
    api_key="sk-d2ebbe1c5f084d5fb0c43177644901be",
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"
)
print("Test 1: basic chat...")
r = c.chat.completions.create(
    model="qwen3.6-plus",
    messages=[{"role":"user","content":"say hi in one word"}],
    temperature=0.1
)
print("OK:", r.choices[0].message.content[:80])
print("Test 2: enable_search...")
r2 = c.chat.completions.create(
    model="qwen3.6-plus",
    messages=[{"role":"user","content":"today date?"}],
    temperature=0.1,
    extra_body={"enable_search": True}
)
print("OK:", r2.choices[0].message.content[:80])
print("ALL DONE")
