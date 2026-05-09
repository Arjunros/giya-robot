from openai import OpenAI
from settings import load_settings
import json, os

def get_client():
    key = "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"   # ← your API key here
    return OpenAI(api_key=key)

def ask_gpt(question: str, language: str = "en") -> str:
    try:
        client = get_client()
        s = load_settings()
        model = s.get("ai_model", "gpt-5.4-nano")

        if language == "ta":
            system_prompt = "நீங்கள் ஒரு உதவியாளர். தமிழில் மட்டும் பதில் சொல்லுங்கள். சுருக்கமாக இரண்டு வாக்கியங்களில் பதில் சொல்லுங்கள்."
        else:
            system_prompt = "You are a helpful assistant. Keep answers short and clear, maximum 2 sentences. No bullet points."

        print(f"[GPT] Asking {model}: {question}")

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": question}
            ],
            max_completion_tokens=100
        )

        answer = response.choices[0].message.content.strip()
        print(f"[GPT] Answer: {answer}")
        return answer

    except Exception as e:
        print(f"[GPT] Error: {e}")
        return "Sorry, I could not connect to the internet to answer that."
