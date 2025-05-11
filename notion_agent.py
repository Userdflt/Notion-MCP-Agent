import os
import asyncio
import threading
import tkinter as tk
from tkinter import scrolledtext
import traceback
from dotenv import load_dotenv
from mcp.client.sse import sse_client
from mcp import ClientSession, McpError
from langchain_mcp_adapters.tools import load_mcp_tools
from langchain_mcp_adapters.prompts import load_mcp_prompt
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from langchain.tools import Tool

# Load environment variables
dotenv_loaded = load_dotenv()
PAGE_ID        = os.getenv("PAGE_ID", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API", "").strip()
MCP_SSE_URL    = os.getenv("MCP_SERVER_URL", "http://localhost:8000/sse")

# Single ChatOpenAI instance
model = ChatOpenAI(model="gpt-4o", openai_api_key=OPENAI_API_KEY)

async def initialize_agent():
    # Manually enter SSE and session contexts to keep them open
    sse_ctx = sse_client(MCP_SSE_URL)
    reader, writer = await sse_ctx.__aenter__()

    session_ctx = ClientSession(reader, writer)
    notion = await session_ctx.__aenter__()
    await notion.initialize()

    # Async summary tool
    async def summarize(text: str) -> str:
        prompt = (
            "Here is some Notion-page content. "
            "Please give me a concise summary:\n\n" + text
        )
        return await model.apredict(prompt)

    # Load MCP tools and add summary
    tools = await load_mcp_tools(notion)
    tools.append(
        Tool(
            name="summarize",
            func=summarize,
            description="Summarize block of texts",
        )
    )

    # Create REACT agent
    agent = create_react_agent(model, tools)
    return sse_ctx, session_ctx, notion, agent

# Setup asyncio loop
loop = asyncio.new_event_loop()
threading.Thread(target=lambda: asyncio.set_event_loop(loop) or loop.run_forever(), daemon=True).start()

# Initialize agent/session
init_future = asyncio.run_coroutine_threadsafe(initialize_agent(), loop)
sse_ctx, session_ctx, notion, agent = init_future.result()

# Build GUI
tk_root = tk.Tk()
tk_root.title("Notion MCP Agent")

# Input area
frame = tk.Frame(tk_root)
frame.pack(fill=tk.X, padx=5, pady=5)
label = tk.Label(frame, text="Input:")
label.pack(side=tk.LEFT)
entry = tk.Entry(frame)
entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
button = tk.Button(frame, text="Send")
button.pack(side=tk.LEFT, padx=5)

# Output area
output = scrolledtext.ScrolledText(tk_root, wrap=tk.WORD, state=tk.DISABLED)
output.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

# Configure text tags for styling
output.tag_configure("thinking", font=(None, 10, "bold"))
output.tag_configure("response", foreground="#555555", font=(None, 10, "bold"))
output.tag_configure("content", foreground="#555555", font=(None, 10, "normal"), underline=True)

def display(msg: str, tag: str = None):
    output.config(state=tk.NORMAL)
    if tag:
        output.insert(tk.END, msg + "\n", tag)
    else:
        output.insert(tk.END, msg + "\n")

    output.config(state=tk.DISABLED)
    output.see(tk.END)

async def process_input(text: str):
    try:
        display(f"Loading prompt for: {text}")
        lower = text.lower()
        prompt_name = "structured_notes_prompt" if any(w in lower for w in ("guide","tutorial","comprehensive","structured", "notes")) else "default_prompt"
        display(f"Using prompt: {prompt_name}")
        prompts = await load_mcp_prompt(notion, prompt_name, arguments={"message": text})
        display(f"---> Thinking....", "thinking")
        resp = await agent.ainvoke({"messages": prompts})
        ### for debug
        # display(f"Raw agent response: {resp}")
        content = resp.get("messages",[])[-1].content if isinstance(resp, dict) and resp.get("messages") else str(resp)
        display(f"Response:", "response")
        display(f"{content}", "content")
    except McpError as e:
        display(f"[MCP ERROR] {e}")
        display(traceback.format_exc())
    except Exception:
        display("[AGENT ERROR] Encountered an error:")
        display(traceback.format_exc())

def on_send():
    user_text = entry.get().strip()
    if not user_text:
        return
    display(f">> {user_text}")
    entry.delete(0, tk.END)
    asyncio.run_coroutine_threadsafe(process_input(user_text), loop)

button.config(command=on_send)
entry.bind("<Return>", lambda e: on_send())

# Start GUI loop
tk_root.mainloop()
