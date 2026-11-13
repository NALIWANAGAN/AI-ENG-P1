import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# Initialize the client pointing to Ollama Cloud
client = OpenAI(
    base_url=os.getenv("OLLAMA_PROVIDER_URL"),
    api_key=os.getenv("OLLAMA_API_KEY"),
)
