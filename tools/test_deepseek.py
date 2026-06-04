import time

from openai import OpenAI

client = OpenAI(
    base_url="https://api.deepseek.com",
    api_key="DEEPSEEK_API_KEY",
)

SYSTEM_PROMPT = "You are a helpful assistant."

messages = [{"role": "system", "content": SYSTEM_PROMPT}]

print("Chat started. Type 'exit' or 'quit' to stop.\n")

while True:
    user_input = input("You: ").strip()

    if not user_input:
        continue
    if user_input.lower() in ("exit", "quit"):
        print("Goodbye!")
        break

    messages.append({"role": "user", "content": user_input})

    print("Assistant: ", end="", flush=True)

    start_time = time.perf_counter()

    stream = client.chat.completions.create(
        model="deepseek-v4-flash",
        messages=messages,
        stream=True,
    )

    assistant_reply = ""
    for chunk in stream:
        delta = chunk.choices[0].delta
        if delta.content:
            print(delta.content, end="", flush=True)
            assistant_reply += delta.content

    elapsed = time.perf_counter() - start_time
    print(f"\n⏱ {elapsed:.2f}s\n")
    messages.append({"role": "assistant", "content": assistant_reply})
