import argparse
import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

import mcp.types as mcp_types
from dotenv import load_dotenv
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from openai import OpenAI
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessage,
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)
from pydantic import BaseModel

from client.openai_client import client as default_openai_client
from extractor.extract_invoice import (
    SUPPORTED_TYPES,
    ExtractedDocument,
    ExtractInvoice,
    NoTextLayerError,
    UnsupportedFileTypeError,
)
from services.schema_service import ALLOWED_TABLES
from tools.query_tools import query_mcp
from tools.schema_tools import schema_mcp

logger = logging.getLogger(__name__)

# only in demo. config and add more during prod
MAX_ERROR_RETRIES = 3
MAX_TURNS = 10
DEFAULT_INVOICE_DIR = "../../invoice-dir/"

mcp = FastMCP(name="invoice-extractor-mcp", version="0.1.0")
mcp.mount(schema_mcp)
mcp.mount(query_mcp)


class DocumentResult(BaseModel):
    file_name: str
    success: bool
    message: str | None = None

SYSTEM_PROMPT = f"""You are an agent to the Invoice Extractor company.
Your task is to extract invoice data from the raw text (tables may be deconstructed due to PdfReader.page_extract.extraction_mode="layout") and store it in Postgres.
You have at most {MAX_ERROR_RETRIES} retries per invoice for retryable SQL/database errors.

Available tables: {sorted(ALLOWED_TABLES)}

Workflow:
1. Call `inspect_table_schema` once with all allowed table names (`vendors`, `invoices`, `line_items`) to inspect columns and constraints.
2. For EACH invoice in the document, you MUST insert into all 3 tables (`vendors`, `invoices`, and `line_items`) in ONE single `execute_model_query` call using a `WITH` CTE:
   ```sql
   WITH v AS (
       INSERT INTO vendors (name, tax_id) VALUES (...) RETURNING id
   ),
   i AS (
       INSERT INTO invoices (vendor_id, invoice_number, issue_date, due_date, currency, subtotal, tax, total)
       SELECT v.id, ... FROM v RETURNING id
   )
   INSERT INTO line_items (invoice_id, description, quantity, unit_price, line_total)
   SELECT i.id, item.description, item.quantity, item.unit_price, item.line_total
   FROM i
   CROSS JOIN (VALUES (...), (...)) AS item(description, quantity, unit_price, line_total);
   ```
3. Never skip `line_items`—an invoice is incomplete until its `vendors`, `invoices`, and `line_items` rows are all inserted.
4. If `execute_model_query` returns a retryable error (`retryable=true`), fix your SQL using the returned `notice` and retry.
5. If `execute_model_query` returns a non-retryable error (`retryable=false`), stop and report the failure.
"""


def build_invoice_user_prompt(inv: str, metadata: dict[str, str | int | datetime | None]) -> str:
    """Build the per-document user prompt sent once at the start of a document's conversation."""
    return (
        "Extract and store the following invoice in Postgres.\n\n"
        f"Invoice String:\n{inv}\n\n"
        f"Metadata:\n{metadata}"
    )


