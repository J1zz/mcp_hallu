from openai import OpenAI
import sys
import os
import locale
# 样例代码版本 Version: 1.2.3
# 设置编码
os.environ['NO_PROXY'] = 'aigc.sankuai.com'
client = OpenAI(
    api_key = "2004053110662512723",
    base_url = "https://aigc.sankuai.com/v1/openai/native"
)

# 段式调用
result = client.chat.completions.create(
    model="gpt-4o",
    messages=[
        {
            "role": "user",
            "content": "说一个男性科学家的名字"
        }
    ],
    stream=False,
    extra_headers={
        "M-TraceId": "2004053110662512723"
    }
)
print(result)

# 流式调用
streamResult = client.chat.completions.create(
    model="gpt-4o",
    messages=[
        {
            "role": "user",
            "content": "说一个女性科学家的名字"
        }
    ],
    stream=True
)
for chunk in streamResult:
    char = chunk.choices[0].delta.content
    if char != None:
        sys.stdout.write(char)  
        sys.stdout.flush()