import os
from dotenv import load_dotenv
from anthropic import Anthropic
import streamlit as st
from datetime import datetime
import requests
import json
import re

load_dotenv()

client = Anthropic(
    base_url=os.getenv("ANTHROPIC_BASE_URL"),
    api_key=os.getenv("ANTHROPIC_API_KEY"),
)

def ask_ai(prompt):
    res = client.messages.create(
        model="claude-haiku-5.5",max_tokens=1000,
        messages=[
            {
                "role": "user",
                "content": prompt,
            }
        ]
    )
    answer = "".join(b.text for b in res.content if b.type == "text")
    return answer
