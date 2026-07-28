import os
import glob
import requests
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from dotenv import load_dotenv

load_dotenv()

SERVER_IP = os.getenv("SERVER_IP", "127.0.0.1")
OLLAMA_PORT = os.getenv("OLLAMA_PORT", "11434")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))

def get_embedding(text):
    url = f"http://{SERVER_IP}:{OLLAMA_PORT}/api/embeddings"
    payload = {
        "model": "nomic-embed-text",
        "prompt": text
    }
    response = requests.post(url, json=payload)
    response.raise_for_status()
    return response.json()["embedding"]

def main():
    docs_dir = os.path.join(os.path.dirname(__file__), "raw_markdown_docs")
    md_files = glob.glob(os.path.join(docs_dir, "*.md"))
    
    if not md_files:
        print("No markdown files found in raw_markdown_docs/")
        return

    client = QdrantClient(host=SERVER_IP, port=QDRANT_PORT)
    collection_name = "technical_manuals"

    # Create collection if it doesn't exist
    if not client.collection_exists(collection_name):
        print(f"Creating collection '{collection_name}' in Qdrant...")
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=768, distance=Distance.COSINE),
        )

    points = []
    point_id = 1
    
    for file_path in md_files:
        print(f"Processing {os.path.basename(file_path)}...")
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        import textwrap
        
        # Simple chunking by Markdown headers
        raw_chunks = content.split("\n## ")
        chunks = []
        for i, chunk in enumerate(raw_chunks):
            if i > 0:
                chunk = "## " + chunk
            chunk = chunk.strip()
            if not chunk:
                continue
            
            # Sub-chunk if it's too large (e.g., > 4000 chars) to prevent Ollama memory/context errors
            if len(chunk) > 4000:
                sub_chunks = textwrap.wrap(chunk, width=4000, replace_whitespace=False)
                chunks.extend(sub_chunks)
            else:
                chunks.append(chunk)
        
        for i, chunk in enumerate(chunks):
            print(f"  - Vectorizing chunk {i+1}...")
            try:
                embedding = get_embedding(chunk)
            except Exception as e:
                print(f"    [!] Error vectorizing chunk {i+1}. Skipping. Error: {e}")
                continue
            
            points.append(
                PointStruct(
                    id=point_id,
                    vector=embedding,
                    payload={"text": chunk, "source": os.path.basename(file_path)}
                )
            )
            point_id += 1

    print(f"Uploading {len(points)} vectors to remote Qdrant database in batches...")
    batch_size = 500
    for i in range(0, len(points), batch_size):
        batch = points[i:i + batch_size]
        client.upsert(
            collection_name=collection_name,
            points=batch
        )
        print(f"  - Uploaded batch {i//batch_size + 1}...")
    print("Database vectors have been successfully stored in your remote Qdrant database instance.")

if __name__ == "__main__":
    main()
