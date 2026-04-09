import sys
sys.path.insert(0, '.')
from backend.config import smart_generate_with_search, smart_generate

# 测试1: 不联网的普通请求
print("=== TEST 1: 普通请求 ===")
try:
    r1 = smart_generate("请回答：1+1等于几？只需要回答数字。")
    print(f"结果: [{r1}]")
except Exception as e:
    print(f"异常: {e}")

# 测试2: 联网搜索请求
print("\n=== TEST 2: 联网搜索 ===")
try:
    r2 = smart_generate_with_search("请搜索 Thirumala-Devi Kanneganti 的邮箱地址，她在 St. Jude Children's Research Hospital 工作。")
    print(f"结果: [{r2}]")
    print(f"长度: {len(r2) if r2 else 0}")
except Exception as e:
    print(f"异常: {e}")

# 测试3: 联网搜索一个简单问题
print("\n=== TEST 3: 联网搜索简单问题 ===")
try:
    r3 = smart_generate_with_search("今天的日期是什么？")
    print(f"结果: [{r3}]")
except Exception as e:
    print(f"异常: {e}")
