"""
document_rag.py — Local Document & Knowledge RAG for MARK LV.

Provides local document ingestion, semantic chunking, and fast retrieval over:
- PDFs (via pdfplumber / PyPDF2)
- Word documents (.docx via python-docx)
- PowerPoint presentations (.pptx via python-pptx)
- Plain text, Markdown, CSV, and code repositories (.py, .js, .json, .md, .txt)

Enables Mark to answer questions and cite facts directly from local user documents.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent
else:
    _BASE = Path(__file__).resolve().parent.parent

_STORE_PATH = _BASE / "memory" / "document_rag.json"
_CHUNK_SIZE = 500       # target characters per chunk
_CHUNK_OVERLAP = 100    # overlap characters between consecutive chunks

# File readers
try:
    import pdfplumber
    _PDFPLUMBER = True
except ImportError:
    _PDFPLUMBER = False

try:
    import docx
    _DOCX = True
except ImportError:
    _DOCX = False

try:
    import pptx
    _PPTX = True
except ImportError:
    _PPTX = False


def _tokenize(text: str) -> list[str]:
    """Tokenize and normalize text into lowercase alphanumeric stems."""
    return re.findall(r"\b[a-zA-Z0-9_]{2,}\b", text.lower())


def _read_file_text(path: Path) -> str:
    """Read text from file depending on extension."""
    suffix = path.suffix.lower()
    
    if suffix == ".pdf":
        if _PDFPLUMBER:
            chunks = []
            with pdfplumber.open(path) as pdf:
                for i, page in enumerate(pdf.pages):
                    t = page.extract_text()
                    if t:
                        chunks.append(f"[Page {i+1}]\n{t}")
            return "\n\n".join(chunks)
        else:
            return path.read_text(encoding="utf-8", errors="ignore")
            
    elif suffix in (".docx", ".doc") and _DOCX:
        doc = docx.Document(path)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        
    elif suffix in (".pptx", ".ppt") and _PPTX:
        prs = pptx.Presentation(path)
        slides_text = []
        for i, slide in enumerate(prs.slides):
            st = []
            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text.strip():
                    st.append(shape.text.strip())
            if st:
                slides_text.append(f"[Slide {i+1}]\n" + "\n".join(st))
        return "\n\n".join(slides_text)
        
    else:
        # Source code, markdown, txt, csv, json
        return path.read_text(encoding="utf-8", errors="ignore")


def _chunk_text(text: str, source_name: str) -> list[dict]:
    """Split text into overlapping chunks with metadata."""
    lines = text.splitlines()
    chunks = []
    current_chunk = []
    current_len = 0
    
    for line in lines:
        line_str = line.strip()
        if not line_str:
            continue
            
        current_chunk.append(line_str)
        current_len += len(line_str) + 1
        
        if current_len >= _CHUNK_SIZE:
            full_str = " ".join(current_chunk)
            chunks.append({
                "source": source_name,
                "text": full_str,
                "tokens": _tokenize(full_str)
            })
            # Keep tail for overlap
            overlap_words = full_str[-_CHUNK_OVERLAP:].split()
            current_chunk = overlap_words
            current_len = sum(len(w) + 1 for w in overlap_words)
            
    if current_chunk:
        full_str = " ".join(current_chunk)
        chunks.append({
            "source": source_name,
            "text": full_str,
            "tokens": _tokenize(full_str)
        })
    return chunks


def _load_rag_store() -> dict:
    if _STORE_PATH.exists():
        try:
            return json.loads(_STORE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {"documents": {}, "chunks": []}
    return {"documents": {}, "chunks": []}


def _save_rag_store(store: dict) -> None:
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STORE_PATH.write_text(json.dumps(store, indent=2), encoding="utf-8")


def index_file(file_path: str) -> str:
    """Index a single file into the knowledge base."""
    p = Path(file_path).expanduser().resolve()
    if not p.exists() or not p.is_file():
        return f"File '{file_path}' does not exist or is not a valid file."
        
    store = _load_rag_store()
    doc_key = str(p)
    
    try:
        content = _read_file_text(p)
        if not content.strip():
            return f"No readable text found in '{p.name}'."
            
        file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        
        # Check if already indexed with same content
        if doc_key in store["documents"] and store["documents"][doc_key].get("hash") == file_hash:
            return f"'{p.name}' is already indexed and up-to-date."
            
        # Remove old chunks for this document if re-indexing
        store["chunks"] = [c for c in store["chunks"] if c.get("source") != str(p)]
        
        chunks = _chunk_text(content, str(p))
        store["chunks"].extend(chunks)
        store["documents"][doc_key] = {
            "name": p.name,
            "hash": file_hash,
            "chunk_count": len(chunks),
            "size_chars": len(content)
        }
        _save_rag_store(store)
        return f"Successfully indexed '{p.name}' into knowledge base ({len(chunks)} chunks, {len(content)} characters)."
    except Exception as e:
        return f"Failed to index '{p.name}': {e}"


def index_directory(directory_path: str, max_files: int = 50) -> str:
    """Index all supported documents in a folder."""
    d = Path(directory_path).expanduser().resolve()
    if not d.exists() or not d.is_dir():
        return f"Directory '{directory_path}' does not exist."
        
    supported_exts = {".pdf", ".docx", ".pptx", ".txt", ".md", ".py", ".json", ".csv"}
    indexed_count = 0
    skipped_count = 0
    
    for root, _, files in os.walk(d):
        for f in files:
            fp = Path(root) / f
            if fp.suffix.lower() in supported_exts:
                if indexed_count >= max_files:
                    break
                res = index_file(str(fp))
                if "Successfully indexed" in res:
                    indexed_count += 1
                else:
                    skipped_count += 1
        if indexed_count >= max_files:
            break
            
    return f"Directory scan complete: {indexed_count} files newly indexed, {skipped_count} skipped/cached."


def query_knowledge(query: str, top_k: int = 3) -> str:
    """Search knowledge base using BM25-style scoring."""
    store = _load_rag_store()
    chunks = store.get("chunks", [])
    if not chunks:
        return "Knowledge base is currently empty. Use 'index_file' or 'index_directory' to add documents."
        
    query_tokens = _tokenize(query)
    if not query_tokens:
        return "Query contains no searchable words."
        
    # BM25-style scoring
    total_docs = len(chunks)
    avg_dl = sum(len(c.get("tokens", [])) for c in chunks) / max(1, total_docs)
    k1 = 1.5
    b = 0.75
    
    # Calculate document frequencies
    df: Counter[str] = Counter()
    for c in chunks:
        unique_tokens = set(c.get("tokens", []))
        for t in query_tokens:
            if t in unique_tokens:
                df[t] += 1
                
    scores = []
    for c in chunks:
        doc_tokens = c.get("tokens", [])
        dl = len(doc_tokens)
        doc_counts = Counter(doc_tokens)
        score = 0.0
        
        for q in query_tokens:
            if q in doc_counts:
                tf = doc_counts[q]
                doc_freq = df.get(q, 0)
                idf = math.log(1.0 + (total_docs - doc_freq + 0.5) / (doc_freq + 0.5))
                score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * (dl / avg_dl)))
                
        if score > 0.1:
            scores.append((score, c))
            
    scores.sort(key=lambda x: x[0], reverse=True)
    top_results = scores[:top_k]
    
    if not top_results:
        return f"No relevant passages found for query '{query}' in indexed documents."
        
    output_lines = [f"Found {len(top_results)} relevant passage(s):"]
    for idx, (sc, chunk) in enumerate(top_results, 1):
        src_name = Path(chunk.get("source", "Unknown")).name
        snippet = chunk.get("text", "").strip()
        output_lines.append(f"\n--- [Result {idx} from {src_name} (Score: {sc:.2f})] ---")
        output_lines.append(snippet)
        
    return "\n".join(output_lines)


def list_indexed_docs() -> str:
    store = _load_rag_store()
    docs = store.get("documents", {})
    if not docs:
        return "Knowledge base is currently empty."
        
    lines = [f"Indexed Documents ({len(docs)} files, {len(store.get('chunks', []))} total chunks):"]
    for path, info in docs.items():
        name = info.get("name", Path(path).name)
        chunks = info.get("chunk_count", 0)
        lines.append(f"• {name}: {chunks} chunks [{path}]")
    return "\n".join(lines)


def clear_knowledge() -> str:
    _save_rag_store({"documents": {}, "chunks": []})
    return "Knowledge base index has been cleared."


def document_rag(parameters: dict, player=None, session_memory=None) -> str:
    action = parameters.get("action", "").lower().strip()
    
    if action in ("query", "search", "ask"):
        q = parameters.get("query", "")
        top_k = int(parameters.get("top_k", 3))
        return query_knowledge(q, top_k=top_k)
    elif action in ("index_file", "index"):
        p = parameters.get("path", "")
        return index_file(p)
    elif action in ("index_dir", "index_folder", "index_directory"):
        p = parameters.get("path", "")
        max_f = int(parameters.get("max_files", 30))
        return index_directory(p, max_files=max_f)
    elif action in ("list", "status", "list_docs"):
        return list_indexed_docs()
    elif action in ("clear", "reset"):
        return clear_knowledge()
    else:
        return f"Unknown document_rag action '{action}'. Available: query | index_file | index_dir | list | clear"
    action = params.get("action", "").lower().strip()
    
    if action in ("query", "search", "ask"):
        q = params.get("query", "")
        top_k = int(params.get("top_k", 3))
        return query_knowledge(q, top_k=top_k)
    elif action in ("index_file", "index"):
        p = params.get("path", "")
        return index_file(p)
    elif action in ("index_dir", "index_folder", "index_directory"):
        p = params.get("path", "")
        max_f = int(params.get("max_files", 30))
        return index_directory(p, max_files=max_f)
    elif action in ("list", "status", "list_docs"):
        return list_indexed_docs()
    elif action in ("clear", "reset"):
        return clear_knowledge()
    else:
        return f"Unknown document_rag action '{action}'. Available: query | index_file | index_dir | list | clear"


TOOL = {
    "name": "document_rag",
    "description": "Local Document & Knowledge Engine: ingest PDFs, Word documents, PowerPoint, code files and search them with semantic retrieval and citations.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "query | index_file | index_dir | list | clear"
            },
            "query": {
                "type": "STRING",
                "description": "Natural language question or search phrase for 'query'"
            },
            "path": {
                "type": "STRING",
                "description": "File or folder path to index"
            },
            "top_k": {
                "type": "INTEGER",
                "description": "Maximum number of relevant passages to return (default: 3)"
            },
            "max_files": {
                "type": "INTEGER",
                "description": "Maximum files to index in a directory scan (default: 30)"
            }
        },
        "required": ["action"]
    },
    "handler": document_rag,
}
