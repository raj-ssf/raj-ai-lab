import os

import fitz  # PyMuPDF
import gradio as gr
import httpx

RAG_URL = os.environ.get("RAG_URL", "http://localhost:8000")

def ingest_document(text, source_name):
    if not text.strip():
        return "Please enter some text to ingest."
    if not source_name.strip():
        source_name = "uploaded-document"
    try:
        r = httpx.post(f"{RAG_URL}/ingest", json={"source": source_name, "text": text}, timeout=60)
        result = r.json()
        return f"✓ {result.get('status','done')} — doc_id: {result.get('doc_id','?')} — Pipeline: Normalizer → Chunker → Embedder will process it."
    except Exception as e:
        return f"Error: {e}"

def extract_pdf_text(file_path):
    """Extract text from PDF using PyMuPDF"""
    doc = fitz.open(file_path)
    pages = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text()
        if text.strip():
            pages.append(f"[Page {page_num + 1}]\n{text.strip()}")
    doc.close()
    return "\n\n".join(pages)

def ingest_file(file, source_name):
    if file is None:
        return "Please upload a file.", ""

    file_path = file.name if hasattr(file, 'name') else str(file)
    filename = os.path.basename(file_path)

    if not source_name.strip():
        source_name = filename

    # Extract text based on file type
    if filename.lower().endswith('.pdf'):
        try:
            text = extract_pdf_text(file_path)
            if not text.strip():
                return "PDF appears to be empty or image-only (no extractable text).", ""
        except Exception as e:
            return f"Error reading PDF: {e}", ""
    else:
        # Text-based files
        try:
            with open(file_path, "r", errors="ignore") as f:
                text = f.read()
        except Exception as e:
            return f"Error reading file: {e}", ""

    if not text.strip():
        return "File appears to be empty.", ""

    # Show preview
    preview = text[:500] + "..." if len(text) > 500 else text

    # Ingest
    result = ingest_document(text, source_name)
    return result, f"Extracted {len(text)} characters from {filename}\n\nPreview:\n{preview}"

def query_rag(question):
    if not question.strip():
        return "", ""
    try:
        r = httpx.post(f"{RAG_URL}/query", json={"question": question}, timeout=180)
        result = r.json()
        answer = result.get("answer", "No answer")
        sources = result.get("sources", [])
        cached = result.get("cached", False)
        query_id = result.get("query_id", "?")

        source_text = ""
        for s in sources:
            source_text += f"- {s['source']} (score: {s['score']:.3f})\n"
        if cached:
            source_text += "\n(served from cache)"
        source_text += f"\nQuery ID: {query_id}"

        return answer, source_text
    except Exception as e:
        return f"Error: {e}", ""

with gr.Blocks(title="Raj AI Lab", theme=gr.themes.Soft()) as app:
    gr.Markdown("# Raj AI Lab — RAG Pipeline")
    gr.Markdown("Ingest documents and query them using the full microservice pipeline: **Normalizer → Chunker → Embedder → Qdrant → LLM**")

    with gr.Tab("🔍 Query"):
        question = gr.Textbox(label="Ask a question", placeholder="What is the torque spec for Model X?", lines=2)
        query_btn = gr.Button("Ask", variant="primary")
        answer = gr.Textbox(label="Answer", lines=8)
        sources = gr.Textbox(label="Sources & Metadata", lines=4)
        query_btn.click(query_rag, inputs=question, outputs=[answer, sources])

    with gr.Tab("📝 Ingest Text"):
        source_name = gr.Textbox(label="Document name", placeholder="maintenance-manual.pdf")
        doc_text = gr.Textbox(label="Paste document text", lines=15, placeholder="Paste your document content here...")
        ingest_btn = gr.Button("Ingest", variant="primary")
        ingest_result = gr.Textbox(label="Result")
        ingest_btn.click(ingest_document, inputs=[doc_text, source_name], outputs=ingest_result)

    with gr.Tab("📄 Upload File"):
        gr.Markdown("Supports: **PDF**, TXT, MD, CSV, JSON, XML, HTML")
        file_source = gr.Textbox(label="Document name (optional)", placeholder="auto-detected from filename")
        file_upload = gr.File(label="Upload a file", file_types=[".pdf", ".txt", ".md", ".csv", ".json", ".xml", ".html", ".log", ".yaml", ".yml"])
        upload_btn = gr.Button("Upload & Ingest", variant="primary")
        upload_result = gr.Textbox(label="Result")
        upload_preview = gr.Textbox(label="Extracted Text Preview", lines=10)
        upload_btn.click(ingest_file, inputs=[file_upload, file_source], outputs=[upload_result, upload_preview])

if __name__ == "__main__":
    app.launch(server_name="0.0.0.0", server_port=7860)
