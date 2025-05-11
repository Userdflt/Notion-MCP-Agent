import os
import json
from dotenv import load_dotenv

from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.server.fastmcp.prompts import base

from notion_client import Client
from notion_client.helpers import iterate_paginated_api
from notion_client.helpers import pick

from typing import Any, Dict, List, Optional

# ── Load environment ─────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("NOTION_TOKEN")
if not TOKEN:
    raise ValueError("NOTION_TOKEN must be set")

# ── Initialize Notion & MCP ──────────────────────────────────
notion = Client(auth=TOKEN)
mcp = FastMCP("Notion")

# ── Retrieve page text ───────────────────────────────────────────────


################################################################
# Tools (write & metadata ops)
################################################################

# ── Create Tools ───────────────────────────────
    
@mcp.tool()
def append_content(
    page_id: str,
    markdown: str,
    after: Optional[str] = None
) -> Dict[str, Any]:
    """
    PATCH /v1/blocks/{page_id}/children
    Parse a Markdown-like string into Notion blocks (headings, bullets, tables, paragraphs),
    and append them to the given page (or block).

    Args:
      page_id:   ID of the page (or block) to append into.
      markdown:  String using:
                   - `# ` for H1, `## ` for H2, etc.
                   - `- ` or `* ` for bullets
                   - Tables with `|` separators, header row first.
      after:     (optional) block_id after which to insert; if omitted, appends to the end.

    Returns:
      The Notion API response for the appended blocks.
    """
    try:
        lines = markdown.splitlines()
        children: List[Dict[str, Any]] = []
        table_buffer: List[List[str]] = []
        in_table = False

        def flush_table():
            nonlocal table_buffer, children, in_table
            if not table_buffer:
                return
            # build a single table block with rows
            width = len(table_buffer[0])
            table_block = {
                "object": "block",
                "type": "table",
                "table": {
                    "table_width": width,
                    "has_column_header": True,
                    "has_row_header": False,
                    "children": []
                }
            }
            for row in table_buffer:
                cells = [[{"type": "text", "text": {"content": cell.strip()}}]
                         for cell in row]
                row_block = {
                    "object": "block",
                    "type": "table_row",
                    "table_row": {"cells": cells}
                }
                table_block["table"]["children"].append(row_block)
            children.append(table_block)
            table_buffer = []
            in_table = False

        for line in lines:
            # detect table rows (must contain |)
            if "|" in line:
                parts = [c for c in (cell.strip() for cell in line.split("|")) if c]
                table_buffer.append(parts)
                in_table = True
                continue

            # if we were collecting a table and now hit non-table, flush it
            if in_table:
                flush_table()

            # headings
            if line.startswith("#"):
                level = len(line) - len(line.lstrip("#"))
                text = line[level:].strip()
                children.append({
                    "object": "block",
                    "type": f"heading_{level}",
                    f"heading_{level}": {
                        "rich_text": [{"type": "text", "text": {"content": text}}]
                    }
                })
            # bullet list
            elif line.startswith(("- ", "* ")):
                text = line[2:].strip()
                children.append({
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": [{"type": "text", "text": {"content": text}}]
                    }
                })
            # blank line: skip
            elif not line.strip():
                continue
            # fallback: paragraph
            else:
                children.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": line}}]
                    }
                })

        # flush any trailing table
        if in_table:
            flush_table()

        body: Dict[str, Any] = {"children": children}
        if after:
            body["after"] = after

        return notion.request(
            path=f"blocks/{page_id}/children",
            method="PATCH",
            body=body
        )

    except Exception as e:
        raise McpError(f"Append content failed: {e}")
    
