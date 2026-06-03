from openai import OpenAI

# Initialize the client pointing to your local LM Studio instance
client = OpenAI(
    base_url="http://localhost:1234/v1",  # Note the '/v1' at the end
    api_key="lm-studio",  # LM Studio doesn't require a key, but the SDK needs a non-empty string
)

# 1. (Optional) Dynamically fetch the loaded model's exact identifier
models = client.models.list()
if not models.data:
    raise RuntimeError(
        "No models found. Make sure a model is loaded and running in LM Studio!"
    )

model_id = models.data[0].id
print(f"Using loaded local model: {model_id}\n")

# 2. Create the chat completion request
response = client.chat.completions.create(
    model=model_id,  # Or pass the exact string like "meta-llama-3-8b-instruct"
    messages=[
        {
            "role": "user",
            "content": "Why do you think humans are so bad at peace?",
        },
    ],
    temperature=0.7,
)

# 3. Print out the response
print("Response:")
print(response.choices[0].message.content)
