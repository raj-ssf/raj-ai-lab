import uvicorn
from fastapi import FastAPI
from sentence_transformers import SentenceTransformer

app = FastAPI(title="Embedding Service")

# Load from local path (baked into image)
model = SentenceTransformer("/app/model")

@app.get("/health")
def health():
    return {"status": "ok", "model": "all-MiniLM-L6-v2"}

@app.post("/embed")
def embed(req: dict):
    text = req.get("inputs", "")
    if isinstance(text, str):
        text = [text]
    embeddings = model.encode(text).tolist()
    return embeddings

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