@mcp.tool()
def create_table(
    page_id: str,
    rows: List[List[str]],
    has_column_header: bool = True,
    has_row_header: bool    = False,
    after: Optional[str]    = None
) -> Dict[str, Any]:
    """
    Create a table in the given page (or block).

    Args:
      page_id:            ID of the page (or block) to append the table to.
      rows:               A list of rows, each itself a list of cell strings.
      has_column_header:  Whether the first row is a header row.
      has_row_header:     Whether the first column is a header column.
      after:              (optional) block_id after which to insert; defaults to end.

    Returns:
      The Notion API response for the appended blocks (the table and its rows).
    """
    try:
        # Determine table width from number of columns in the first row
        width = len(rows[0]) if rows and rows[0] else 0

        # Build the table block with its rows as children
        table_block: Dict[str, Any] = {
            "object": "block",
            "type": "table",
            "table": {
                "table_width": width,
                "has_column_header": has_column_header,
                "has_row_header": has_row_header,
                "children": []
            }
        }

        # Create each row
        for row in rows:
            # Each cell becomes a rich_text token
            cells = [[{"type": "text", "text": {"content": cell}}] for cell in row]
            row_block = {
                "object": "block",
                "type": "table_row",
                "table_row": {
                    "cells": cells
                }
            }
            table_block["table"]["children"].append(row_block)

        # Wrap into the append request
        body: Dict[str, Any] = {"children": [table_block]}
        if after is not None:
            body["after"] = after

        # PATCH /v1/blocks/{page_id}/children
        return notion.request(
            path=f"blocks/{page_id}/children",
            method="PATCH",
            body=body
        )
    except Exception as e:
        raise McpError(f"Create table failed: {e}")
    
# ── PagesPropertiesEndpoint → retrieve ────────────────────────────────────────

@mcp.tool()
def get_page_text(page_id: str) -> str:
    """
    Recursively extract *all* visible text from a Notion page:
      - paragraphs, headings (1–3), lists, blockquotes, callouts, code, etc.
      - recurses into any child_page blocks
    """
    try:
        parts: list[str] = []

        for block in iterate_paginated_api(
            notion.blocks.children.list,
            block_id=page_id,
            page_size=100
        ):
            btype = block.get("type", "")
            data = block.get(btype, {})

            # 1) Headings
            if btype.startswith("heading_"):
                level = btype.split("_", 1)[1]   # e.g. "1", "2", "3"
                text = "".join(tok["plain_text"] for tok in data.get("rich_text", []))
                parts.append(f"{'#'*int(level)} {text}")

            # 2) Lists (bulleted or numbered)
            elif btype in ("bulleted_list_item", "numbered_list_item"):
                text = "".join(tok["plain_text"] for tok in data.get("rich_text", []))
                prefix = "-" if btype=="bulleted_list_item" else "1."
                parts.append(f"{prefix} {text}")

            # 3) Callouts, quotes, to-dos, toggles, paragraphs, code, etc.
            else:
                tokens = data.get("rich_text") or data.get("text") or []
                if tokens:
                    parts.append("".join(tok.get("plain_text","") for tok in tokens))

            # 4) Recurse into sub-pages
            if btype == "child_page":
                title = data.get("title", "<no title>")
                sub_id = block["id"]
                sub_text = get_page_text(sub_id)
                parts.append(f"\n--- Sub-page: {title} ({sub_id}) ---\n{sub_text}")

        return "\n\n".join(parts)

    except Exception as e:
        raise McpError(f"Error reading page text: {e}")
    
@mcp.tool()
def retrieve_page_property(
    page_id:     str,
    property_id: str,
    start_cursor: Optional[str] = None,
    page_size:    Optional[int] = None,
) -> Dict[str, Any]:
    """
    GET /v1/pages/{page_id}/properties/{property_id}
    *Retrieve a single page property’s items, paginated.*
    """
    try:
        return notion.pages.properties.retrieve(
            page_id=page_id,
            property_id=property_id,
            start_cursor=start_cursor,
            page_size=page_size,
        )
    except Exception as e:
        raise McpError(f"Retrieve page property failed: {e}")


# ── PagesEndpoint → create, retrieve, update ─────────────────────────────────
@mcp.tool()
def update_page_title(
    page_id: str,
    new_title: str
) -> Dict[str, Any]:
    """
    PATCH /v1/pages/{page_id}
    Update the title of the specified page.

    Args:
      page_id:   The ID of the page whose title you want to change.
      new_title: The new title text for the page.

    Returns:
      The updated Page object.
    """
    try:
        body = {
            "properties": {
                "title": [
                    {
                        "type": "text",
                        "text": {"content": new_title}
                    }
                ]
            }
        }
        return notion.request(
            path=f"pages/{page_id}",
            method="PATCH",
            body=body
        )
    except Exception as e:
        raise McpError(f"Update page title failed: {e}")

