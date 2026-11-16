# AI Invoice Extraction & Agentic SQL Pipeline

A production-grade, LLM-driven invoice ingestion and database persistence pipeline built on **Python 3.11+**, **FastMCP**, **PostgreSQL 16**, and **SQLGlot**.

<img width="1672" height="941" alt="Self-Correcting LLM Data Pipeline" src="https://github.com/user-attachments/assets/a9bf9d74-11b2-413f-85a6-8ae673e2238d" />

The system extracts structured line-item data from multi-page PDF invoices using layout-preserving parsing, orchestrates an LLM via the **Model Context Protocol (MCP)** to inspect schemas and generate SQL, and enforces multi-layered security guardrails—from AST-level query validation to least-privilege PostgreSQL roles—before any statement touches the database.

---

## Key Architectural Highlights

* **Layout-Preserving PDF Extraction (`pypdf`)**: Uses `extraction_mode="layout"` paired with a structured column parser (`src/extractor/extract_invoice.py`) to retain visual spacing and table alignment, preventing row/column scrambling before LLM ingestion.
* **Zero-Regex AST SQL Guardrail (`sqlglot`)**: Parses every model-generated query into an Abstract Syntax Tree (`src/guardrail/sql_validator.py`) rather than relying on fragile regex matching:
  * Whitelists strictly `SELECT`, `INSERT`, and `UPDATE` statements.
  * Blocks multi-statement injection (`;`), DDL (`DROP`, `ALTER`, `TRUNCATE`), `DELETE`, privilege escalation (`GRANT`, `SET ROLE`), and dangerous CTEs (`WITH ... DELETE/UPDATE`).
  * Enforces a mandatory top-level `WHERE` clause on all `UPDATE` statements.
  * Executes only the normalized SQL emitted by `sqlglot`.
* **Self-Correcting Agentic Execution Loop**: Returns structured execution feedback (`success`, `retryable`, `sqlstate`, and PostgreSQL `diag` constraint/detail/hint messages) so the LLM can autonomously diagnose and fix schema or constraint errors while immediately aborting on non-retryable faults (e.g., `23505` duplicate invoice, `42501` permission denied, `57014` timeout).
* **Layered FastMCP Architecture with Dependency Injection**: Mirrors FastAPI's clean separation of concerns (`Client -> Repository -> Service -> Tool Router -> Main`) using `FastMCP` and `Depends()`, isolating database access from business logic and stripping internal dependencies from tool JSON schemas.
* **Defense-in-Depth PostgreSQL Security**:
  * Normalized relational schema (`vendors`, `invoices`, `line_items`) with UUID primary keys, foreign keys, unique constraints, and financial integrity checks (`subtotal + tax = total`).
  * Least-privilege role separation (`app_write_role` and `encoder`) with role-level `statement_timeout` circuit breakers to kill runaway queries.
* **Provider-Agnostic LLM Client**: Connects to any OpenAI-compatible endpoint (OpenRouter, Ollama Cloud, or local Ollama) via environment variables without code changes.

---

## Project Purpose
This project is a milestone for my study of AI Engineering.

---

## Contributing & Connect

This project is **open source** and **open for contributions**—feel free to open an issue or submit a pull request.

- **Email:** [benjicanones6@gmail.com](mailto:benjicanones6@gmail.com)
- **LinkedIn:** [linkedin.com/in/benji-cañones](https://www.linkedin.com/in/benji-cañones)

---
