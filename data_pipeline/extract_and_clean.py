import os
import re
from pathlib import Path

DESTINATION_DIR = Path("./data_pipeline/raw_markdown_docs")

# Mapping the cloned folders to prefixes
SOURCE_DIRECTORIES = {
    "hacktricks": Path("./hacktricks"),
    "payloads": Path("./PayloadsAllTheThings"),
    "gtfobins": Path("./GTFOBins.github.io")
}

def clean_markdown_content(text: str) -> str:
    """Strips non-technical navigation fluff and contributor banners."""
    text = re.sub(r'\[.*?\]\(.*?\)\s*\|\s*\[.*?\]\(.*?\)', '', text)
    text = re.sub(r'(?i)##\s*Contributors.*?(?=\n##|$)', '', text, flags=re.DOTALL)
    text = re.sub(r'(?i)##\s*License.*?(?=\n##|$)', '', text, flags=re.DOTALL)
    text = re.sub(r'\n[\s\-_*]{3,}\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def execute_extraction_pipeline():
    if not DESTINATION_DIR.exists():
        DESTINATION_DIR.mkdir(parents=True, exist_ok=True)

    file_counter = 0

    for source_label, source_path in SOURCE_DIRECTORIES.items():
        if not source_path.exists():
            print(f"[!] Source '{source_path}' not found. Skipping...")
            continue

        print(f"[*] Processing: {source_label}...")
        for root, _, files in os.walk(source_path):
            for file in files:
                if file.endswith(".md"):
                    source_file_path = Path(root) / file
                    try:
                        with open(source_file_path, 'r', encoding='utf-8', errors='ignore') as src:
                            cleaned_data = clean_markdown_content(src.read())
                        
                        # Skip files that are basically empty after cleaning
                        if len(cleaned_data) < 150:
                            continue
                        
                        # Create a flat, unique filename
                        relative_path_str = str(source_file_path.relative_to(source_path)).replace(os.sep, "_")
                        output_file_path = DESTINATION_DIR / f"{source_label}_{relative_path_str}"
                        
                        with open(output_file_path, 'w', encoding='utf-8') as dest:
                            dest.write(cleaned_data)
                            
                        file_counter += 1
                    except Exception as e:
                        pass # Silently skip unreadable files

    print(f"\n[+] Extraction complete! {file_counter} clean markdown files are ready in {DESTINATION_DIR}")

if __name__ == "__main__":
    execute_extraction_pipeline()