@mcp.tool()
def create_subpage(
    page_id: str,
    title: str,
    icon: Optional[Dict[str, Any]]  = None,
    cover: Optional[Dict[str, Any]] = None,
    children: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    POST /v1/pages
    Create a new sub-page under the given page.

    Args:
      page_id:   ID of the parent page under which to create this sub-page.
      title:     The title text of the new page.
      icon:      (optional) {"type":"emoji","emoji":"…"} or {"type":"external","external":{"url":"…"}}
      cover:     (optional) {"type":"external","external":{"url":"…"}}
      children:  (optional) list of block-objects to populate initial content

    Returns:
      The newly created Page object.
    """
    try:
        # build parent spec
        parent = {"type": "page_id", "page_id": page_id}

        # required title property
        properties = {
            "title": [
                {
                    "type": "text",
                    "text": {"content": title}
                }
            ]
        }

        body: Dict[str, Any] = {
            "parent": parent,
            "properties": properties
        }
        if icon:
            body["icon"] = icon
        if cover:
            body["cover"] = cover
        if children:
            body["children"] = children

        # call the Notion API
        return notion.request(
            path="pages",
            method="POST",
            body=body
        )
    except Exception as e:
        raise McpError(f"Create subpage failed: {e}")

@mcp.tool()
def retrieve_page(
    page_id:           str,
    filter_properties: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    GET /v1/pages/{page_id}
    *Retrieve a page’s properties (no block content).*
    """
    try:
        return notion.pages.retrieve(
            page_id=page_id,
            filter_properties=filter_properties,
        )
    except Exception as e:
        raise McpError(f"Retrieve page failed: {e}")

@mcp.tool()
def update_page(
    page_id:     str,
    in_trash:    Optional[bool]           = None,
    archived:    Optional[bool]           = None,
    properties:  Optional[Dict[str, Any]] = None,
    icon:        Optional[Dict[str, Any]] = None,
    cover:       Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    PATCH /v1/pages/{page_id}
    *Update a page’s properties, icon, cover, or archive state.*
    """
    try:
        body = pick(
            locals(),
            "in_trash","archived","properties","icon","cover"
        )
        return notion.pages.update(page_id=page_id, **body)
    except Exception as e:
        raise McpError(f"Update page failed: {e}")


# ── UsersEndpoint → list, retrieve, me ───────────────────────────────────────

@mcp.tool()
def list_users(
    start_cursor: Optional[str] = None,
    page_size:    Optional[int] = None,
) -> Dict[str, Any]:
    """
    GET /v1/users
    *List all users in the workspace.*
    """
    try:
        return notion.users.list(
            start_cursor=start_cursor,
            page_size=page_size,
        )
    except Exception as e:
        raise McpError(f"List users failed: {e}")

@mcp.tool()
def retrieve_user(
    user_id: str
) -> Dict[str, Any]:
    """
    GET /v1/users/{user_id}
    *Retrieve a user by ID.*
    """
    try:
        return notion.users.retrieve(user_id=user_id)
    except Exception as e:
        raise McpError(f"Retrieve user failed: {e}")

@mcp.tool()
def get_me() -> Dict[str, Any]:
    """
    GET /v1/users/me
    *Retrieve the integration’s bot user.*
    """
    try:
        return notion.users.me()
    except Exception as e:
        raise McpError(f"Retrieve bot user failed: {e}")


# ── SearchEndpoint → __call__ ────────────────────────────────────────────────

@mcp.tool()
def search_notion(
    query: str,
    sort: Optional[Dict[str, Any]],
    filter: Optional[Dict[str, Any]],
    start_cursor: Optional[str],
    page_size: Optional[int],
) -> List[Dict[str, Any]]:
    """
    POST /v1/search
    Search all pages and databases shared with the integration.

    Args:
      query: text to search for
      sort: {"direction": "ascending"|"descending", "timestamp": "<created_time|last_edited_time>"}
      filter: {"property": <property_name>, "value": <value>}
      start_cursor: pagination cursor
      page_size: number of items per page

    Returns:
      List of matching page/database objects.
    """
    try:
        
        body: Dict[str, Any] = {"query": query}
        if sort is not None:
            body["sort"] = sort
        if filter is not None:
            body["filter"] = filter
        if start_cursor is not None:
            body["start_cursor"] = start_cursor
        if page_size is not None:
            body["page_size"] = page_size

       
        resp = notion.request(path="search", method="POST", body=body)
        return resp.get("results", [])
    except Exception as e:
        raise McpError(f"Search failed: {e}")


# ── PromptEndpoint → default_prompt + structured notes prompt ──────────────────────────────────────────

@mcp.prompt()
def default_prompt(message: str) -> list[base.Message]:
    return [
        base.AssistantMessage(
            "You are an AI-powered Notion assistant.  "
            "You have these MCP tools available:\n"
            "  • read_page(page_id): fetch all text from a Notion page\n"
            "  • create_page(input): create a new page under a database or page\n"
            "  • search_notion(query)\n"
            "  • list_child_pages(page_id)\n"
            "  • append_blocks(page_id, blocks)\n"
            "  • update_block(block_id, text)\n"
            "  • update_page_title(page_id, new_title)\n"
            "  • etc.\n\n"
            "When you receive a user request, plan which tool(s) to call and with what JSON arguments, "
            "then return a JSON object with two keys:\n"
            "  • actions: an array of tool‐call objects `{\"tool\": name, \"input\": args}`\n"
            "  • results: the final summary, confirmation, or data output\n"
        ),
        base.UserMessage(message),
    ]

@mcp.prompt()
def structured_notes_prompt(message: str) -> list[base.Message]:
    """
    Build a chain-of-thought, deeply structured guide, 
    following the user’s 10-step “structured notes” specification.
    """
    return [
        base.AssistantMessage(
            "ANALYZE AND RESPOND TO ALL QUERIES IN A CHAIN OF THOUGHT MANNER.\n\n"
            "### 1. Title and Headings\n"
            "- **Title**: Begin with a clear, descriptive title reflecting the topic.\n"
            "- **Table of Contents**: Numbered sections/subsections for easy navigation.\n"
            "- **Headings/Subheadings**: Use consistent, logical organization.\n\n"
            "### 2. Introduction\n"
            "- **Overview**: Provide a high-level summary of the topic.\n"
            "- **Importance**: Explain why it matters and real-world applications.\n"
            "- **Context**: Set the scene with practical scenarios.\n\n"
            "### 3. Objectives\n"
            "- List clear learning goals as bullet points at the start.\n\n"
            "### 4. Theoretical Background\n"
            "- Explain core concepts, include LaTeX‐formatted formulas.\n"
            "- Break down each formula, define all variables.\n"
            "- Use analogies or simple examples to clarify complex ideas.\n\n"
            "### 5. Practical Implementation\n"
            "- **Code Examples**: Python snippets using OpenCV, NumPy, Matplotlib.\n"
            "- **Step-by-Step**: Comment every line, explain parameters and purpose.\n"
            "- **Testing**: Ensure snippets run error-free; note prerequisites.\n\n"
            "### 6. Visualizations\n"
            "- Describe expected outputs or ASCII sketches.\n"
            "- Explain how to interpret plots, histograms, images.\n\n"
            "### 7. Applications and Use Cases\n"
            "- Provide domain-specific examples (medical imaging, robotics, etc.).\n"
            "- Include mini case studies illustrating real use.\n\n"
            "### 8. Best Practices and Tips\n"
            "- Offer guidelines, parameter-selection advice, common pitfalls.\n"
            "- Discuss performance optimizations and trade-offs.\n\n"
            "### 9. Conclusion\n"
            "- Summarize key takeaways.\n"
            "- Suggest next steps or advanced topics for further study.\n\n"
            "### 10. References\n"
            "- Cite sources in proper format; link docs and papers.\n\n"
            "### Formatting & Style\n"
            "- Use bullet/numbered lists, clearly distinguish headings.\n"
            "- Syntax-highlight code blocks, separate them from text.\n"
            "- Maintain professional, educational tone; define terms.\n"
            "- Verify accuracy; avoid hallucinations.\n"
            "- (Optional) Add exercises with hints/solutions for engagement.\n\n"
            "Now, create your guide according to these instructions."
        ),
        base.UserMessage(message),
    ]

if __name__ == "__main__":
    mcp.run(transport="sse")

################################################################
# Quick local test for extraction of page content - 
# DO NOT RUN IF A LOT OF CONTENT
################################################################

# if __name__ == "__main__":
#     # fold in SSE transport automatically
#     # but also test get_page_text on your default PAGE_ID
#     test_id = os.getenv("PAGE_ID")
#     if test_id:
#         text = get_page_text(test_id)
#         print(f"\n\n=== CONTENT of {test_id} ===\n\n{text}\n")
#     mcp.run(transport="sse")
