import os

import uvicorn
from fastapi import FastAPI
from sentence_transformers import SentenceTransformer

app = FastAPI(title="Embedding Service")

MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
CACHE_DIR = os.environ.get("MODEL_CACHE_DIR", "/app/model-cache")

print(f"[embedding] Loading model: {MODEL_NAME}")
print(f"[embedding] Cache dir: {CACHE_DIR}")

# Resolve model path: check PVC cache first, then baked-in, then download
model_short_name = MODEL_NAME.split("/")[-1] if "/" in MODEL_NAME else MODEL_NAME
cached_path = os.path.join(CACHE_DIR, "sentence-transformers", model_short_name)

if os.path.exists(os.path.join(cached_path, "config.json")):
    print(f"[embedding] Loading from PVC cache: {cached_path}")
    model = SentenceTransformer(cached_path)
elif os.path.exists("/app/model/config.json"):
    print("[embedding] Using baked-in model from /app/model")
    model = SentenceTransformer("/app/model")
else:
    print(f"[embedding] Downloading {MODEL_NAME} to {CACHE_DIR}")
    os.makedirs(CACHE_DIR, exist_ok=True)
    os.environ["TRANSFORMERS_CACHE"] = CACHE_DIR
    os.environ["HF_HOME"] = CACHE_DIR
    model = SentenceTransformer(MODEL_NAME, cache_folder=CACHE_DIR)

DIMENSIONS = model.get_sentence_embedding_dimension()
print(f"[embedding] Model loaded: {MODEL_NAME}, dimensions: {DIMENSIONS}")

@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_NAME, "dimensions": DIMENSIONS}

@app.post("/embed")
def embed(req: dict):
    text = req.get("inputs", "")
    if isinstance(text, str):
        text = [text]
    embeddings = model.encode(text).tolist()
    return embeddings

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