def _to_openai_tools(mcp_tools: list[mcp_types.Tool]) -> list[ChatCompletionToolParam]:
    """Convert FastMCP Tool definitions into OpenAI function-calling tool schemas."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.input_schema,
            },
        }
        for tool in mcp_tools
    ]


def _collect_invoice_files(invc_dir: str | Path) -> list[Path]:
    """Discover supported invoice files from a file or directory path."""
    path = Path(invc_dir)
    if not path.exists():
        raise FileNotFoundError(path)

    invc_files: list[Path] = []
    if path.is_dir():
        logger.info("[INFO] Invoice directory is a folder.")
        for file in path.rglob("*"):
            if file.is_file() and file.suffix.lower() in SUPPORTED_TYPES:
                logger.info("[INFO] Found pdf file %s in directory.", file)
                invc_files.append(file.resolve())
    elif path.is_file():
        logger.info("[INFO] Invoice path is a single file.")
        if path.suffix.lower() in SUPPORTED_TYPES:
            invc_files.append(path.resolve())
        else:
            raise UnsupportedFileTypeError(f"Cannot extract file type: {path.suffix.lower()}")

    if not invc_files:
        raise FileNotFoundError("No PDF or invoice file exists.")

    return invc_files


def _extract_documents(invc_files: list[Path]) -> list[ExtractedDocument]:
    """Extract raw text and metadata from each invoice file, skipping scanned/image-only PDFs."""
    extractor = ExtractInvoice()
    docs: list[ExtractedDocument] = []
    for file in invc_files:
        try:
            docs.append(extractor.extract_invoice(str(file)))
        except NoTextLayerError as exc:
            logger.warning("Skipping %s", exc)
    return docs


def _sanitize_tool_name(name: str) -> str:
    """Strip leaked special control tokens (e.g. `<|channel|>`) from model tool names."""
    return re.sub(r"<\|.*?\|>", "", name).strip()


def _format_assistant_message(msg: ChatCompletionMessage) -> ChatCompletionAssistantMessageParam:
    """Serialize an OpenAI chat completion message with tool calls for the next turn."""
    return {
        "role": "assistant",
        "content": msg.content or "",
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": _sanitize_tool_name(tc.function.name),
                    "arguments": tc.function.arguments,
                },
            }
            for tc in (msg.tool_calls or [])
        ],
    }


async def process_document(
    doc: ExtractedDocument,
    mcp_client: Client,
    llm_client: OpenAI,
    openai_tools: list[ChatCompletionToolParam],
    model_name: str,
) -> DocumentResult:
    """Run the LLM <-> MCP tool loop for a single extracted invoice document."""
    doc_metadata: dict[str, str | int | datetime | None] = {
        "file_name": doc.metadata.file_name,
        "title": doc.metadata.title,
        "page_count": doc.metadata.page_count,
        "pdf_creation_date": doc.metadata.pdf_creation_date,
    }
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_invoice_user_prompt(doc.raw_text, doc_metadata)},
    ]

    error_retries = 0
    logger.info("[%s] Starting processing (%d chars).", doc.metadata.file_name, len(doc.raw_text))
    for turn in range(1, MAX_TURNS + 1):
        logger.info("[%s] Turn %d/%d: calling model '%s'...", doc.metadata.file_name, turn, MAX_TURNS, model_name)
        response = llm_client.chat.completions.create(
            model=model_name,
            messages=messages,
            tools=openai_tools,
        )
        msg = response.choices[0].message

        if not msg.tool_calls:
            logger.info("[%s] Finished: %s", doc.metadata.file_name, msg.content)
            return DocumentResult(
                file_name=doc.metadata.file_name,
                success=True,
                message=msg.content,
            )

        messages.append(_format_assistant_message(msg))

        for tc in msg.tool_calls:
            tool_name = _sanitize_tool_name(tc.function.name)
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                logger.warning("[%s] Tool '%s' received invalid JSON args: %s", doc.metadata.file_name, tool_name, exc)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps({"success": False, "notice": f"Invalid JSON arguments: {exc}"}),
                })
                continue

            logger.info("[%s] Calling MCP tool '%s' args=%s", doc.metadata.file_name, tool_name, args)
            try:
                result = await mcp_client.call_tool(tool_name, args)
            except ToolError as exc:
                logger.warning("[%s] MCP ToolError calling '%s': %s", doc.metadata.file_name, tool_name, exc)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps({"success": False, "notice": f"Tool error: {exc}"}),
                })
                continue
            payload: dict[str, object] | None = result.structured_content
            if payload is None and result.content:
                first_block = result.content[0]
                if isinstance(first_block, mcp_types.TextContent):
                    payload = {"text": first_block.text}

            if isinstance(payload, dict):
                logger.info(
                    "[%s] Tool '%s' -> success=%s | notice=%s",
                    doc.metadata.file_name,
                    tool_name,
                    payload.get("success"),
                    payload.get("notice"),
                )

            if tool_name == "execute_model_query" and isinstance(payload, dict) and payload.get("success") is False:
                notice_str = str(payload.get("notice") or "")
                if not payload.get("retryable", False):
                    logger.error("[%s] Non-retryable DB error: %s", doc.metadata.file_name, notice_str)
                    return DocumentResult(
                        file_name=doc.metadata.file_name,
                        success=False,
                        message=notice_str,
                    )

                error_retries += 1
                logger.warning(
                    "[%s] Retryable error (%d/%d): %s",
                    doc.metadata.file_name,
                    error_retries,
                    MAX_ERROR_RETRIES,
                    notice_str,
                )
                payload = {**payload, "retry_count": f"{error_retries} of {MAX_ERROR_RETRIES}"}
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(payload, default=str),
                })

                if error_retries >= MAX_ERROR_RETRIES:
                    logger.error(
                        "[%s] Exhausted %d retries: %s",
                        doc.metadata.file_name,
                        MAX_ERROR_RETRIES,
                        notice_str,
                    )
                    return DocumentResult(
                        file_name=doc.metadata.file_name,
                        success=False,
                        message=notice_str,
                    )
                continue

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(payload, default=str),
            })

    max_turns_msg = f"Reached max turns ({MAX_TURNS}) without completion."
    logger.warning("[%s] %s", doc.metadata.file_name, max_turns_msg)
    return DocumentResult(
        file_name=doc.metadata.file_name,
        success=False,
        message=max_turns_msg,
    )


async def run_pipeline(
    invc_dir: str | Path | None = None,
    llm_client: OpenAI | None = None,
) -> list[DocumentResult]:
    """Extract PDFs and process each document through the LLM + MCP pipeline."""
    load_dotenv()
    target_dir = invc_dir or os.getenv("INVOICE_DIR", DEFAULT_INVOICE_DIR)
    model_name = os.getenv("OLLAMA_MODEL_NAME", "gpt-oss:20b")
    llm = llm_client or default_openai_client

    logger.info("Scanning for invoices in: %s", target_dir)
    invc_files = _collect_invoice_files(target_dir)
    logger.info("Extracting text from %d PDF file(s)...", len(invc_files))
    extracted_docs = _extract_documents(invc_files)
    logger.info("Extracted %d document(s). Starting pipeline (model=%s).", len(extracted_docs), model_name)

    results: list[DocumentResult] = []
    async with Client(mcp) as mcp_client:
        openai_tools = _to_openai_tools(await mcp_client.list_tools())
        logger.info("Loaded %d MCP tool(s): %s", len(openai_tools), [t["function"]["name"] for t in openai_tools])
        for doc in extracted_docs:
            results.append(
                await process_document(
                    doc=doc,
                    mcp_client=mcp_client,
                    llm_client=llm,
                    openai_tools=openai_tools,
                    model_name=model_name,
                )
            )
    succeeded = sum(1 for r in results if r.success)
    logger.info("Pipeline finished: %d/%d document(s) succeeded.", succeeded, len(results))
    return results


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    parser = argparse.ArgumentParser(description="Invoice Extractor MCP & Pipeline")
    parser.add_argument("--serve", action="store_true", help="Run as standalone FastMCP server.")
    parser.add_argument("--dir", dest="invc_dir", default=None, help="Invoice PDF file or directory.")
    cli_args = parser.parse_args()

    if cli_args.serve:
        mcp.run()
    else:
        asyncio.run(run_pipeline(invc_dir=cli_args.invc_dir))
