from openai import OpenAI

# Test Z.AI international endpoint (what Render will use)
c = OpenAI(
    api_key="657998bf48944aa7a073b6cae6a2527f.EVTDVtArJZpNP0CG",
    base_url="https://api.z.ai/api/paas/v4"
)

print("Test: Z.AI intl endpoint + GLM-5 + web_search")
try:
    r = c.chat.completions.create(
        model="glm-5",
        messages=[{"role":"user","content":"What is today's date?"}],
        temperature=0.1,
        tools=[{"type": "web_search", "web_search": {"enable": True}}]
    )
    print("OK:", r.choices[0].message.content[:150])
except Exception as e:
    print("FAIL:", e)
print("DONE")
