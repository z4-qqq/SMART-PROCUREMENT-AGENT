import os
from openai import AsyncOpenAI
from dotenv import load_dotenv
import asyncio

# Загрузка переменных из .env файла
load_dotenv()


print(os.getenv("EVOLUTION_KEY_ID"), os.getenv("EVOLUTION_KEY_SECRET"), os.getenv("EVOLUTION_ENDPOINT"))
client = AsyncOpenAI(
   api_key=os.getenv("OPENAI_API_KEY"),
   base_url=os.getenv("OPENAI_BASE_URL")
)

async def main():
   response = await client.chat.completions.create(
      model="openai/gpt-oss-120b",
      max_tokens=5000,
      temperature=0.5,
      presence_penalty=0,
      top_p=0.95,
      messages=[
         {
               "role": "user",
               "content":"Как написать хороший код?"
         }
      ]
   )
   return response

response = asyncio.run(main())
print(response.choices[0].message.content)
