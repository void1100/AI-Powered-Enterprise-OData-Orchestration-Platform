"""Download ChromaDB ONNX model during Docker build."""
import os
import sys

CACHE_DIR = os.environ.get("CHROMA_ONNX_CACHE", "/root/.cache/chroma/onnx_models/all-MiniLM-L6-v2")
MODEL_URL = "https://chroma-onnx-models.s3.amazonaws.com/all-MiniLM-L6-v2/onnx.tar.gz"

def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    target = os.path.join(CACHE_DIR, "onnx.tar.gz")
    if os.path.exists(target) and os.path.getsize(target) > 1_000_000:
        print(f"ONNX model already exists at {target} ({os.path.getsize(target)} bytes), skipping download.")
        return

    print(f"Downloading ONNX model to {target} ...")
    try:
        import urllib.request
        urllib.request.urlretrieve(MODEL_URL, target)
        size = os.path.getsize(target)
        print(f"Downloaded ONNX model: {size} bytes")
    except Exception as e:
        print(f"WARNING: Failed to download ONNX model: {e}", file=sys.stderr)
        print("ChromaDB embeddings will be disabled at runtime.", file=sys.stderr)

if __name__ == "__main__":
    main()
